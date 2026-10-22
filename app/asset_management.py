"""Delete current images while preserving immutable versions and an audit trail."""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .accounts import _account_row
from .auth import Actor, get_actor, require_admin
from .collaboration import guard_asset
from .config import settings
from .database import db, now_iso, one
from .work_blocks import refresh_project_blocks

router = APIRouter(prefix="/api/projects/{project_id}/assets", tags=["assets"])
logger = logging.getLogger(__name__)


class DeleteTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: int = Field(gt=0, le=2**63 - 1, strict=True)
    expected_revision: int = Field(ge=0, le=2**63 - 1, strict=True)


class BatchDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assets: list[DeleteTarget] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def consistent_revisions(self):
        revisions: dict[int, int] = {}
        for target in self.assets:
            if target.asset_id in revisions and revisions[target.asset_id] != target.expected_revision:
                raise ValueError("同一图片不能包含不同版本号，请刷新后重新选择")
            revisions[target.asset_id] = target.expected_revision
        return self


def _media_path(storage_name: str) -> Path:
    """Never follow a stored symlink or traverse outside the media directory."""
    root = settings.media_dir.resolve()
    relative = Path(storage_name)
    if not storage_name or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("invalid_media_path")
    source = root / relative
    resolved = source.resolve()
    if source != resolved or root not in resolved.parents or source.is_symlink():
        raise ValueError("invalid_media_path")
    if source.exists() and not source.is_file():
        raise ValueError("invalid_media_path")
    return source


def _cleanup_media(asset: dict, audit_id: int) -> dict:
    # The current row and a durable pending marker have already committed. A
    # crash here may leave an orphan file, never a live row whose file we removed.
    # The marker records the exact storage name for later operator cleanup.
    outcome = {"status": "pending", "message": "已从当前数据删除，原文件清理待完成"}
    try:
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute("SELECT 1 FROM assets WHERE storage_name=?", (asset["storage_name"],)).fetchone():
                    raise ValueError("media_referenced")
                source = _media_path(asset["storage_name"])
                try:
                    source.unlink()
                    outcome = {"status": "deleted", "message": "原文件已清理"}
                except FileNotFoundError:
                    outcome = {"status": "missing", "message": "原文件已不存在"}
            except (OSError, ValueError, RuntimeError) as error:
                logger.warning("Media cleanup pending for deleted asset %s: %s", asset["id"], type(error).__name__)
            row = conn.execute("SELECT payload_json FROM audit_logs WHERE id=?", (audit_id,)).fetchone()
            payload = json.loads(row["payload_json"])
            payload["media_cleanup"] = {**outcome, "checked_at": now_iso()}
            conn.execute("UPDATE audit_logs SET payload_json=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), audit_id))
    except sqlite3.Error:
        # Deletion already succeeded; do not invite the caller to repeat a
        # destructive operation just because updating cleanup metadata failed.
        logger.exception("Could not update media cleanup audit for asset %s", asset["id"])
    return outcome


def _item(project_id: int, target: DeleteTarget, actor: Actor, *, preview: bool) -> dict:
    result = {"asset_id": target.asset_id, "expected_revision": target.expected_revision}
    with db() as conn:
        conn.execute("BEGIN" if preview else "BEGIN IMMEDIATE")
        # Demotion, disablement and session revocation must take effect even if
        # they happen between the route dependency and an individual batch item.
        try:
            _account_row(conn, actor, admin=True)
        except HTTPException:
            return {**result, "status": "forbidden", "code": "permission_changed", "message": "登录状态或管理员权限已变化，请刷新后重新登录"}
        row = conn.execute("SELECT * FROM assets WHERE id=? AND project_id=?", (target.asset_id, project_id)).fetchone()
        if row is None:
            return {**result, "status": "not_found", "code": "not_in_project", "message": "图片不存在或不属于当前项目"}
        asset = dict(row)
        counts = conn.execute("""SELECT COUNT(*) AS total,
            COALESCE(SUM(deleted_at IS NULL),0) AS active FROM annotations WHERE asset_id=?""", (target.asset_id,)).fetchone()
        version_count = conn.execute("SELECT COUNT(DISTINCT version_id) FROM dataset_version_assets WHERE original_asset_id=?", (target.asset_id,)).fetchone()[0]
        # The count is exact; details are bounded even for long-running projects.
        versions = [dict(row) for row in conn.execute("""SELECT DISTINCT v.id,v.version_number,v.name
            FROM dataset_versions v JOIN dataset_version_assets a ON a.version_id=v.id
            WHERE a.original_asset_id=? ORDER BY v.version_number DESC LIMIT 20""", (target.asset_id,))]
        review_event_count = conn.execute("SELECT COUNT(*) FROM review_events WHERE asset_id=?", (target.asset_id,)).fetchone()[0]
        result.update(filename=asset["filename"], review_status=asset["review_status"], current_revision=asset["revision"],
                      annotation_count=counts["active"], total_annotation_count=counts["total"],
                      review_event_count=review_event_count, version_count=version_count, versions=versions,
                      versions_truncated=version_count > len(versions), versions_preserved=True)
        if asset["revision"] != target.expected_revision:
            return {**result, "status": "conflict", "code": "revision_changed", "message": "图片或标注已变化，请刷新并重新检查后再删除"}
        try:
            guard_asset(conn, target.asset_id, actor, editable=False, action="delete")
        except HTTPException as error:
            locked = error.status_code == 423
            return {**result, "status": "conflict", "code": "locked" if locked else "busy",
                    "message": "图片正由其他成员编辑，请稍后删除" if locked else "AI 正在处理这张图片或等待执行，请等待任务结束后再删除"}
        try:
            source = _media_path(asset["storage_name"])
        except (OSError, ValueError, RuntimeError):
            return {**result, "status": "conflict", "code": "invalid_storage", "message": "图片文件路径异常，请管理员检查存储后再删除"}
        result["media_exists"] = source.is_file()
        if preview:
            return {**result, "status": "eligible", "code": "ready", "message": "可删除当前图片及标注；已保存版本保留"}
        now = now_iso()
        summary = {"filename": asset["filename"], "storage_name": asset["storage_name"], "sha256": asset["sha256"],
                   "revision": asset["revision"], "review_status": asset["review_status"],
                   "annotation_count": counts["active"], "total_annotation_count": counts["total"],
                   "review_event_count": review_event_count, "version_count": version_count, "versions_preserved": True,
                   "batch": True, "media_cleanup": {"status": "pending"}}
        audit_id = conn.execute("""INSERT INTO audit_logs
            (actor,action,resource_type,resource_id,project_id,payload_json,created_at) VALUES (?,?,?,?,?,?,?)""",
            (actor.id, "delete", "asset", str(target.asset_id), project_id, json.dumps(summary, ensure_ascii=False), now)).lastrowid
        conn.execute("DELETE FROM assets WHERE id=? AND project_id=? AND revision=?", (target.asset_id, project_id, target.expected_revision))
        refresh_project_blocks(conn, project_id, actor)
        conn.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, project_id))
    cleanup = _cleanup_media(asset, audit_id)
    return {**result, "status": "succeeded", "code": "deleted", "message": "已删除当前图片及标注", "media_cleanup": cleanup}


def _run(project_id: int, payload: BatchDelete, actor: Actor, *, preview: bool) -> dict:
    require_admin(actor)
    if not one("SELECT id FROM projects WHERE id=?", (project_id,)):
        raise HTTPException(404, "项目不存在")
    unique = {target.asset_id: target for target in payload.assets}
    items = [_item(project_id, target, actor, preview=preview) for target in unique.values()]
    counts = {status: sum(item["status"] == status for item in items)
              for status in ("eligible", "succeeded", "skipped", "conflict", "forbidden", "not_found")}
    return {"project_id": project_id, "action": "delete", "preview": preview, "total": len(items),
            "duplicate_count": len(payload.assets) - len(items), "counts": counts, "items": items,
            "cleanup_pending_count": sum(item.get("media_cleanup", {}).get("status") == "pending" for item in items)}


@router.post("/delete-preview")
def preview_delete_assets(project_id: int, payload: BatchDelete, actor: Actor = Depends(get_actor)) -> dict:
    return _run(project_id, payload, actor, preview=True)


@router.post("/delete-batch")
def delete_assets(project_id: int, payload: BatchDelete, actor: Actor = Depends(get_actor)) -> dict:
    return _run(project_id, payload, actor, preview=False)
