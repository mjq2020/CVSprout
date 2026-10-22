from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import uuid
from datetime import UTC, datetime, timedelta

from .config import settings
from .database import all_rows, audit, db, now_iso, one
from .work_blocks import guard_block_ai, refresh_block
from .providers import get_provider
from .annotation_inference import (filter_predictions, inference_hints, prepare_inference,
                                   valid_prediction as _valid_prediction)


logger = logging.getLogger(__name__)


def _future_iso(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def claim_next_job(worker_id: str) -> int | None:
    """Atomically lease one queued job or recover a job whose worker lease expired."""
    now = now_iso()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""UPDATE jobs SET status='cancelled', finished_at=?, claimed_by=NULL,
            lease_expires_at=NULL, progress_message='已取消'
            WHERE cancel_requested=1 AND (status='queued' OR
                (status='running' AND (lease_expires_at IS NULL OR lease_expires_at<=?)))""", (now, now))
        row = conn.execute(
            """SELECT * FROM jobs
               WHERE cancel_requested = 0
                 AND (status = 'queued' OR (status = 'running' AND (lease_expires_at IS NULL OR lease_expires_at < ?)))
               ORDER BY created_at, id LIMIT 1""",
            (now,),
        ).fetchone()
        if not row:
            return None
        job = dict(row)
        if job["attempt"] > job["max_retries"]:
            conn.execute(
                "UPDATE jobs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                ("超过最大重试次数", now, job["id"]),
            )
            return None
        conn.execute(
            """UPDATE jobs SET status = 'running', claimed_by = ?, attempt = attempt + 1,
               lease_expires_at = ?, heartbeat_at = ?, started_at = COALESCE(started_at, ?),
               progress_message = '准备模型服务', error = NULL, finished_at = NULL
               WHERE id = ?""",
            (worker_id, _future_iso(settings.job_lease_seconds), now, now, job["id"]),
        )
        return int(job["id"])


def heartbeat(job_id: int, worker_id: str, message: str = "") -> bool:
    with db() as conn:
        now = now_iso()
        cursor = conn.execute(
            """UPDATE jobs SET lease_expires_at = ?, heartbeat_at = ?, progress_message = ?
               WHERE id = ? AND status = 'running' AND claimed_by = ? AND cancel_requested = 0""",
            (_future_iso(settings.job_lease_seconds), now, message, job_id, worker_id),
        )
        if cursor.rowcount == 1:
            conn.execute(
                """UPDATE workers SET last_seen_at = ?, status = 'busy', current_job_id = ?
                   WHERE id = ?""",
                (now, job_id, worker_id),
            )
        return cursor.rowcount == 1


async def run_annotation_job(job_id: int, worker_id: str = "embedded") -> None:
    job = one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not job:
        return
    provider = get_provider(job["model_id"])
    if not provider:
        _fail_or_retry(job_id, "模型不存在或已停用", worker_id)
        return

    try:
        config = json.loads(job["settings_json"])
        project = one("SELECT * FROM projects WHERE id=?", (job["project_id"],))
        asset_ids = config.get("asset_ids")
        params: tuple = (job["project_id"],)
        query = "SELECT * FROM assets WHERE project_id = ?"
        if not config.get("include_reviewed", False):
            query += " AND review_status NOT IN ('submitted', 'approved')"
        if asset_ids is not None:
            placeholders = ",".join("?" for _ in asset_ids)
            query += f" AND id IN ({placeholders})"
            params += tuple(asset_ids)
        query += " ORDER BY id"
        assets = all_rows(query, params)
        labels = all_rows(
            "SELECT * FROM labels WHERE project_id = ? ORDER BY id", (job["project_id"],)
        )
        hint_annotation = None
        if config.get("hint_annotation_id"):
            hint_annotation = one(
                """SELECT n.*, l.name AS label_name FROM annotations n
                   JOIN labels l ON l.id = n.label_id
                   WHERE n.id = ? AND n.deleted_at IS NULL""",
                (config["hint_annotation_id"],),
            )
        inference = prepare_inference(project, labels, config, job["prompt"], hint_annotation)

        with db() as conn:
            conn.execute("UPDATE jobs SET total = ? WHERE id = ?", (len(assets), job_id))

        completed_assets = {row["asset_id"]: row["created_count"] for row in all_rows("SELECT * FROM job_asset_results WHERE job_id=?", (job_id,))}
        created_total = sum(completed_assets.values())
        predicted_total = 0
        rejected_unknown = 0
        rejected_target = 0
        rejected_confidence = 0
        rejected_geometry = 0
        for index, asset in enumerate(assets, start=1):
            if asset["id"] in completed_assets:
                continue
            progress_prefix = f"正在处理 {index}/{len(assets)}"
            if not heartbeat(job_id, worker_id, f"{progress_prefix}：{asset['filename']}"):
                _cancel(job_id, worker_id)
                return
            hints = inference_hints(inference, asset, len(assets))
            predictions = await _predict_with_lease(
                provider,
                job_id,
                worker_id,
                settings.media_dir / asset["storage_name"],
                asset["width"],
                asset["height"],
                inference.prompt,
                inference.target_labels,
                hints,
                f"模型推理中 {index}/{len(assets)}：{asset['filename']}",
            )
            if not heartbeat(job_id, worker_id, f"正在校验 {index}/{len(assets)}：{asset['filename']}"):
                _cancel(job_id, worker_id)
                return
            accepted, counts = filter_predictions(inference, predictions, asset)
            predicted_total += counts["predicted"]
            rejected_unknown += counts["filtered_unknown_label"]
            rejected_target += counts["filtered_target_label"]
            rejected_confidence += counts["filtered_confidence"]
            rejected_geometry += counts["filtered_geometry"]

            # Replace and insert in one transaction so a provider failure never destroys good labels.
            with db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                ownership = conn.execute(
                    """SELECT 1 FROM jobs WHERE id = ? AND status = 'running'
                       AND claimed_by = ? AND cancel_requested = 0""",
                    (job_id, worker_id),
                ).fetchone()
                if not ownership:
                    return
                guard_block_ai(conn, job["project_id"], [asset["id"]],
                               assist_owner_key=config.get("assist_owner_key") if job["mode"] == "assist" else None)
                current = dict(conn.execute("SELECT * FROM assets WHERE id=?", (asset["id"],)).fetchone())
                if current["revision"] != asset["revision"] or (current["lock_owner"] and current["lock_expires_at"] > now_iso()):
                    raise RuntimeError("图片在推理期间被修改或锁定，已拒绝覆盖，请重试")
                if current["review_status"] in {"submitted", "approved"}:
                    if not config.get("include_reviewed"):
                        raise RuntimeError("图片已提交审核，已拒绝覆盖")
                    conn.execute("""INSERT INTO review_events (asset_id,from_status,to_status,actor,reason,created_at)
                        VALUES (?,?,'draft',?,'授权 AI 重新标注',?)""", (asset["id"],current["review_status"],f"model:{job['model_id']}",now_iso()))
                if project.get("task_type") == "classify" and conn.execute(
                    "SELECT 1 FROM annotations WHERE asset_id=? AND deleted_at IS NULL AND (source='manual' OR human_modified=1)", (asset["id"],)
                ).fetchone():
                    accepted = []
                if config.get("replace_existing_auto", True):
                    conn.execute(
                        """UPDATE annotations SET deleted_at = ?, updated_at = ?, updated_by = ?
                           WHERE asset_id = ? AND source IN ('auto', 'assist') AND human_modified = 0 AND deleted_at IS NULL""",
                        (now_iso(), now_iso(), f"model:{job['model_id']}", asset["id"]),
                    )
                if project.get("task_type") == "classify" and conn.execute("SELECT 1 FROM annotations WHERE asset_id=? AND deleted_at IS NULL", (asset["id"],)).fetchone():
                    accepted = []
                for prediction, label in accepted:
                    now = now_iso()
                    conn.execute(
                        """INSERT INTO annotations
                           (asset_id, label_id, shape_type, points_json, attributes_json, keypoints_json,
                            confidence, source, model_id, version, created_by, updated_by,
                            created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
                        (
                            asset["id"], label["id"], prediction.shape_type,
                            json.dumps(prediction.points),
                            json.dumps(prediction.attributes, ensure_ascii=False),
                            json.dumps(prediction.keypoints),
                            prediction.confidence,
                            "assist" if job["mode"] == "assist" else "auto",
                            job["model_id"], f"model:{job['model_id']}",
                            f"model:{job['model_id']}", now, now,
                        ),
                    )
                active_count = conn.execute(
                    "SELECT COUNT(*) FROM annotations WHERE asset_id = ? AND deleted_at IS NULL",
                    (asset["id"],),
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO job_asset_results (job_id,asset_id,created_count) VALUES (?,?,?)",
                    (job_id, asset["id"], len(accepted)),
                )
                conn.execute(
                    """UPDATE assets SET status = ?, review_status = 'draft', negative_confirmed = 0,
                       revision = revision + 1, updated_at = ? WHERE id = ?""",
                    ("labeled" if active_count else "unlabeled", now_iso(), asset["id"]),
                )
                refresh_block(conn, asset["id"])
                conn.execute(
                    """UPDATE jobs SET processed = ?, created_count = ?, progress_message = ?
                       WHERE id = ?""",
                    (index, created_total + len(accepted), f"已处理 {index}/{len(assets)}", job_id),
                )
            created_total += len(accepted)

        if created_total:
            completion_message = f"完成：新增 {created_total} 个标注"
        elif not predicted_total:
            completion_message = "完成：模型未检测到目标"
        else:
            completion_message = (
                f"完成：模型返回 {predicted_total} 个候选，但均未通过类别、"
                "置信度或几何校验"
            )
        with db() as conn:
            cursor = conn.execute(
                """UPDATE jobs SET status = 'completed', finished_at = ?, created_count = ?,
                   lease_expires_at = NULL, progress_message = ?
                   WHERE id = ? AND status = 'running' AND claimed_by = ? AND cancel_requested = 0""",
                (now_iso(), created_total, completion_message, job_id, worker_id),
            )
        if cursor.rowcount != 1:
            _cancel(job_id, worker_id)
            return
        _audit_job(
            f"model:{job['model_id']}", "complete", "annotation_job", job_id,
            job["project_id"], {
                "created_count": created_total,
                "asset_count": len(assets),
                "predicted_count": predicted_total,
                "rejected_unknown_label": rejected_unknown,
                "rejected_target_label": rejected_target,
                "rejected_confidence": rejected_confidence,
                "rejected_geometry": rejected_geometry,
            },
        )
    except Exception as exc:
        _fail_or_retry(job_id, str(exc)[:2000], worker_id)


def _fail_or_retry(job_id: int, error: str, worker_id: str | None = None) -> None:
    job = one(
        """SELECT attempt, max_retries, cancel_requested, project_id, model_id, claimed_by
           FROM jobs WHERE id = ?""",
        (job_id,),
    )
    if not job:
        return
    if worker_id is not None and job["claimed_by"] != worker_id:
        return
    if job["cancel_requested"]:
        _cancel(job_id, worker_id)
        return
    retry = job["attempt"] <= job["max_retries"]
    with db() as conn:
        query = (
            """UPDATE jobs SET status = ?, error = ?, claimed_by = NULL, lease_expires_at = NULL,
               finished_at = ?, progress_message = ? WHERE id = ?"""
        )
        params: tuple = (
            "queued" if retry else "failed", error,
            None if retry else now_iso(),
            f"失败，等待第 {job['attempt'] + 1} 次尝试" if retry else "失败",
            job_id,
        )
        if worker_id is not None:
            query += " AND claimed_by = ?"
            params += (worker_id,)
        cursor = conn.execute(query, params)
        if cursor.rowcount != 1:
            return
    _audit_job(
        f"model:{job['model_id']}", "retry" if retry else "fail", "annotation_job", job_id,
        job["project_id"], {"error": error, "attempt": job["attempt"]},
    )


def _cancel(job_id: int, worker_id: str | None = None) -> None:
    with db() as conn:
        query = """UPDATE jobs SET status = 'cancelled', finished_at = ?, claimed_by = NULL,
                   lease_expires_at = NULL, progress_message = '已取消'
                   WHERE id = ? AND cancel_requested = 1"""
        params: tuple = (now_iso(), job_id)
        if worker_id is not None:
            query += " AND claimed_by = ?"
            params += (worker_id,)
        conn.execute(query, params)


def _audit_job(
    actor: str,
    action: str,
    resource_type: str,
    resource_id: int,
    project_id: int,
    payload: dict,
) -> None:
    try:
        audit(actor, action, resource_type, resource_id, project_id, payload)
    except Exception:
        logger.exception("Failed to write audit event for annotation job %s", resource_id)


async def _predict_with_lease(
    provider,
    job_id: int,
    worker_id: str,
    image_path,
    width: int,
    height: int,
    prompt: str,
    labels: list[str],
    hints: dict | None,
    progress_message: str,
):
    stopped = asyncio.Event()

    async def renew() -> None:
        interval = max(1.0, settings.job_lease_seconds / 3)
        while not stopped.is_set():
            try:
                await asyncio.wait_for(stopped.wait(), interval)
            except TimeoutError:
                if not heartbeat(job_id, worker_id, progress_message):
                    return

    renew_task = asyncio.create_task(renew())
    try:
        from .training import ResourceBusy
        while True:
            try:
                return await provider.predict(image_path, width, height, prompt, labels, hints)
            except ResourceBusy:
                if not heartbeat(job_id, worker_id, "等待 GPU：另一任务正在使用计算资源"):
                    return []
                await asyncio.sleep(max(0.5, settings.worker_poll_seconds))
    finally:
        stopped.set()
        await renew_task




class JobWorker:
    def __init__(self, worker_id: str | None = None) -> None:
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._stopping = asyncio.Event()

    async def run_forever(self) -> None:
        started = now_iso()
        self._touch("idle", None, started)
        try:
            while not self._stopping.is_set():
                job_id = claim_next_job(self.worker_id)
                if job_id is None:
                    self._touch("idle", None, started)
                    try:
                        await asyncio.wait_for(self._stopping.wait(), settings.worker_poll_seconds)
                    except TimeoutError:
                        pass
                    continue
                self._touch("busy", job_id, started)
                await run_annotation_job(job_id, self.worker_id)
                self._touch("idle", None, started)
        finally:
            self._touch("offline", None, started)

    def _touch(self, status: str, current_job_id: int | None, started_at: str) -> None:
        with db() as conn:
            conn.execute(
                """INSERT INTO workers
                   (id, started_at, last_seen_at, status, current_job_id, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET last_seen_at = excluded.last_seen_at,
                     status = excluded.status, current_job_id = excluded.current_job_id""",
                (
                    self.worker_id, started_at, now_iso(), status, current_job_id,
                    json.dumps(
                        {
                            "worker_type": "annotation",
                            "hostname": socket.gethostname(),
                            "pid": os.getpid(),
                        }
                    ),
                ),
            )

    def stop(self) -> None:
        self._stopping.set()


async def worker_main() -> None:
    from .database import init_db
    from .providers import seed_builtin_models

    init_db()
    seed_builtin_models()
    await JobWorker().run_forever()


if __name__ == "__main__":
    asyncio.run(worker_main())
