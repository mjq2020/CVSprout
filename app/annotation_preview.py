"""Bounded, read-only AI previews; no jobs, annotations or editing leases are written."""
from __future__ import annotations

import asyncio
import logging
import threading
import time

import httpx
from fastapi import HTTPException

from .annotation_inference import empty_counts, filter_predictions, inference_hints, prepare_inference
from .config import settings
from .database import all_rows, one
from .providers.execution import local_inference_busy

logger = logging.getLogger(__name__)
_PREVIEW_SLOT = threading.Lock()
MAX_PREVIEW_SECONDS = 180
LABEL_PALETTE = ["#22c55e", "#38bdf8", "#f97316", "#a78bfa", "#f43f5e", "#facc15"]


def _finish_preview(task: asyncio.Task) -> None:
    # A cancelled browser request or timeout must not release capacity while a
    # local inference thread is still running. Shielding leaves this task alive.
    try:
        if not task.cancelled():
            task.exception()
    finally:
        _PREVIEW_SLOT.release()


async def run_preview(project: dict, model: dict, provider, labels: list[dict], config: dict,
                      prompt: str, pending_labels: list[str]) -> dict:
    if model.get("provider_type", "").startswith("local_") and local_inference_busy():
        raise HTTPException(429, "本地 AI 模型正在处理其他图片，请稍候再预览。", headers={"Retry-After": "3"})
    if not _PREVIEW_SLOT.acquire(blocking=False):
        raise HTTPException(429, "已有 AI 预览正在运行，请稍候再试。", headers={"Retry-After": "3"})
    timeout = min(MAX_PREVIEW_SECONDS, max(1, model.get("timeout_seconds", 180)))
    stopped = asyncio.Event()
    task = asyncio.create_task(_preview_images(project, model, provider, labels, config, prompt, pending_labels, stopped))
    task.add_done_callback(_finish_preview)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except (TimeoutError, httpx.TimeoutException) as exc:
        stopped.set()
        raise HTTPException(504, "AI 预览超时，未保存任何标注。请稍后重试，或选择更少的图片。") from exc
    except asyncio.CancelledError:
        stopped.set()
        raise
    except HTTPException:
        raise
    except Exception as exc:
        from .training import ResourceBusy
        if isinstance(exc, ResourceBusy):
            raise HTTPException(409, str(exc), headers={"Retry-After": "3"}) from exc
        logger.warning("AI preview failed for model %s (%s)", model.get("id"), type(exc).__name__)
        raise HTTPException(502, "AI 预览失败，未保存任何标注。请检查模型服务后重试。") from exc


async def _preview_images(project: dict, model: dict, provider, labels: list[dict], config: dict,
                          prompt: str, pending_labels: list[str], stopped: asyncio.Event) -> dict:
    started = time.monotonic()
    asset_ids = config["asset_ids"]
    placeholders = ",".join("?" for _ in asset_ids)
    rows = all_rows(f"SELECT * FROM assets WHERE project_id=? AND id IN ({placeholders})",
                    (project["id"], *asset_ids))
    asset_map = {asset["id"]: asset for asset in rows}
    if len(asset_map) != len(asset_ids):
        raise HTTPException(409, "预览图片已变化，请重新选择。")
    # Match the eventual label colors without creating project labels.
    preview_labels = list(labels)
    for index, name in enumerate(pending_labels, start=len(labels)):
        preview_labels.append({"id": None, "name": name, "color": LABEL_PALETTE[index % len(LABEL_PALETTE)],
                               "attributes_schema_json": "{}"})
    hint = None
    if config.get("hint_annotation_id"):
        hint = one("""SELECT n.*, l.name AS label_name FROM annotations n
                      JOIN labels l ON l.id=n.label_id WHERE n.id=? AND n.deleted_at IS NULL""",
                   (config["hint_annotation_id"],))
        if not hint or hint["asset_id"] not in asset_ids:
            raise HTTPException(409, "提示标注已变化，请重新选择。")
    context = prepare_inference(project, preview_labels, config, prompt, hint)
    totals = empty_counts()
    items = []
    warnings = []
    if pending_labels:
        warnings.append("以下类别将在开始批量标注时创建：" + "、".join(pending_labels))
    for asset_id in asset_ids:
        if stopped.is_set():
            raise asyncio.CancelledError
        asset_started = time.monotonic()
        asset = asset_map[asset_id]
        if not config.get("include_reviewed") and asset["review_status"] in {"submitted", "approved"}:
            raise HTTPException(409, "预览图片已提交审核，请重新选择草稿图片。")
        path = settings.media_dir / asset["storage_name"]
        if not path.is_file():
            raise HTTPException(409, "预览图片文件不可用，请重新导入图片。")
        predictions = await provider.predict(path, asset["width"], asset["height"], context.prompt,
                                             context.target_labels, inference_hints(context, asset, len(asset_ids)))
        if stopped.is_set():
            raise asyncio.CancelledError
        accepted, counts = filter_predictions(context, predictions, asset)
        item_warnings = []
        if any(prediction.confidence is None for prediction, _label in accepted):
            item_warnings.append("部分结果未提供置信度，无法按置信度筛选，请人工检查。")
        if project["task_type"] == "classify" and one(
                """SELECT 1 FROM annotations WHERE asset_id=? AND deleted_at IS NULL
                   AND (source='manual' OR human_modified=1)""", (asset_id,)):
            item_warnings.append("此图片已有人工分类；批量标注会保留人工结果，预览仅展示 AI 判断。")
        annotations = [{"label_id": label["id"], "label_name": label["name"], "label_color": label["color"],
                        "shape_type": prediction.shape_type, "points": prediction.points,
                        "keypoints": prediction.keypoints, "confidence": prediction.confidence,
                        "attributes": prediction.attributes} for prediction, label in accepted]
        for name, value in counts.items():
            totals[name] += value
        items.append({"asset_id": asset_id, "filename": asset["filename"], "width": asset["width"],
                      "height": asset["height"], "asset_revision": asset["revision"],
                      "image_url": f"/api/assets/{asset_id}/image", "annotations": annotations,
                      "counts": counts, "warnings": item_warnings,
                      "elapsed_ms": round((time.monotonic() - asset_started) * 1000)})
    return {"project_id": project["id"], "model_id": model.get("id", provider.info.id),
            "task_type": project["task_type"], "keypoint_schema": project.get("keypoint_schema", {}),
            "prompt": prompt, "target_labels": context.target_labels,
            "confidence_threshold": config["confidence_threshold"], "preview_only": True,
            "pending_labels": pending_labels, "items": items, "totals": totals, "warnings": warnings,
            "elapsed_ms": round((time.monotonic() - started) * 1000)}
