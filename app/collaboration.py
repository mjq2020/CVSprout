"""Shared write guards. Call inside BEGIN IMMEDIATE before changing an asset."""
from __future__ import annotations

import json
import sqlite3

from fastapi import HTTPException

from .auth import Actor
from .database import now_iso


def active_job_for_assets(conn: sqlite3.Connection, project_id: int, asset_ids: list[int], exclude: int | None = None):
    wanted = set(asset_ids)
    for row in conn.execute(
        "SELECT id, settings_json FROM jobs WHERE project_id = ? AND status IN ('queued', 'running')",
        (project_id,),
    ):
        if row["id"] == exclude:
            continue
        targets = json.loads(row["settings_json"]).get("asset_ids")
        if targets is None or wanted.intersection(targets):
            return row["id"]
    return None


def guard_asset(conn: sqlite3.Connection, asset_id: int, actor: Actor, *, editable: bool = True) -> dict:
    row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "图片不存在")
    asset = dict(row)
    if (asset["lock_owner"] and asset["lock_owner"] != actor.id
            and asset["lock_expires_at"] and asset["lock_expires_at"] > now_iso()):
        raise HTTPException(423, "图片正在由其他协作者编辑，请稍后重试")
    job = active_job_for_assets(conn, asset["project_id"], [asset_id])
    if job:
        raise HTTPException(409, f"图片正在由 AI 任务 #{job} 处理，请等待完成后校正")
    if editable and asset["review_status"] in {"submitted", "approved"}:
        raise HTTPException(409, "图片已提交或审核通过，请先重新打开再修改标注")
    return asset


def guard_job_targets(conn: sqlite3.Connection, project_id: int, asset_ids: list[int], *, exclude: int | None = None) -> None:
    if not asset_ids:
        raise HTTPException(409, "没有可处理的图片")
    conflict = active_job_for_assets(conn, project_id, asset_ids, exclude)
    if conflict:
        raise HTTPException(409, f"所选图片与 AI 任务 #{conflict} 重叠，请选择其他图片或等待任务结束")
    wanted = set(asset_ids)
    for asset in conn.execute(
        "SELECT id, lock_owner FROM assets WHERE project_id = ? AND lock_owner IS NOT NULL AND lock_expires_at > ?",
        (project_id, now_iso()),
    ):
        if asset["id"] in wanted:
            raise HTTPException(409, f"图片 #{asset['id']} 正由 {asset['lock_owner']} 编辑，请释放后再启动 AI 任务")
