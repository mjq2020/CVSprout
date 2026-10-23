from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
from importlib.util import find_spec
from typing import Any

from ..config import settings
from ..database import all_rows, db, now_iso, one
from ..annotation_models import preparation, sam2_model_path
from .base import AnnotationProvider, ModelInfo
from .demo import DemoProvider
from .local import (
    LocalGroundingDinoProvider,
    LocalSam2Provider,
    LocalSam3Provider,
    local_runtime_status,
    local_sam2_runtime_status,
    local_sam3_runtime_status,
)
from .qwen import QwenProvider
from .remote import RemoteVisionProvider
from .yolo import LocalYoloProvider


def supported_tasks(provider_type: str, config: dict) -> list[str]:
    return {
        "local_grounding_dino": ["detect"], "local_sam2": ["segment"], "local_sam3": ["detect", "segment"],
        "remote_detection": ["detect"], "remote_segmentation": ["segment"],
        "interactive_segmentation": ["segment"], "remote_classification": ["classify"],
        "remote_keypoints": ["pose"], "local_yolo": [config.get("task", "detect")],
        "demo": ["detect", "segment"],
    }.get(provider_type, ["detect", "segment", "classify", "pose"])


BUILTINS = [
    {
        "id": "qwen3.5-0.8b", "name": "Qwen3.5-0.8B（本地）", "family": "视觉语言模型",
        "provider_type": "local_qwen", "description": "较小规模的本地图像语义标注模型，支持按需下载；结果需人工检查。",
        "endpoint": "local://transformers", "model_name": str(settings.models_dir / "qwen3.5-0.8b"),
        "api_key_env": "", "capabilities": ["检测框", "多边形", "场景分类", "关键点"],
        "modes": ["auto", "assist"], "badge": "按需下载",
    },
    {
        "id": "qwen3.5-35b-a3b",
        "name": "Qwen3.5-35B-A3B",
        "family": "视觉语言模型",
        "provider_type": "openai_vision",
        "description": "自然语言理解、框定位、属性和关系判断，适合复杂业务规则。",
        "endpoint": settings.qwen_base_url,
        "model_name": settings.qwen_model,
        "api_key_env": "QWEN_API_KEY",
        "capabilities": ["检测框", "属性", "关系", "场景分类", "视频理解"],
        "modes": ["auto", "assist"],
        "badge": "推荐语义",
    },
    {
        "id": "grounding-dino",
        "name": "Grounding DINO Tiny（本地）",
        "family": "开放词汇检测",
        "provider_type": "local_grounding_dino",
        "description": "已下载的本地开放词汇检测模型，根据项目类别批量产生目标框，无需配置远程端点。",
        "endpoint": "local://transformers",
        "model_name": str(settings.models_dir / "grounding-dino-tiny"),
        "api_key_env": "",
        "capabilities": ["检测框", "开放词汇"],
        "modes": ["auto"],
        "badge": "本地可用",
        "force_sync": True,
    },
    {
        "id": "sam3",
        "name": "SAM 3（本地）",
        "family": "概念分割",
        "provider_type": "local_sam3",
        "description": "使用本地 SAM 3 权重，按文本概念查找实例；检测项目输出矩形框，分割项目输出多边形。",
        "endpoint": "local://sam3",
        "model_name": str(settings.sam3_checkpoint),
        "api_key_env": "",
        "capabilities": ["检测框", "实例分割", "多边形", "开放词汇"],
        "modes": ["auto"],
        "badge": "推荐分割",
        "force_sync": True,
    },
    {
        "id": "sam2-assist",
        "name": "SAM 2.1 Hiera Tiny（本地）",
        "family": "交互式分割",
        "provider_type": "local_sam2",
        "description": "已下载的本地半自动分割模型，接收正负点或粗框并返回精细多边形轮廓。",
        "endpoint": "local://transformers",
        "model_name": str(settings.models_dir / "sam2.1-hiera-tiny"),
        "api_key_env": "",
        "capabilities": ["多边形", "正负点提示", "框提示"],
        "modes": ["assist"],
        "badge": "本地可用",
        "force_sync": True,
    },
]


def seed_builtin_models() -> None:
    now = now_iso()
    records = [dict(item) for item in BUILTINS]
    for item in records:
        if item["id"] == "sam2-assist":
            item["model_name"] = str(sam2_model_path())
            item["description"] = "本地 SAM 2.1 Tiny 半自动分割；自行放置权重后，使用正负点或粗框生成轮廓。"
        elif item["id"] == "sam3":
            item["model_name"] = str(settings.sam3_checkpoint)
        elif item["id"] == "grounding-dino":
            item["model_name"] = str(settings.models_dir / "grounding-dino-tiny")
        elif item["id"].startswith("qwen3.5-"):
            remote = item["id"] == "qwen3.5-35b-a3b" and settings.qwen_base_url
            item.update(provider_type="openai_vision" if remote else "local_qwen",
                        endpoint=settings.qwen_base_url if remote else "local://transformers",
                        model_name=settings.qwen_model if remote else str(settings.models_dir / item["id"]),
                        api_key_env="QWEN_API_KEY" if remote else "")
    from ..training import _model_candidates, _declared_model_task
    for path in _model_candidates():
        task = _declared_model_task(path.name)
        if task not in {"detect", "segment", "classify", "pose"}:
            continue
        records.append({"id": "local-yolo-" + hashlib.sha256(str(path).encode()).hexdigest()[:12],
                        "name": path.stem + "（本地 AI）", "family": "Ultralytics",
                        "provider_type": "local_yolo", "description": "使用本地权重预标注；类别名称及关键点顺序需与项目一致。",
                        "endpoint": "local://ultralytics", "model_name": str(path), "api_key_env": "",
                        "capabilities": [{"detect":"检测框", "segment":"实例分割", "classify":"图片分类", "pose":"关键点"}[task]],
                        "modes":["auto"], "badge":"本地 AI", "task":task})
    if settings.enable_demo_model:
        records.append(
            {
                "id": "demo",
                "name": "开发演示模型",
                "family": "Demo",
                "provider_type": "demo",
                "description": "只用于自动化测试，不可生成生产数据。",
                "endpoint": "internal://demo",
                "model_name": "demo",
                "api_key_env": "",
                "capabilities": ["检测框"],
                "modes": ["auto", "assist"],
                "badge": "仅测试",
            }
        )
    with db() as conn:
        for item in records:
            conn.execute(
                """INSERT OR IGNORE INTO model_services
                   (id, name, family, provider_type, description, endpoint, model_name,
                    api_key_env, capabilities_json, modes_json, config_json, timeout_seconds,
                    enabled, builtin, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 180, 1, 1, ?, ?)""",
                (
                    item["id"], item["name"], item["family"], item["provider_type"],
                    item["description"], item["endpoint"], item["model_name"],
                    item["api_key_env"], json.dumps(item["capabilities"], ensure_ascii=False),
                    json.dumps(item["modes"]), json.dumps({"badge": item["badge"], **({"task":item["task"]} if "task" in item else {})}), now, now,
                ),
            )
            if item.get("force_sync"):
                conn.execute(
                    """UPDATE model_services SET name = ?, family = ?, provider_type = ?,
                       description = ?, endpoint = ?, model_name = ?, api_key_env = ?,
                       capabilities_json = ?, modes_json = ?,
                       builtin = 1, updated_at = ? WHERE id = ?""",
                    (
                        item["name"], item["family"], item["provider_type"],
                        item["description"], item["endpoint"], item["model_name"],
                        item["api_key_env"],
                        json.dumps(item["capabilities"], ensure_ascii=False),
                        json.dumps(item["modes"]),
                        now, item["id"],
                    ),
                )
            elif item["id"].startswith("qwen3.5-"):
                # Preserve administrator-configured remote endpoints. Only the
                # empty built-in placeholder or an existing local entry migrate.
                conn.execute("""UPDATE model_services SET provider_type=?,endpoint=?,model_name=?,api_key_env=?,updated_at=?
                    WHERE id=? AND builtin=1 AND (provider_type='local_qwen' OR
                    (provider_type='openai_vision' AND endpoint='' AND model_name='Qwen/Qwen3.5-35B-A3B'))""",
                    (item["provider_type"], item["endpoint"], item["model_name"], item["api_key_env"], now, item["id"]))


def _row_to_public(row: dict[str, Any]) -> dict[str, Any]:
    row = _runtime_row(row)
    config = json.loads(row["config_json"] or "{}")
    configured = bool(row["enabled"] and row["endpoint"])
    availability_error = ""
    if row["provider_type"] == "local_qwen":
        from .qwen_local import local_qwen_runtime_status
        configured, availability_error = local_qwen_runtime_status(row["model_name"])
        configured = bool(row["enabled"] and configured)
    elif row["provider_type"] == "local_sam2":
        configured, availability_error = local_sam2_runtime_status(row["model_name"])
        configured = bool(row["enabled"] and configured)
    elif row["provider_type"] == "local_grounding_dino":
        configured, availability_error = local_runtime_status(row["model_name"])
        configured = bool(row["enabled"] and configured)
    elif row["provider_type"] == "local_sam3":
        configured, availability_error = local_sam3_runtime_status(
            row["model_name"], str(settings.sam3_source_dir)
        )
        configured = bool(row["enabled"] and configured)
    elif row["provider_type"] == "local_yolo":
        configured = bool(row["enabled"] and Path(row["model_name"]).is_file() and (settings.worker_url or find_spec("ultralytics")))
        availability_error = "" if configured else "本地权重或 ultralytics 不可用，或服务已停用"
    return {
        "id": row["id"],
        "name": row["name"],
        "family": row["family"],
        "provider_type": row["provider_type"],
        "description": row["description"],
        "endpoint": row["endpoint"],
        "model_name": row["model_name"],
        "api_key_env": row["api_key_env"],
        "capabilities": json.loads(row["capabilities_json"] or "[]"),
        "modes": json.loads(row["modes_json"] or "[]"),
        "configured": configured,
        "availability_error": availability_error,
        "enabled": bool(row["enabled"]),
        "builtin": bool(row["builtin"]),
        "timeout_seconds": row["timeout_seconds"],
        "last_health_status": row["last_health_status"],
        "last_health_error": row["last_health_error"],
        "last_health_at": row["last_health_at"],
        "badge": config.get("badge", "自定义"),
        "config": config,
        "tasks": supported_tasks(row["provider_type"], config),
        "preparation": preparation(row),
    }


def _runtime_row(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    if row["builtin"] and row["id"] == "sam2-assist" and row["provider_type"] == "local_sam2":
        row["model_name"] = str(sam2_model_path())
    return row


def list_models() -> list[dict[str, Any]]:
    return [_row_to_public(row) for row in all_rows("SELECT * FROM model_services ORDER BY builtin DESC, name")]


def get_model(model_id: str) -> dict[str, Any] | None:
    row = one("SELECT * FROM model_services WHERE id = ?", (model_id,))
    return _row_to_public(row) if row else None


def get_provider(model_id: str) -> AnnotationProvider | None:
    row = one("SELECT * FROM model_services WHERE id = ? AND enabled = 1", (model_id,))
    if not row:
        return None
    row = _runtime_row(row)
    capabilities = json.loads(row["capabilities_json"] or "[]")
    modes = json.loads(row["modes_json"] or "[]")
    config = json.loads(row["config_json"] or "{}")
    configured = bool(row["endpoint"])
    if row["provider_type"] == "local_qwen":
        from .qwen_local import local_qwen_runtime_status
        configured, _ = local_qwen_runtime_status(row["model_name"])
    elif row["provider_type"] == "local_sam2":
        configured, _ = local_sam2_runtime_status(row["model_name"])
    elif row["provider_type"] == "local_grounding_dino":
        configured, _ = local_runtime_status(row["model_name"])
    elif row["provider_type"] == "local_sam3":
        configured, _ = local_sam3_runtime_status(
            row["model_name"], str(settings.sam3_source_dir)
        )
    elif row["provider_type"] == "local_yolo":
        configured = bool(Path(row["model_name"]).is_file() and (settings.worker_url or find_spec("ultralytics")))
    info = ModelInfo(
        row["id"], row["name"], row["family"], row["description"], capabilities,
        modes, configured, config.get("badge", "自定义"),
    )
    api_key = os.getenv(row["api_key_env"], "") if row["api_key_env"] else ""
    if settings.worker_url and row["provider_type"].startswith("local_"):
        from ..worker_client import WorkerProvider
        return WorkerProvider(info)
    if row["provider_type"] == "demo":
        return DemoProvider()
    if row["provider_type"] == "local_qwen":
        from .qwen_local import LocalQwenProvider
        return LocalQwenProvider(info, row["model_name"], settings.model_device)
    if row["provider_type"] == "local_yolo":
        return LocalYoloProvider(info, row["model_name"], config.get("task", "detect"), settings.model_device)
    if row["provider_type"] == "local_grounding_dino":
        return LocalGroundingDinoProvider(info, row["model_name"], settings.model_device)
    if row["provider_type"] == "local_sam2":
        return LocalSam2Provider(info, row["model_name"], settings.model_device)
    if row["provider_type"] == "local_sam3":
        return LocalSam3Provider(
            info,
            row["model_name"],
            str(settings.sam3_source_dir),
            settings.model_device,
        )
    if row["provider_type"] == "openai_vision":
        return QwenProvider(
            row["endpoint"], row["model_name"], api_key, row["timeout_seconds"], info=info
        )
    return RemoteVisionProvider(
        info, row["endpoint"], api_key, row["timeout_seconds"], config.get("inference", {})
    )
