"""Project membership is the boundary for every project-owned API resource."""
from __future__ import annotations

from dataclasses import replace
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from .auth import Actor, get_actor, require_admin, require_project_manager
from .database import db, now_iso

router = APIRouter(prefix="/api/projects/{project_id}", tags=["project members"])


def access(conn, project_id: int, actor: Actor, *, write=False, manage=False, export=False):
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if project is None:
        raise HTTPException(404, "项目不存在或无权访问")
    # Refresh account privileges within write transactions, including demotion.
    role = actor.role
    if actor.user_id is not None:
        user = conn.execute("SELECT role,enabled,must_change_password FROM users WHERE id=?", (actor.user_id,)).fetchone()
        if not user or not user["enabled"]:
            raise HTTPException(401, "账号不可用，请重新登录")
        if actor.session_id and not conn.execute("SELECT 1 FROM user_sessions WHERE user_id=? AND session_id=? AND expires_at>?",
                                                (actor.user_id, actor.session_id, now_iso())).fetchone():
            raise HTTPException(401, "登录已失效，请重新登录")
        if user["must_change_password"]:
            from .auth import password_change_required
            raise password_change_required()
        role = user["role"]
    if role == "admin":
        return "owner", True
    if actor.user_id is not None and project["owner_user_id"] == actor.user_id:
        return "owner", True
    member = conn.execute("SELECT role,can_export FROM project_members WHERE project_id=? AND user_id=?",
                          (project_id, actor.user_id)).fetchone()
    if member is None:
        raise HTTPException(404, "项目不存在或无权访问")
    if manage or (write and member["role"] != "editor"):
        raise HTTPException(403, "需要项目所有者权限" if manage else "此项目为只读分享")
    can_export = member["role"] == "editor" or bool(member["can_export"])
    if export and not can_export:
        raise HTTPException(403, "项目所有者尚未授予下载或导出权限")
    return member["role"], can_export


def recheck_access(conn, actor: Actor, *, manage=False):
    """Revalidate at commit boundaries after uploads or expensive preparation."""
    if actor.project_id is not None:
        access(conn, actor.project_id, actor, write=True, manage=manage)


# Paths containing a project ID are scoped directly. Indirect resource IDs must
# first resolve to their project; never infer authorization from a request body.
RESOURCE_PROJECT = {
    "assets": "SELECT project_id FROM assets WHERE id=?",
    "labels": "SELECT project_id FROM labels WHERE id=?",
    "annotations": "SELECT a.project_id FROM annotations n JOIN assets a ON a.id=n.asset_id WHERE n.id=?",
    "jobs": "SELECT project_id FROM jobs WHERE id=?",
    "versions": "SELECT project_id FROM dataset_versions WHERE id=?",
    "training-runs": "SELECT project_id FROM training_runs WHERE id=?",
    "conversions": "SELECT r.project_id FROM model_conversions c JOIN training_runs r ON r.id=c.training_run_id WHERE c.id=?",
}


def scope_actor(request: Request, actor: Actor) -> Actor:
    path = request.url.path.rstrip("/")
    match = re.match(r"^/api/(projects|assets|labels|annotations|jobs|versions|training-runs|conversions)/([^/]+)(?:/|$)", path)
    if not match:
        return actor
    resource, value = match.groups()
    if len(value) > 19 or not value.isascii() or not value.isdigit() or not 0 < int(value) < 2**63:
        raise HTTPException(404, "资源不存在或无权访问")
    with db() as conn:
        if resource == "projects":
            project_id = int(value)
        else:
            row = conn.execute(RESOURCE_PROJECT[resource], (int(value),)).fetchone()
            if row is None:
                raise HTTPException(404, "资源不存在或无权访问")
            project_id = row[0]
        download = "/export/" in path or path.endswith(("/artifact", "/deployment", "/manifest"))
        role, _ = access(conn, project_id, actor, write=request.method not in {"GET", "HEAD", "OPTIONS"}, export=download)
    return replace(actor, project_id=project_id, project_role=role)


def permissions(conn, project, actor: Actor):
    role, can_export = access(conn, project["id"], actor)
    return {"project_role": role, "permissions": {"edit": role in {"owner", "editor"},
            "manage": role == "owner", "export": can_export},
            "is_owner": actor.user_id is not None and project["owner_user_id"] == actor.user_id}


class MemberUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=2, max_length=64)
    role: Literal["editor", "viewer"] = "editor"
    can_export: bool = False


class OwnerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=2, max_length=64)


def _event(conn, actor, project_id, action, user_id, **details):
    import json
    conn.execute("""INSERT INTO audit_logs(actor,action,resource_type,resource_id,project_id,payload_json,created_at)
        VALUES (?,?,'project_member',?,?,?,?)""",
        (actor.id, action, str(user_id), project_id, json.dumps(details, ensure_ascii=False), now_iso()))


def _revoke_work(conn, project_id, user_id):
    # Release editing leases and work assignments, retaining annotation/history.
    username = conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()[0]
    prefix = f"user:{username}:"
    conn.execute("""UPDATE assets SET lock_owner=NULL,lock_expires_at=NULL,lock_token=NULL,revision=revision+1
        WHERE project_id=? AND substr(lock_owner,1,?)=?""", (project_id, len(prefix), prefix))
    from .work_blocks import _fence, _refresh, _block, _event as block_event
    blocks = conn.execute("SELECT id FROM work_blocks WHERE project_id=? AND owner_user_id=? AND status<>'completed'", (project_id, user_id)).fetchall()
    for block in blocks:
        block_event(conn, _block(conn, project_id, block["id"]), None, "access_revoked", previous_owner=f"user:{user_id}")
        conn.execute("""UPDATE work_blocks SET owner_key=NULL,owner_user_id=NULL,owner_name='',claimed_at=NULL
            WHERE id=?""", (block["id"],))
        _fence(conn, block["id"])
        _refresh(conn, _block(conn, project_id, block["id"]))
    # A previously authorized AI job must not keep editing after access is revoked.
    conn.execute("""UPDATE jobs SET cancel_requested=1 WHERE project_id=? AND status IN ('queued','running')
        AND json_extract(settings_json,'$.block_owner_key')=?""", (project_id, f"user:{user_id}"))


@router.get("/members")
def list_members(project_id: int, actor: Actor = Depends(get_actor)):
    require_project_manager(actor)
    with db() as conn:
        access(conn, project_id, actor, manage=True)
        owner = conn.execute("""SELECT u.id,u.username,u.display_name,u.enabled FROM users u
            JOIN projects p ON p.owner_user_id=u.id WHERE p.id=?""", (project_id,)).fetchone()
        rows = conn.execute("""SELECT u.id,u.username,u.display_name,u.enabled,m.role,m.can_export FROM project_members m
            JOIN users u ON u.id=m.user_id WHERE m.project_id=? ORDER BY u.display_name,u.id""", (project_id,)).fetchall()
        return {"owner": dict(owner) if owner else None, "members": [dict(row) for row in rows]}


@router.put("/members")
def set_member(project_id: int, payload: MemberUpsert, actor: Actor = Depends(get_actor)):
    require_project_manager(actor)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        access(conn, project_id, actor, manage=True)
        user = conn.execute("SELECT id,username,display_name,role FROM users WHERE username=? AND enabled=1",
                            (payload.username.strip().lower(),)).fetchone()
        if not user:
            raise HTTPException(404, "未找到已启用的账号，请核对完整登录账号")
        if user["role"] == "admin":
            raise HTTPException(409, "系统管理员已拥有全部项目权限，无需添加为成员")
        owner = conn.execute("SELECT owner_user_id FROM projects WHERE id=?", (project_id,)).fetchone()[0]
        if user["id"] == owner:
            raise HTTPException(409, "项目所有者无需添加为成员")
        previous = conn.execute("SELECT role FROM project_members WHERE project_id=? AND user_id=?", (project_id, user["id"])).fetchone()
        if previous and previous["role"] == "editor" and payload.role == "viewer":
            _revoke_work(conn, project_id, user["id"])
        conn.execute("""INSERT INTO project_members(project_id,user_id,role,can_export,created_at)
            VALUES (?,?,?,?,?) ON CONFLICT(project_id,user_id) DO UPDATE SET role=excluded.role,can_export=excluded.can_export""",
            (project_id, user["id"], payload.role, payload.can_export, now_iso()))
        _event(conn, actor, project_id, "share", user["id"], role=payload.role, can_export=payload.can_export)
        return {**dict(user), "role": payload.role, "can_export": payload.can_export}


@router.delete("/members/{user_id}")
def remove_member(project_id: int, user_id: int = Path(gt=0, le=2**63 - 1), actor: Actor = Depends(get_actor)):
    require_project_manager(actor)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        access(conn, project_id, actor, manage=True)
        if not conn.execute("DELETE FROM project_members WHERE project_id=? AND user_id=?", (project_id, user_id)).rowcount:
            raise HTTPException(404, "项目成员不存在")
        _revoke_work(conn, project_id, user_id)
        _event(conn, actor, project_id, "unshare", user_id)
    return {"removed": True}


@router.put("/owner")
def assign_owner(project_id: int, payload: OwnerUpdate, actor: Actor = Depends(get_actor)):
    require_admin(actor)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        from .accounts import _account_row
        _account_row(conn, actor, admin=True)
        access(conn, project_id, actor, manage=True)
        user = conn.execute("SELECT id FROM users WHERE username=? AND enabled=1", (payload.username.strip().lower(),)).fetchone()
        if not user:
            raise HTTPException(404, "未找到已启用的账号")
        old = conn.execute("SELECT owner_user_id FROM projects WHERE id=?", (project_id,)).fetchone()[0]
        if old is not None and old != user["id"]:
            _revoke_work(conn, project_id, old)
        conn.execute("UPDATE projects SET owner_user_id=?,updated_at=? WHERE id=?", (user["id"], now_iso(), project_id))
        conn.execute("DELETE FROM project_members WHERE project_id=? AND user_id=?", (project_id, user["id"]))
        _event(conn, actor, project_id, "assign_owner", user["id"], previous_owner=old)
    return {"owner_user_id": user["id"]}
