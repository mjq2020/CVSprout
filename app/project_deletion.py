"""Delete a confirmed project and its owned files without touching shared models."""
from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .accounts import _account_row
from .auth import Actor, get_actor, require_admin
from .config import settings
from .database import db, now_iso


router = APIRouter(prefix="/api/projects", tags=["projects"])
logger = logging.getLogger(__name__)


class ProjectDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Deliberately do not trim: the whole saved project name must be entered.
    confirmation_name: str = Field(min_length=1, max_length=100)


def _checked_path(item: dict) -> Path:
    roots = {"media": settings.media_dir, "thumbnail": settings.thumbnail_dir,
             "version": settings.versions_dir, "run": settings.training_dir,
             "conversion": settings.training_dir}
    configured_root = roots[item["kind"]]
    root = configured_root.resolve()
    # A relocated data_dir itself is supported; an unexpected child-directory
    # symlink must not redirect cleanup into a different data tree.
    if root != settings.data_dir.resolve() / configured_root.name:
        raise ValueError("invalid_storage_root")
    name = item["name"]
    if not isinstance(name, str) or not name or name in {".", ".."} or Path(name).name != name or "\\" in name:
        raise ValueError("invalid_storage_name")
    path = root / name
    if path.is_symlink() or path.resolve() != path or root not in path.parents:
        raise ValueError("invalid_storage_path")
    if path.exists() and (not path.is_dir() if item["directory"] else not path.is_file()):
        raise ValueError("invalid_storage_type")
    return path


def _remove_path(path: Path, is_directory: bool) -> None:
    if is_directory:
        # shutil.rmtree does not traverse symlinks within a directory tree.
        shutil.rmtree(path)
    else:
        path.unlink()


def _owned_files(conn, project_id: int) -> list[dict]:
    items = []
    assets = conn.execute("SELECT storage_name,sha256 FROM assets WHERE project_id=?", (project_id,)).fetchall()
    versions = [row[0] for row in conn.execute("SELECT id FROM dataset_versions WHERE project_id=?", (project_id,))]
    for asset in assets:
        items.append({"kind": "media", "name": asset["storage_name"], "directory": False})
    for version_id in versions:
        items.append({"kind": "version", "name": f"version-{version_id}", "directory": True})
    for row in conn.execute("SELECT id FROM training_runs WHERE project_id=?", (project_id,)):
        items.append({"kind": "run", "name": f"run-{row[0]}", "directory": True})
    for row in conn.execute("SELECT c.id FROM model_conversions c JOIN training_runs r ON r.id=c.training_run_id WHERE r.project_id=?", (project_id,)):
        items.append({"kind": "conversion", "name": f"conversion-{row[0]}", "directory": True})

    # Live-image thumbnails are shared by content hash across projects.
    shared = {row[0] for row in conn.execute("SELECT DISTINCT sha256 FROM assets WHERE project_id<>?", (project_id,))}
    hashes = {asset["sha256"] for asset in assets if re.fullmatch(r"[0-9a-f]{64}", asset["sha256"]) and asset["sha256"] not in shared}
    version_ids = set(versions)
    if settings.thumbnail_dir.exists():
        for path in settings.thumbnail_dir.iterdir():
            live = re.fullmatch(r"([0-9a-f]{64})-(\d+)\.webp", path.name)
            frozen = re.fullmatch(r"version-(\d+)-\d+-\d+\.webp", path.name)
            if (live and live[1] in hashes) or (frozen and int(frozen[1]) in version_ids):
                items.append({"kind": "thumbnail", "name": path.name, "directory": False})
    return items


def _protect_shared_files(conn, project_id: int, items: list[dict]) -> None:
    # Never use artifact_path as a deletion target. It is only an additional
    # protection when another project's metadata references one of our files.
    protected = {settings.training_base_model.resolve(), settings.training_segmentation_model.resolve()}
    for directory in settings.training_model_dirs:
        if directory.is_dir():
            protected.update(path.resolve() for path in directory.glob("*.pt"))
    for row in conn.execute("SELECT artifact_path,settings_json FROM training_runs WHERE project_id<>?", (project_id,)):
        if row["artifact_path"]:
            protected.add(Path(row["artifact_path"]).resolve())
        try:
            config = json.loads(row["settings_json"])
            if isinstance(config, dict) and isinstance(config.get("base_model"), str):
                protected.add(Path(config["base_model"]).resolve())
        except (ValueError, TypeError):
            pass
    for row in conn.execute("SELECT c.artifact_path FROM model_conversions c JOIN training_runs r ON r.id=c.training_run_id WHERE r.project_id<>? AND c.artifact_path IS NOT NULL", (project_id,)):
        protected.add(Path(row[0]).resolve())
    models_root = settings.models_dir.resolve()
    for item in items:
        path = _checked_path(item)
        if (path == models_root or models_root in path.parents
                or item["directory"] and models_root.is_relative_to(path)
                or any(path == source or item["directory"] and source.is_relative_to(path) for source in protected)):
            raise HTTPException(409, "项目文件仍被共享模型或其他项目引用，请先迁移共享文件后再删除")


def _ensure_idle(conn, project_id: int) -> None:
    if conn.execute("SELECT 1 FROM jobs WHERE project_id=? AND status IN ('queued','running') LIMIT 1", (project_id,)).fetchone():
        raise HTTPException(409, "项目还有排队或运行中的 AI 标注任务，请先取消或等待任务结束")
    if conn.execute("SELECT 1 FROM training_runs WHERE project_id=? AND status IN ('queued','running') LIMIT 1", (project_id,)).fetchone():
        raise HTTPException(409, "项目还有排队或运行中的训练任务，请先取消或等待任务结束")
    if conn.execute("SELECT 1 FROM model_conversions c JOIN training_runs r ON r.id=c.training_run_id WHERE r.project_id=? AND c.status IN ('queued','running') LIMIT 1", (project_id,)).fetchone():
        raise HTTPException(409, "项目还有排队或运行中的模型转换任务，请先取消或等待任务结束")
    if conn.execute("SELECT 1 FROM assets WHERE project_id=? AND lock_owner IS NOT NULL AND lock_expires_at>? LIMIT 1", (project_id, now_iso())).fetchone():
        raise HTTPException(409, "项目图片仍有有效编辑占用，请先关闭编辑页面并释放图片锁")
    if conn.execute("""SELECT 1 FROM pipeline_resource_leases l WHERE expires_at>? AND
        ((task_type='training' AND task_id IN (SELECT id FROM training_runs WHERE project_id=?)) OR
         (task_type='conversion' AND task_id IN (SELECT c.id FROM model_conversions c JOIN training_runs r
            ON r.id=c.training_run_id WHERE r.project_id=?))) LIMIT 1""", (now_iso(), project_id, project_id)).fetchone():
        raise HTTPException(409, "项目计算资源仍被占用，请等待任务完全停止后再删除")


def _ensure_no_foreign_links(conn, project_id: int) -> None:
    # Defensive checks for legacy/corrupt cross-project associations that would
    # otherwise be affected by ON DELETE CASCADE.
    if conn.execute("""SELECT 1 FROM annotations n JOIN labels l ON l.id=n.label_id
        JOIN assets a ON a.id=n.asset_id WHERE a.project_id<>l.project_id
        AND (a.project_id=? OR l.project_id=?) LIMIT 1""", (project_id, project_id)).fetchone():
        raise HTTPException(409, "项目存在跨项目标注引用，请管理员检查数据后再删除")
    if conn.execute("""SELECT 1 FROM work_block_assets m JOIN work_blocks b ON b.id=m.block_id
        JOIN assets a ON a.id=m.asset_id WHERE a.project_id<>b.project_id
        AND (a.project_id=? OR b.project_id=?) LIMIT 1""", (project_id, project_id)).fetchone():
        raise HTTPException(409, "项目存在跨项目任务块引用，请管理员检查数据后再删除")
    if conn.execute("""SELECT 1 FROM dataset_version_annotations n
        JOIN dataset_versions v ON v.id=n.version_id
        JOIN dataset_version_assets a ON a.id=n.version_asset_id
        JOIN dataset_versions av ON av.id=a.version_id
        JOIN dataset_version_labels l ON l.id=n.version_label_id
        JOIN dataset_versions lv ON lv.id=l.version_id
        WHERE (v.project_id<>av.project_id OR v.project_id<>lv.project_id)
        AND (v.project_id=? OR av.project_id=? OR lv.project_id=?) LIMIT 1""",
        (project_id, project_id, project_id)).fetchone():
        raise HTTPException(409, "项目存在跨项目数据版本引用，请管理员检查数据后再删除")
    if conn.execute("""SELECT 1 FROM job_asset_results r JOIN jobs j ON j.id=r.job_id
        JOIN assets a ON a.id=r.asset_id WHERE j.project_id<>a.project_id
        AND (j.project_id=? OR a.project_id=?) LIMIT 1""", (project_id, project_id)).fetchone():
        raise HTTPException(409, "项目存在跨项目 AI 结果引用，请管理员检查数据后再删除")


def _cleanup_files(items: list[dict], audit_id: int) -> dict:
    outcomes = []
    for item in items:
        try:
            path = _checked_path(item)
            live_thumbnail = re.fullmatch(r"([0-9a-f]{64})-\d+\.webp", item["name"]) if item["kind"] == "thumbnail" else None
            if live_thumbnail:
                # An independent upload may have reused this thumbnail hash
                # since the project-delete transaction committed.
                with db() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    if conn.execute("SELECT 1 FROM assets WHERE sha256=? LIMIT 1", (live_thumbnail[1],)).fetchone():
                        status = "retained"
                    else:
                        _remove_path(path, False)
                        status = "deleted"
            else:
                _remove_path(path, item["directory"])
                status = "deleted"
        except FileNotFoundError:
            status = "missing"
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            status = "pending"
            logger.warning("Project file cleanup pending: %s/%s", item["kind"], item["name"])
        outcomes.append({**item, "status": status})
    counts = {f"{status}_count": sum(item["status"] == status for item in outcomes)
              for status in ("deleted", "missing", "pending", "retained")}
    result = {"status": "pending" if counts["pending_count"] else "completed", **counts}
    try:
        with db() as conn:
            row = conn.execute("SELECT payload_json FROM audit_logs WHERE id=?", (audit_id,)).fetchone()
            payload = json.loads(row["payload_json"])
            payload["cleanup"] = {**result, "checked_at": now_iso()}
            payload["files"] = outcomes
            conn.execute("UPDATE audit_logs SET payload_json=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), audit_id))
    except (sqlite3.Error, TypeError, ValueError):
        # The deletion has committed. Never invite repeating a destructive write
        # because recording file cleanup failed; the pending manifest survives.
        logger.exception("Could not update project deletion cleanup audit %s", audit_id)
        result["status"] = "pending"
        result["audit_updated"] = False
    return result


@router.delete("/{project_id}")
def delete_project(project_id: int, payload: ProjectDelete, actor: Actor = Depends(get_actor)) -> dict:
    require_admin(actor)
    try:
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _account_row(conn, actor, admin=True)
            row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "项目不存在")
            project = dict(row)
            if payload.confirmation_name != project["name"]:
                raise HTTPException(422, "项目名称不匹配，请完整输入要删除的项目名称")
            _ensure_idle(conn, project_id)
            _ensure_no_foreign_links(conn, project_id)
            try:
                items = _owned_files(conn, project_id)
                _protect_shared_files(conn, project_id, items)
            except (OSError, ValueError, RuntimeError) as exc:
                raise HTTPException(409, "项目文件路径异常，请管理员检查存储后再删除") from exc
            counts = {}
            for label, sql in {
                "assets": "SELECT COUNT(*) FROM assets WHERE project_id=?",
                "annotations": "SELECT COUNT(*) FROM annotations n JOIN assets a ON a.id=n.asset_id WHERE a.project_id=?",
                "labels": "SELECT COUNT(*) FROM labels WHERE project_id=?",
                "work_blocks": "SELECT COUNT(*) FROM work_blocks WHERE project_id=?",
                "jobs": "SELECT COUNT(*) FROM jobs WHERE project_id=?",
                "versions": "SELECT COUNT(*) FROM dataset_versions WHERE project_id=?",
                "training_runs": "SELECT COUNT(*) FROM training_runs WHERE project_id=?",
                "conversions": "SELECT COUNT(*) FROM model_conversions c JOIN training_runs r ON r.id=c.training_run_id WHERE r.project_id=?",
            }.items():
                counts[label] = conn.execute(sql, (project_id,)).fetchone()[0]
            summary = {"name": project["name"], "counts": counts, "files": items,
                       "cleanup": {"status": "pending"}, "audit_history_preserved": True}
            audit_id = conn.execute("""INSERT INTO audit_logs(actor,action,resource_type,resource_id,
                project_id,payload_json,created_at) VALUES (?,'delete','project',?,?,?,?)""",
                (actor.id, str(project_id), project_id, json.dumps(summary, ensure_ascii=False), now_iso())).lastrowid
            conn.execute("""DELETE FROM pipeline_resource_leases WHERE
                (task_type='training' AND task_id IN (SELECT id FROM training_runs WHERE project_id=?)) OR
                (task_type='conversion' AND task_id IN (SELECT c.id FROM model_conversions c
                    JOIN training_runs r ON r.id=c.training_run_id WHERE r.project_id=?))""", (project_id, project_id))
            # RESTRICT references require this order; the remaining owned rows
            # cascade from versions and projects after their dependents are gone.
            conn.execute("DELETE FROM training_runs WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM dataset_version_annotations WHERE version_id IN (SELECT id FROM dataset_versions WHERE project_id=?)", (project_id,))
            conn.execute("DELETE FROM dataset_versions WHERE project_id=?", (project_id,))
            conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "项目仍有其他数据引用，删除未执行，请管理员检查关联记录") from exc
    cleanup = _cleanup_files(items, audit_id)
    return {"deleted": True, "project_id": project_id, "name": project["name"],
            "counts": counts, "cleanup": cleanup, "audit_id": audit_id}
