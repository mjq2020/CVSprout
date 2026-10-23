"""Durable annotation assignments; every ownership change is serialized with writes.

Image locks protect a short editing session. A block owner survives logout and
network failure. Membership never copies images or changes dataset split rules.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .auth import Actor, get_actor, require_admin, require_reviewer
from .database import db, now_iso

router = APIRouter(prefix="/api/projects/{project_id}/work-blocks", tags=["work-blocks"])


class CreateBlocks(BaseModel):
    model_config = ConfigDict(extra="forbid")
    block_size: int = Field(default=50, ge=1, le=500, strict=True)


class ClaimBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    block_id: int | None = Field(default=None, gt=0, le=2**63 - 1, strict=True)


class ChangeBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0, le=2**63 - 1, strict=True)
    reason: str = Field(default="", max_length=2000)


class AssignBlock(ChangeBlock):
    user_id: int = Field(gt=0, le=2**63 - 1, strict=True)


def owner_key(actor: Actor) -> str:
    return f"user:{actor.user_id}" if actor.user_id is not None else f"actor:{actor.id}"


def owns_block(block, actor: Actor) -> bool:
    return block["owner_key"] == owner_key(actor)


def _project(conn, project_id: int, *, write: bool = False) -> dict:
    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "项目不存在")
    if write and row["archived_at"]:
        raise HTTPException(409, "项目已归档，请先恢复项目")
    return dict(row)


def _block(conn, project_id: int, block_id: int) -> dict:
    row = conn.execute("SELECT * FROM work_blocks WHERE id=? AND project_id=?", (block_id, project_id)).fetchone()
    if row is None:
        raise HTTPException(404, "任务块不存在或不属于当前项目")
    return dict(row)


def _assets(conn, block_id: int) -> list[dict]:
    return [dict(row) for row in conn.execute("""SELECT a.id AS asset_id,a.filename,a.review_status,
        a.revision,a.revision AS expected_revision,a.negative_confirmed,a.lock_owner,a.lock_expires_at,
        (SELECT COUNT(*) FROM annotations n WHERE n.asset_id=a.id AND n.deleted_at IS NULL) AS annotation_count
        FROM work_block_assets m JOIN assets a ON a.id=m.asset_id
        WHERE m.block_id=? ORDER BY m.position,a.id""", (block_id,))]


def _active_job(conn, project_id: int, asset_ids: list[int]):
    from .collaboration import active_job_for_assets
    return active_job_for_assets(conn, project_id, asset_ids) if asset_ids else None


def _state(block, assets: list[dict], busy: bool) -> str:
    if not assets or all(item["review_status"] == "approved" for item in assets):
        return "completed"
    if not block["owner_key"]:
        # A released block with submitted images remains in the review queue.
        if all(item["review_status"] in {"submitted", "approved"} for item in assets):
            return "submitted"
        return "preparing" if busy else "available"
    if any(item["review_status"] == "rejected" for item in assets):
        return "rejected"
    if all(item["review_status"] in {"submitted", "approved"} for item in assets):
        return "submitted"
    return "in_progress"


def _present(conn, block, actor: Actor, *, detail: bool = False) -> dict:
    assets = _assets(conn, block["id"])
    ids = [item["asset_id"] for item in assets]
    busy = _active_job(conn, block["project_id"], ids)
    status = _state(block, assets, bool(busy))
    result = {**block, "status": status, "is_mine": owns_block(block, actor), "asset_ids": ids,
              "total": len(ids), "approved_count": sum(item["review_status"] == "approved" for item in assets),
              "submitted_count": sum(item["review_status"] == "submitted" for item in assets),
              "rejected_count": sum(item["review_status"] == "rejected" for item in assets),
              "active_job_id": busy}
    if detail:
        result["items"] = [{key: value for key, value in item.items() if key not in {"lock_owner", "lock_expires_at"}} for item in assets]
        result["events"] = [{**dict(row), "payload": json.loads(row["payload_json"])} for row in conn.execute(
            "SELECT * FROM work_block_events WHERE block_id=? ORDER BY id DESC LIMIT 100", (block["id"],))]
    return result


def _event(conn, block, actor: Actor | None, action: str, **payload) -> None:
    who = actor.id if actor else "system"
    encoded = json.dumps(payload, ensure_ascii=False)
    now = now_iso()
    conn.execute("INSERT INTO work_block_events(block_id,actor,action,payload_json,created_at) VALUES (?,?,?,?,?)",
                 (block["id"], who, action, encoded, now))
    conn.execute("""INSERT INTO audit_logs(actor,action,resource_type,resource_id,project_id,payload_json,created_at)
        VALUES (?,?,?,?,?,?,?)""", (who, action, "work_block", str(block["id"]), block["project_id"], encoded, now))


def _refresh(conn, block, actor: Actor | None = None) -> None:
    assets = _assets(conn, block["id"])
    state = _state(block, assets, bool(_active_job(conn, block["project_id"], [item["asset_id"] for item in assets])))
    completed_at = (block["completed_at"] or now_iso()) if state == "completed" else None
    conn.execute("UPDATE work_blocks SET status=?,completed_at=?,revision=revision+1,updated_at=? WHERE id=?",
                 (state, completed_at, now_iso(), block["id"]))
    if state == "completed" and block["status"] != "completed":
        _event(conn, block, actor, "completed", owner_key=block["owner_key"])


def refresh_block(conn: sqlite3.Connection, asset_id: int, actor: Actor | None = None) -> None:
    """Call in the same write transaction after changing an image or its review."""
    row = conn.execute("SELECT b.* FROM work_blocks b JOIN work_block_assets m ON m.block_id=b.id WHERE m.asset_id=?", (asset_id,)).fetchone()
    if row:
        _refresh(conn, dict(row), actor)


def refresh_project_blocks(conn: sqlite3.Connection, project_id: int, actor: Actor | None = None) -> None:
    """Deletion removes membership; refresh project blocks after deleting images."""
    for row in conn.execute("SELECT * FROM work_blocks WHERE project_id=?", (project_id,)).fetchall():
        _refresh(conn, dict(row), actor)


def guard_block_asset(conn: sqlite3.Connection, asset, actor: Actor,
                      action: Literal["edit", "review", "delete", "reopen"] = "edit") -> None:
    project = conn.execute("SELECT work_blocks_enabled FROM projects WHERE id=?", (asset["project_id"],)).fetchone()
    if not project or not project["work_blocks_enabled"]:
        return
    if action == "delete":
        require_admin(actor)
        return
    if action == "review":
        require_reviewer(actor)
        return
    row = conn.execute("SELECT b.* FROM work_blocks b JOIN work_block_assets m ON m.block_id=b.id WHERE m.asset_id=?", (asset["id"],)).fetchone()
    if row is None:
        # Enabling blocks excludes historical review queues and approved images.
        # Reviewers may return those to draft so an administrator can group them.
        if action == "reopen" and asset["review_status"] in {"submitted", "approved", "rejected"}:
            require_reviewer(actor)
            return
        raise HTTPException(403, "此项目已启用任务块，请先由管理员分块并领取后再标注")
    block = dict(row)
    assets = _assets(conn, block["id"])
    if _state(block, assets, False) == "completed":
        raise HTTPException(409, "任务块已完成；如需返工，请管理员重新打开整个任务块")
    if not owns_block(block, actor):
        raise HTTPException(403, "请先领取此任务块；只能修改自己负责的图片，管理员可转交任务块")


def eligible_ai_asset_ids(conn: sqlite3.Connection, project_id: int, ids: list[int], *,
                          owner_key: str | None = None) -> list[int]:
    # A claimed block may use AI under its owner's identity, but review queues
    # and completed work are protected even for that owner and include_reviewed.
    # Unclaimed, untouched blocks retain the initial AI-preparation workflow.
    query = """SELECT m.asset_id,a.review_status,b.owner_key,b.human_started,b.status
        FROM work_block_assets m JOIN work_blocks b ON b.id=m.block_id
        JOIN assets a ON a.id=m.asset_id WHERE b.project_id=?"""
    params: tuple = (project_id,)
    # Workers check before inference and before saving each image. Use the
    # membership primary key instead of scanning a large project twice per image.
    if len(ids) == 1:
        query += " AND m.asset_id=?"
        params += (ids[0],)
    blocked = {
        row["asset_id"] for row in conn.execute(query, params)
        if (row["status"] == "completed" or row["review_status"] not in {"draft", "rejected"}
            or (row["owner_key"] is not None and row["owner_key"] != owner_key)
            or (row["owner_key"] is None and row["human_started"]))
    }
    return [asset_id for asset_id in ids if asset_id not in blocked]


def guard_block_ai(conn: sqlite3.Connection, project_id: int, asset_ids: list[int], *,
                   owner_key: str | None = None) -> None:
    if len(eligible_ai_asset_ids(conn, project_id, asset_ids, owner_key=owner_key)) == len(asset_ids):
        return
    raise HTTPException(409, "AI 只能处理自己负责的任务块中未提交或退回修改的图片；其他成员、已交回、待审核或已完成的任务块图片受保护")


def _list(conn, project, actor: Actor) -> dict:
    rows = conn.execute("SELECT * FROM work_blocks WHERE project_id=? ORDER BY id", (project["id"],)).fetchall()
    count = conn.execute("""SELECT COUNT(*) FROM assets a WHERE a.project_id=?
        AND a.review_status NOT IN ('submitted','approved')
        AND NOT EXISTS (SELECT 1 FROM work_block_assets m WHERE m.asset_id=a.id)""", (project["id"],)).fetchone()[0]
    return {"enabled": bool(project["work_blocks_enabled"]), "blocks": [_present(conn, dict(row), actor) for row in rows],
            "unassigned_count": count}


@router.get("")
def list_blocks(project_id: int, actor: Actor = Depends(get_actor)) -> dict:
    with db() as conn:
        conn.execute("BEGIN")
        return _list(conn, _project(conn, project_id), actor)


@router.post("", status_code=201)
def create_blocks(project_id: int, payload: CreateBlocks, actor: Actor = Depends(get_actor)) -> dict:
    require_admin(actor)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        project = _project(conn, project_id, write=True)
        assets = [dict(row) for row in conn.execute("""SELECT a.* FROM assets a WHERE a.project_id=?
            AND a.review_status NOT IN ('submitted','approved')
            AND NOT EXISTS (SELECT 1 FROM work_block_assets m WHERE m.asset_id=a.id) ORDER BY a.id""", (project_id,))]
        if not assets:
            raise HTTPException(409, "没有可分块的图片：新块只包含未分配且尚未提交审核的图片")
        if any(item["lock_owner"] and item["lock_expires_at"] and item["lock_expires_at"] > now_iso() for item in assets):
            raise HTTPException(423, "部分待分块图片仍在编辑，请关闭编辑页面并释放图片锁后再分块")
        count = conn.execute("SELECT COUNT(*) FROM work_blocks WHERE project_id=?", (project_id,)).fetchone()[0]
        created = 0
        for offset in range(0, len(assets), payload.block_size):
            chunk = assets[offset:offset + payload.block_size]
            now = now_iso()
            cursor = conn.execute("INSERT INTO work_blocks(project_id,name,created_at,updated_at) VALUES (?,?,?,?)",
                                  (project_id, f"任务块 {count + created + 1:03d}", now, now))
            block_id = cursor.lastrowid
            conn.executemany("INSERT INTO work_block_assets(asset_id,block_id,position) VALUES (?,?,?)",
                             [(item["id"], block_id, index) for index, item in enumerate(chunk)])
            # Existing manual annotations are protected even before a first claim.
            manual = conn.execute("""SELECT 1 FROM annotations n JOIN work_block_assets m ON m.asset_id=n.asset_id
                WHERE m.block_id=? AND n.deleted_at IS NULL AND (n.source='manual' OR n.human_modified=1) LIMIT 1""", (block_id,)).fetchone()
            if manual or any(item["negative_confirmed"] for item in chunk):
                conn.execute("UPDATE work_blocks SET human_started=1 WHERE id=?", (block_id,))
            block = _block(conn, project_id, block_id)
            _refresh(conn, block, actor)
            _event(conn, block, actor, "created", asset_ids=[item["id"] for item in chunk])
            created += 1
        conn.execute("UPDATE projects SET work_blocks_enabled=1,updated_at=? WHERE id=?", (now_iso(), project_id))
        return {**_list(conn, _project(conn, project_id), actor), "created_count": created}


@router.get("/assignees")
def block_assignees(project_id: int, actor: Actor = Depends(get_actor)) -> list[dict]:
    require_admin(actor)
    with db() as conn:
        _project(conn, project_id)
        return [dict(row) for row in conn.execute("SELECT id,username,display_name,role FROM users WHERE enabled=1 ORDER BY display_name,id")]


def _available(conn, block) -> None:
    assets = _assets(conn, block["id"])
    if block["owner_key"] or _state(block, assets, False) != "available":
        raise HTTPException(409, "任务块已被领取或已提交、完成，请刷新后选择其他任务块")
    job = _active_job(conn, block["project_id"], [item["asset_id"] for item in assets])
    if job:
        raise HTTPException(409, f"任务块正在由 AI 任务 #{job} 准备，请等待处理结束后领取")
    if any(item["lock_owner"] and item["lock_expires_at"] and item["lock_expires_at"] > now_iso() for item in assets):
        raise HTTPException(423, "任务块中有图片仍在编辑，请释放图片锁后再领取")


def _fence(conn, block_id: int) -> None:
    conn.execute("""UPDATE assets SET lock_owner=NULL,lock_expires_at=NULL,lock_token=NULL,
        revision=revision+1,updated_at=? WHERE id IN (SELECT asset_id FROM work_block_assets WHERE block_id=?)""", (now_iso(), block_id))


@router.post("/claim")
def claim_block(project_id: int, payload: ClaimBlock, actor: Actor = Depends(get_actor)) -> dict:
    if actor.role not in {"admin", "annotator", "reviewer"}:
        raise HTTPException(403, "当前账号不能领取标注任务")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _project(conn, project_id, write=True)
        owned = conn.execute("SELECT * FROM work_blocks WHERE project_id=? AND owner_key=?", (project_id, owner_key(actor))).fetchall()
        if any(_state(row, _assets(conn, row["id"]), False) in {"in_progress", "rejected"} for row in owned):
            raise HTTPException(409, "请先完成或交回当前未提交的任务块，再领取下一块；退回任务需要优先处理")
        if payload.block_id is not None:
            block = _block(conn, project_id, payload.block_id)
            _available(conn, block)
        else:
            block = None
            for row in conn.execute("SELECT * FROM work_blocks WHERE project_id=? AND owner_key IS NULL ORDER BY id", (project_id,)).fetchall():
                try:
                    _available(conn, row)
                except HTTPException:
                    continue
                block = dict(row)
                break
            if block is None:
                raise HTTPException(409, "暂时没有可领取的任务块，请等待 AI 完成或管理员创建新块")
        conn.execute("""UPDATE work_blocks SET owner_key=?,owner_user_id=?,owner_name=?,human_started=1,
            claimed_at=?,updated_at=? WHERE id=?""", (owner_key(actor), actor.user_id, actor.display_name or actor.username or actor.id,
                                                        now_iso(), now_iso(), block["id"]))
        _fence(conn, block["id"])
        _event(conn, block, actor, "claimed", owner_key=owner_key(actor))
        _refresh(conn, _block(conn, project_id, block["id"]), actor)
        return _present(conn, _block(conn, project_id, block["id"]), actor, detail=True)


@router.get("/{block_id}")
def get_block(project_id: int, block_id: int, actor: Actor = Depends(get_actor)) -> dict:
    with db() as conn:
        conn.execute("BEGIN")
        _project(conn, project_id)
        return _present(conn, _block(conn, project_id, block_id), actor, detail=True)


def _changeable(conn, project_id: int, block_id: int, payload: ChangeBlock) -> dict:
    _project(conn, project_id, write=True)
    block = _block(conn, project_id, block_id)
    if block["revision"] != payload.expected_revision:
        raise HTTPException(409, "任务块已变化，请刷新后重新操作")
    assets = _assets(conn, block_id)
    if _active_job(conn, project_id, [item["asset_id"] for item in assets]):
        raise HTTPException(409, "任务块仍有 AI 任务，请等待任务结束后再修改分配")
    return {**block, "status": _state(block, assets, False)}


@router.post("/{block_id}/release")
def release_block(project_id: int, block_id: int, payload: ChangeBlock, actor: Actor = Depends(get_actor)) -> dict:
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        block = _changeable(conn, project_id, block_id, payload)
        if actor.role != "admin" and not owns_block(block, actor):
            raise HTTPException(403, "只能交回自己的任务块；管理员可以转交或回收")
        if not block["owner_key"] or block["status"] == "completed":
            raise HTTPException(409, "此任务块没有工作占用，或已经完成，无需交回")
        conn.execute("UPDATE work_blocks SET owner_key=NULL,owner_user_id=NULL,owner_name='',claimed_at=NULL WHERE id=?", (block_id,))
        _fence(conn, block_id)
        _event(conn, block, actor, "released", previous_owner=block["owner_key"], reason=payload.reason.strip())
        _refresh(conn, _block(conn, project_id, block_id), actor)
        return _present(conn, _block(conn, project_id, block_id), actor, detail=True)


@router.post("/{block_id}/assign")
def assign_block(project_id: int, block_id: int, payload: AssignBlock, actor: Actor = Depends(get_actor)) -> dict:
    require_admin(actor)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        block = _changeable(conn, project_id, block_id, payload)
        if block["status"] == "completed":
            raise HTTPException(409, "已完成的任务块不能重新分配，请先明确重新打开")
        user = conn.execute("SELECT id,username,display_name FROM users WHERE id=? AND enabled=1", (payload.user_id,)).fetchone()
        if user is None:
            raise HTTPException(422, "请选择有效且已启用的成员账号")
        key = f"user:{user['id']}"
        conn.execute("""UPDATE work_blocks SET owner_key=?,owner_user_id=?,owner_name=?,human_started=1,claimed_at=? WHERE id=?""",
                     (key, user["id"], user["display_name"] or user["username"], now_iso(), block_id))
        _fence(conn, block_id)
        _event(conn, block, actor, "assigned", previous_owner=block["owner_key"], owner_key=key, reason=payload.reason.strip())
        _refresh(conn, _block(conn, project_id, block_id), actor)
        return _present(conn, _block(conn, project_id, block_id), actor, detail=True)


@router.post("/{block_id}/reopen")
def reopen_block(project_id: int, block_id: int, payload: ChangeBlock, actor: Actor = Depends(get_actor)) -> dict:
    require_admin(actor)
    if not payload.reason.strip():
        raise HTTPException(422, "请填写重新打开任务块的原因")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        block = _changeable(conn, project_id, block_id, payload)
        assets = _assets(conn, block_id)
        if block["status"] != "completed" or not assets:
            raise HTTPException(409, "只能重新打开包含图片且已全部审核完成的任务块")
        now = now_iso()
        conn.executemany("""INSERT INTO review_events(asset_id,from_status,to_status,actor,reason,created_at)
            VALUES (?,?,'draft',?,?,?)""", [(item["asset_id"], item["review_status"], actor.id, payload.reason.strip(), now) for item in assets])
        conn.execute("""UPDATE assets SET review_status='draft',review_reason=? WHERE id IN
            (SELECT asset_id FROM work_block_assets WHERE block_id=?)""", (payload.reason.strip(), block_id))
        _fence(conn, block_id)
        _event(conn, block, actor, "reopened", reason=payload.reason.strip())
        _refresh(conn, block, actor)
        return _present(conn, _block(conn, project_id, block_id), actor, detail=True)
