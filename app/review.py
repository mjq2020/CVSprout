"""Preview and apply review actions without hiding per-image conflicts."""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .auth import Actor, get_actor, password_change_required
from .collaboration import guard_asset
from .database import db, now_iso, one
from .work_blocks import refresh_block

router = APIRouter(prefix="/api/projects/{project_id}/review", tags=["review"])


class ReviewTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: int = Field(gt=0, le=2**63 - 1, strict=True)
    expected_revision: int = Field(ge=0, le=2**63 - 1, strict=True)


class BatchReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["submit", "approve", "reject"]
    assets: list[ReviewTarget] = Field(min_length=1, max_length=500)
    reason: str = Field(default="", max_length=2000)
    source_job_id: int | None = Field(default=None, gt=0, le=2**63 - 1, strict=True)
    source_job_generation: int | None = Field(default=None, ge=0, le=2**63 - 1, strict=True)

    @model_validator(mode="after")
    def consistent_revisions(self):
        if (self.source_job_id is None) != (self.source_job_generation is None):
            raise ValueError("按 AI 任务提交时，需要同时提供任务编号和任务批次，请刷新任务后重试")
        if self.source_job_id is not None and self.action != "submit":
            raise ValueError("AI 任务范围仅用于提交审核，请在待审核图片中通过或退回")
        revisions: dict[int, int] = {}
        for item in self.assets:
            if item.asset_id in revisions and revisions[item.asset_id] != item.expected_revision:
                raise ValueError("同一图片不能包含不同版本号，请刷新后重新选择")
            revisions[item.asset_id] = item.expected_revision
        self.reason = self.reason.strip()
        return self


TRANSITIONS = {
    "submit": ({"draft", "in_progress", "rejected"}, "submitted"),
    "approve": ({"submitted"}, "approved"),
    "reject": ({"submitted"}, "rejected"),
}
FINISHED_JOB_STATUSES = {"completed", "failed", "cancelled"}


def _source_job_issue(conn, project_id: int, target: ReviewTarget, payload: BatchReview) -> dict | None:
    if payload.source_job_id is None:
        return None
    job = conn.execute("SELECT * FROM jobs WHERE id=? AND project_id=?", (payload.source_job_id, project_id)).fetchone()
    if job is None:
        return {"status": "not_found", "code": "source_job_not_found", "message": "任务不存在或不属于当前项目"}
    if job["review_generation"] != payload.source_job_generation:
        return {"status": "conflict", "code": "source_job_changed", "message": "任务已被重试，请重新打开任务的提交审核入口"}
    if job["status"] not in FINISHED_JOB_STATUSES:
        return {"status": "conflict", "code": "source_job_active", "message": "任务尚未结束，请等待本次任务结束后再提交审核"}
    if not conn.execute("SELECT 1 FROM job_asset_results WHERE job_id=? AND asset_id=?", (payload.source_job_id, target.asset_id)).fetchone():
        return {"status": "conflict", "code": "source_job_asset_missing", "message": "没有本次任务处理此图片的保存记录，请重新检查任务范围"}
    return None


def _item(project_id: int, target: ReviewTarget, payload: BatchReview, actor: Actor, preview: bool) -> dict:
    result = {"asset_id": target.asset_id, "expected_revision": target.expected_revision}
    # An independent transaction means one stale image does not block the rest.
    with db() as conn:
        conn.execute("BEGIN" if preview else "BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM assets WHERE id=? AND project_id=?", (target.asset_id, project_id)).fetchone()
        if row is None:
            return {**result, "status": "not_found", "code": "not_in_project", "message": "图片不存在或不属于当前项目"}
        asset = dict(row)
        result.update(filename=asset["filename"], review_status=asset["review_status"], current_revision=asset["revision"])
        if payload.action in {"approve", "reject"} and actor.role not in {"admin", "reviewer"}:
            return {**result, "status": "forbidden", "code": "reviewer_required", "message": "需要审核员或管理员处理；当前账号可提交审核"}
        if actor.role not in {"admin", "reviewer", "annotator"}:
            return {**result, "status": "forbidden", "code": "role_not_allowed", "message": "当前账号没有操作权限"}
        source_issue = _source_job_issue(conn, project_id, target, payload)
        if source_issue:
            return {**result, **source_issue}
        if target.expected_revision != asset["revision"]:
            return {**result, "status": "conflict", "code": "revision_changed", "message": "图片已变化，请刷新并重新检查后重试"}
        try:
            guard_asset(conn, target.asset_id, actor, editable=False,
                        action="review" if payload.action in {"approve", "reject"} else "edit")
        except HTTPException as error:
            locked = error.status_code == 423
            return {**result, "status": "forbidden" if error.status_code == 403 else "conflict",
                    "code": "locked" if locked else "permission_denied" if error.status_code == 403 else "busy",
                    "message": "图片正由其他成员或浏览器页面编辑，请稍后处理" if locked else str(error.detail)}
        allowed, new_status = TRANSITIONS[payload.action]
        if asset["review_status"] not in allowed:
            message = "只处理草稿、标注中或已退回的图片" if payload.action == "submit" else "只处理已提交的待审核图片；AI 初标不能直接通过"
            return {**result, "status": "skipped", "code": "state_not_allowed", "message": message}
        count = conn.execute("SELECT COUNT(*) FROM annotations WHERE asset_id=? AND deleted_at IS NULL", (target.asset_id,)).fetchone()[0]
        if payload.action in {"submit", "approve"} and not count and not asset["negative_confirmed"]:
            return {**result, "status": "skipped", "code": "no_annotations", "message": "请先完成标注，或明确确认图片无目标"}
        if preview:
            return {**result, "status": "eligible", "code": "ready", "message": "可处理", "target_status": new_status}
        now = now_iso()
        # guard_asset rejects another member's live lock. Only our own lock is released.
        audit_details = {"from": asset["review_status"], "to": new_status, "reason": payload.reason, "batch": True}
        if payload.source_job_id is not None:
            audit_details.update(source_job_id=payload.source_job_id, source_job_generation=payload.source_job_generation)
        conn.execute(
            """UPDATE assets SET review_status=?, review_reason=?, revision=revision+1, updated_at=?,
               lock_owner=CASE WHEN lock_owner=? THEN NULL ELSE lock_owner END,
               lock_expires_at=CASE WHEN lock_owner=? THEN NULL ELSE lock_expires_at END,
               lock_token=CASE WHEN lock_owner=? THEN NULL ELSE lock_token END
               WHERE id=? AND project_id=? AND revision=?""",
            (new_status, payload.reason, now, actor.id, actor.id, actor.id, target.asset_id, project_id, target.expected_revision),
        )
        conn.execute(
            """INSERT INTO review_events(asset_id,from_status,to_status,actor,reason,created_at)
               VALUES (?,?,?,?,?,?)""",
            (target.asset_id, asset["review_status"], new_status, actor.id, payload.reason, now),
        )
        conn.execute(
            """INSERT INTO audit_logs(actor,action,resource_type,resource_id,project_id,payload_json,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (actor.id, payload.action, "asset_review", str(target.asset_id), project_id,
             json.dumps(audit_details, ensure_ascii=False), now),
        )
        updated = dict(conn.execute("SELECT * FROM assets WHERE id=?", (target.asset_id,)).fetchone())
        refresh_block(conn, target.asset_id, actor)
        return {**result, "status": "succeeded", "code": "updated", "message": {"submit": "已提交审核", "approve": "审核已通过", "reject": "已退回修改"}[payload.action], "asset": updated}


def _run(project_id: int, payload: BatchReview, actor: Actor, *, preview: bool) -> dict:
    if actor.must_change_password:
        raise password_change_required()
    if not one("SELECT id FROM projects WHERE id=?", (project_id,)):
        raise HTTPException(404, "项目不存在")
    if not preview and payload.action == "reject" and not payload.reason:
        raise HTTPException(422, "退回修改时请填写原因，方便标注员按要求修正")
    unique = {item.asset_id: item for item in payload.assets}
    items = [_item(project_id, target, payload, actor, preview) for target in unique.values()]
    counts = {status: sum(item["status"] == status for item in items) for status in ("eligible", "succeeded", "skipped", "conflict", "forbidden", "not_found")}
    return {"project_id": project_id, "action": payload.action, "preview": preview,
            **({"source_job_id": payload.source_job_id, "source_job_generation": payload.source_job_generation}
               if payload.source_job_id is not None else {}),
            "total": len(items), "duplicate_count": len(payload.assets) - len(items), "counts": counts, "items": items}


@router.get("/jobs/{job_id}")
def job_review_scope(project_id: int, job_id: int, actor: Actor = Depends(get_actor)) -> dict:
    """Only persisted per-image job results establish a task's review scope.

    Frozen requested IDs describe intent, not completed work. In particular,
    legacy jobs with no result rows cannot safely infer a project-wide scope.
    """
    if actor.must_change_password:
        raise password_change_required()
    if actor.role not in {"admin", "reviewer", "annotator"}:
        raise HTTPException(403, "当前账号没有提交审核权限")
    with db() as conn:
        conn.execute("BEGIN")
        job = conn.execute("SELECT * FROM jobs WHERE id=? AND project_id=?", (job_id, project_id)).fetchone()
        if job is None:
            raise HTTPException(404, "任务不存在或不属于当前项目")
        items = [dict(row) for row in conn.execute("""SELECT a.id AS asset_id,a.revision AS expected_revision,
            a.filename,a.review_status,r.created_count,COUNT(n.id) AS annotation_count
            FROM job_asset_results r JOIN assets a ON a.id=r.asset_id
            LEFT JOIN annotations n ON n.asset_id=a.id AND n.deleted_at IS NULL
            WHERE r.job_id=? AND a.project_id=? GROUP BY a.id ORDER BY a.id""", (job_id, project_id))]
        counts = {
            "processed": len(items),
            "with_annotations": sum(item["annotation_count"] > 0 for item in items),
            "pending_submission": sum(item["annotation_count"] > 0 and item["review_status"] in TRANSITIONS["submit"][0] for item in items),
            "submitted": sum(item["review_status"] == "submitted" for item in items),
            "approved": sum(item["review_status"] == "approved" for item in items),
            "without_annotations": sum(item["annotation_count"] == 0 for item in items),
        }
        finished = job["status"] in FINISHED_JOB_STATUSES
        if not finished:
            code, reason = "job_active", "任务尚未结束，请等待本次任务结束后再提交审核"
        elif not items:
            code, reason = "scope_unavailable", "此任务没有可确认的已保存图片记录，请在图片列表检查后选择提交"
        elif not counts["pending_submission"]:
            code, reason = "no_submittable_assets", "此任务当前没有可提交的图片：图片可能已提交、已通过审核，或尚无有效标注"
        else:
            code, reason = "ready", "可检查此任务已处理图片的当前标注，并提交审核"
        return {"project_id": project_id, "job_id": job_id, "job_status": job["status"],
                "source_job_id": job_id, "source_job_generation": job["review_generation"],
                "scope_available": bool(items), "scope_code": code, "scope_reason": reason,
                "can_submit": finished and counts["pending_submission"] > 0,
                "total": len(items), "asset_ids": [item["asset_id"] for item in items], "items": items, "counts": counts}


@router.post("/preview")
def preview_batch_review(project_id: int, payload: BatchReview, actor: Actor = Depends(get_actor)) -> dict:
    return _run(project_id, payload, actor, preview=True)


@router.post("/batch")
def apply_batch_review(project_id: int, payload: BatchReview, actor: Actor = Depends(get_actor)) -> dict:
    return _run(project_id, payload, actor, preview=False)
