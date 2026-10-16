from __future__ import annotations

import asyncio
import fcntl
import hashlib
import importlib.metadata
import json
import logging
import math
import os
import platform
import shutil
import socket
import sqlite3
import sys
import threading
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError

from .auth import Actor, get_actor, require_reviewer
from .config import settings
from .worker_client import on_worker
from . import conversion_support as conversion_tools
from .database import all_rows, audit, db, now_iso, one
from .dataset_versions import build_version_export
from .schemas import (
    ConversionCreate,
    ConversionRetryCreate,
    TrainingRetryCreate,
    TrainingRunCreate,
)


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["training"])
_checkpoint_probe_lock = threading.Lock()


def get_pipeline_actor(actor: Actor = Depends(get_actor)) -> Actor:
    require_reviewer(actor)
    return actor

TERMINAL_STATES = {"completed", "failed", "cancelled"}
CONVERSION_SUFFIXES = {"torchscript": ".torchscript", "onnx": ".onnx", "engine": ".engine", "rknn": ".rknn"}
SUPPORTED_TRAINING_TASKS = {"detect", "segment", "classify", "pose"}
KNOWN_TASKS = ("detect", "segment", "classify", "pose", "obb")
LICENSING_NOTICE = {
    "framework": "Ultralytics",
    "default_license": "AGPL-3.0",
    "commercial_option": "Ultralytics Enterprise License",
    "notice": "使用者需自行确认训练、分发与闭源商业部署符合 AGPL-3.0 或已取得适用的商业许可。平台不会自动完成许可证合规。",
    "url": "https://www.ultralytics.com/license",
}


def _path_info(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.is_file(),
        "size_bytes": path.stat().st_size if path.is_file() else 0,
    }


@on_worker("backend")
def _backend_info() -> dict[str, Any]:
    ultralytics_present = find_spec("ultralytics") is not None
    try:
        version = importlib.metadata.version("ultralytics") if ultralytics_present else None
    except importlib.metadata.PackageNotFoundError:
        version = None
    return {
        "name": "ultralytics" if ultralytics_present else None,
        "version": version,
        "installed": ultralytics_present,
    }


@lru_cache(maxsize=256)
def _cached_sha256(path_text: str, size: int, mtime_ns: int, ctime_ns: int) -> str:
    del size, mtime_ns, ctime_ns
    digest = hashlib.sha256()
    with Path(path_text).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    stat = path.stat()
    return _cached_sha256(str(path.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


@lru_cache(maxsize=256)
def _probe_checkpoint(path_text: str, size: int, mtime_ns: int) -> dict[str, Any]:
    """Load an administrator-managed checkpoint once and report its real task.

    Ultralytics checkpoints use torch serialization, so discovery is intentionally
    limited to explicitly configured directories and never includes uploaded files.
    """

    del size, mtime_ns
    try:
        # Model discovery runs on a server, including when DISPLAY was inherited
        # from an SSH session. Never let plotting backend detection open a GUI.
        import matplotlib
        matplotlib.use("Agg", force=True)
        from ultralytics import YOLO

        model = YOLO(path_text, verbose=False)
        task = str(getattr(model, "task", "") or "")
        parameters = sum(parameter.numel() for parameter in model.model.parameters())
        trainable_parameters = sum(
            parameter.numel() for parameter in model.model.parameters() if parameter.requires_grad
        )
        return {
            "loadable": task in KNOWN_TASKS,
            "task": task or None,
            "parameters": parameters,
            "trainable_parameters": trainable_parameters,
            "error": "" if task in KNOWN_TASKS else f"不支持的 checkpoint task: {task or 'unknown'}",
        }
    except Exception as exc:
        return {
            "loadable": False,
            "task": None,
            "parameters": None,
            "trainable_parameters": None,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }


@on_worker("checkpoint")
def _checkpoint_probe(path: Path) -> dict[str, Any]:
    stat = path.stat()
    # Concurrent catalog/availability requests share the same cache fill. Model
    # initialization and plotting imports are not safe to run in parallel here.
    with _checkpoint_probe_lock:
        return _probe_checkpoint(str(path.resolve()), stat.st_size, stat.st_mtime_ns)


def _model_family(name: str) -> str:
    stem = Path(name).stem.lower()
    for prefix, family in (
        ("yolo26", "YOLO26"),
        ("yolo12", "YOLO12"),
        ("yolo11", "YOLO11"),
        ("yolov10", "YOLOv10"),
        ("yolov8", "YOLOv8"),
    ):
        if stem.startswith(prefix):
            return family
    return "Ultralytics YOLO"


def _declared_model_task(name: str) -> str:
    stem = Path(name).stem.lower()
    for suffix, task in (
        ("-seg", "segment"),
        ("-cls", "classify"),
        ("-pose", "pose"),
        ("-obb", "obb"),
    ):
        if stem.endswith(suffix):
            return task
    return "detect"


def _model_defaults(parameters: int | None) -> dict[str, Any]:
    parameters = parameters or 0
    batch_size = 16 if parameters <= 6_000_000 else 8 if parameters <= 15_000_000 else 4
    return {
        "epochs": 50,
        "image_size": 640,
        "batch_size": batch_size,
        "patience": 25,
        "workers": min(8, max(1, (os.cpu_count() or 2) // 2)),
        "optimizer": "auto",
        "lr0": 0.01,
        "weight_decay": 0.0005,
    }


def _model_candidates() -> list[Path]:
    candidates: list[Path] = []
    configured = (settings.training_base_model, settings.training_segmentation_model)
    for path in configured:
        if path.is_file() and path.suffix.lower() == ".pt":
            candidates.append(path.resolve())
    for directory in settings.training_model_dirs:
        if not directory.is_dir():
            continue
        candidates.extend(path.resolve() for path in sorted(directory.glob("*.pt")) if path.is_file())
    # model_arch remains a filename for backwards compatibility. Prefer the first
    # administrator-configured occurrence when two directories contain the same name.
    unique: dict[str, Path] = {}
    for path in candidates:
        unique.setdefault(path.name, path)
    return list(unique.values())


@on_worker("models")
def model_catalog() -> list[dict[str, Any]]:
    backend = _backend_info()
    models: list[dict[str, Any]] = []
    recommended_paths = {
        settings.training_base_model.resolve(),
        settings.training_segmentation_model.resolve(),
    }
    for path in _model_candidates():
        stat = path.stat()
        probe = (
            _checkpoint_probe(path)
            if backend["installed"]
            else {
                "loadable": False,
                "task": None,
                "parameters": None,
                "trainable_parameters": None,
                "error": "未安装 ultralytics",
            }
        )
        task = probe["task"] or _declared_model_task(path.name)
        schema_supported = task in SUPPORTED_TRAINING_TASKS
        available = bool(
            settings.training_enabled
            and backend["installed"]
            and probe["loadable"]
            and schema_supported
        )
        if not settings.training_enabled:
            reason = "训练功能已被 MDP_TRAINING_ENABLED 禁用"
        elif not backend["installed"]:
            reason = "未安装 ultralytics；请安装 requirements-models.txt"
        elif not probe["loadable"]:
            reason = f"checkpoint 无法加载：{probe['error']}"
        elif not schema_supported:
            reason = f"权重已安装，但当前标注结构不支持 {task} 训练"
        else:
            reason = ""
        models.append(
            {
                "id": path.name,
                "model_arch": path.name,
                "name": path.stem,
                "family": _model_family(path.name),
                "task": task,
                "checkpoint_task": probe["task"],
                "task_verified": bool(probe["loadable"]),
                "path": path.name,
                "installed": True,
                "loadable": bool(probe["loadable"]),
                "available": available,
                "trainable": available,
                "reason": reason,
                "size_bytes": stat.st_size,
                "sha256": _file_sha256(path),
                "parameters": probe["parameters"],
                "trainable_parameters": probe["trainable_parameters"],
                "recommended": path in recommended_paths and available,
                "defaults": _model_defaults(probe["parameters"]),
                "compatibility": {
                    "annotation_shapes": {"detect":["rectangle","polygon"],"segment":["polygon","rectangle"],"classify":["classification"],"pose":["keypoints"]}.get(task, []),
                    "dataset_schema_supported": schema_supported,
                },
            }
        )
    return sorted(models, key=lambda item: (KNOWN_TASKS.index(item["task"]) if item["task"] in KNOWN_TASKS else 99, item["family"], item["id"]))


def _task_catalog(models: list[dict[str, Any]]) -> dict[str, Any]:
    task_names = {
        "detect": "目标检测",
        "segment": "实例分割",
        "classify": "图像分类",
        "pose": "姿态估计",
        "obb": "旋转框检测",
    }
    result: dict[str, Any] = {}
    for task in KNOWN_TASKS:
        task_models = [item for item in models if item["task"] == task]
        schema_supported = task in SUPPORTED_TRAINING_TASKS
        available_models = [item["id"] for item in task_models if item["available"]]
        if available_models:
            reason = ""
        elif not schema_supported:
            reason = f"当前标注结构尚不能构建 {task} 数据集"
        elif task_models:
            reason = task_models[0]["reason"]
        else:
            reason = f"配置的模型目录中没有可用的 {task} checkpoint"
        result[task] = {
            "name": task_names[task],
            "available": bool(available_models),
            "dataset_schema_supported": schema_supported,
            "model_ids": [item["id"] for item in task_models],
            "available_model_ids": available_models,
            "reason": reason,
        }
    return result


@on_worker("resources")
def training_resources() -> dict[str, Any]:
    devices: list[dict[str, Any]] = [
        {
            "id": "cpu",
            "type": "cpu",
            "name": platform.processor() or platform.machine() or "CPU",
            "available": True,
            "logical_cores": os.cpu_count() or 1,
            "total_memory_bytes": None,
            "free_memory_bytes": None,
            "utilization_percent": None,
            "reason": "",
        }
    ]
    try:
        import psutil

        memory = psutil.virtual_memory()
        devices[0].update(
            {
                "total_memory_bytes": int(memory.total),
                "free_memory_bytes": int(memory.available),
                "utilization_percent": float(psutil.cpu_percent(interval=None)),
            }
        )
    except Exception:
        pass

    active_leases = all_rows(
        "SELECT * FROM pipeline_resource_leases WHERE expires_at >= ? ORDER BY resource_id",
        (now_iso(),),
    )
    cuda_available = False
    try:
        import torch

        cuda_available = torch.cuda.is_available()
        for index in range(torch.cuda.device_count() if cuda_available else 0):
            props = torch.cuda.get_device_properties(index)
            free, total = torch.cuda.mem_get_info(index)
            try:
                utilization = float(torch.cuda.utilization(index))
            except Exception:
                utilization = None
            devices.append(
                {
                    "id": f"cuda:{index}",
                    "type": "cuda",
                    "index": index,
                    "name": props.name,
                    "available": True,
                    "total_memory_bytes": int(total),
                    "free_memory_bytes": int(free),
                    "utilization_percent": utilization,
                    "compute_capability": f"{props.major}.{props.minor}",
                    "multiprocessor_count": int(props.multi_processor_count),
                    "reason": "",
                }
            )
    except Exception:
        pass

    leases_by_resource = {row["resource_id"]: row for row in active_leases}
    for device in devices:
        lease = leases_by_resource.get(device["id"])
        device["leased"] = lease is not None
        device["lease"] = (
            {
                "task_type": lease["task_type"],
                "task_id": lease["task_id"],
                "worker_id": lease["worker_id"],
                "claim_generation": lease["claim_generation"],
                "expires_at": lease["expires_at"],
            }
            if lease
            else None
        )

    online_workers: list[dict[str, Any]] = []
    cutoff = (datetime.now(UTC) - timedelta(seconds=settings.pipeline_lease_seconds * 2)).isoformat()
    for row in all_rows(
        """SELECT * FROM workers
           WHERE status != 'offline' AND last_seen_at >= ? ORDER BY id""",
        (cutoff,),
    ):
        metadata = _parse_json(row.get("metadata_json"))
        if metadata.get("worker_type") == "pipeline":
            online_workers.append(
                {
                    "id": row["id"],
                    "status": row["status"],
                    "current_job_id": row["current_job_id"],
                    "last_seen_at": row["last_seen_at"],
                    "metadata": metadata,
                }
            )
    gpu_devices = [item for item in devices if item["type"] == "cuda" and item["available"]]
    default_device = (
        max(gpu_devices, key=lambda item: item.get("free_memory_bytes") or 0)["id"]
        if gpu_devices
        else "cpu"
    )
    queue = one(
        """SELECT
             SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS queued,
             SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running
           FROM training_runs"""
    ) or {"queued": 0, "running": 0}
    return {
        "snapshot_at": now_iso(),
        "hostname": socket.gethostname(),
        "cuda_available": cuda_available,
        "default_device": default_device,
        "devices": devices,
        "cpu": devices[0],
        "worker": {
            "embedded_enabled": settings.embedded_pipeline_worker,
            "poll_seconds": settings.training_poll_seconds,
            "lease_seconds": settings.pipeline_lease_seconds,
            "online_count": len(online_workers),
            "workers": online_workers,
        },
        "queue": {
            "queued": int(queue.get("queued") or 0),
            "running": int(queue.get("running") or 0),
        },
        "leases": [dict(row) for row in active_leases],
    }


@on_worker("availability")
def training_availability() -> dict[str, Any]:
    backend = _backend_info()
    models = model_catalog()
    tasks = _task_catalog(models)
    base_available = any(item["available"] for item in models)
    available = settings.training_enabled and backend["installed"] and base_available
    if not settings.training_enabled:
        reason = "训练功能已被 MDP_TRAINING_ENABLED 禁用"
    elif not backend["installed"]:
        reason = "未安装 ultralytics；请安装 requirements-models.txt"
    elif not base_available:
        reason = "没有与当前数据结构兼容且可加载的本地训练权重"
    else:
        reason = ""
    resources = training_resources()
    cuda_available = resources["cuda_available"]
    conversion_formats = conversion_tools.format_catalog(settings.training_enabled, backend["installed"], cuda_available)
    return {
        "available": available,
        "reason": reason,
        "backend": backend["name"],
        "backend_version": backend["version"],
        "cuda_available": cuda_available,
        "base_models": {
            "detect": _path_info(settings.training_base_model),
            "segment": _path_info(settings.training_segmentation_model),
        },
        "tasks": tasks,
        "conversion_formats": conversion_formats,
        "catalog_url": "/api/training/catalog",
        "resources_url": "/api/training/resources",
        "licensing": LICENSING_NOTICE,
    }


def _parse_json(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _build_artifact_metadata(path: Path, **metadata: Any) -> dict[str, Any]:
    return {
        "available": path.is_file(),
        "filename": path.name,
        "size_bytes": path.stat().st_size if path.is_file() else 0,
        "sha256": _file_sha256(path) if path.is_file() else None,
        "created_at": now_iso(),
        **metadata,
    }


def _artifact_metadata(
    path_value: str | None,
    persisted: dict[str, Any],
    download_url: str,
) -> dict[str, Any] | None:
    if not path_value:
        return None
    path = Path(path_value)
    if not path.is_file():
        return {
            **persisted,
            "available": False,
            "size_bytes": 0,
            "sha256": None,
            "download_url": download_url,
        }
    valid = not persisted.get("sha256") or (path.stat().st_size == persisted.get("size_bytes") and _file_sha256(path) == persisted["sha256"])
    return {
        **(persisted or _build_artifact_metadata(path, filename=path.name)),
        "available": valid,
        "integrity": "verified" if valid and persisted.get("sha256") else "legacy_unverified" if valid else "failed",
        "error": "" if valid else "产物 SHA-256 校验失败",
        "download_url": download_url,
    }


def serialize_training_run(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["cancel_requested"] = bool(result["cancel_requested"])
    result["settings"] = _parse_json(result.pop("settings_json", "{}"))
    result["metrics"] = _parse_json(result.pop("metrics_json", "{}"))
    result["resource"] = _parse_json(result.pop("resource_json", "{}"))
    persisted_artifact = _parse_json(result.pop("artifact_metadata_json", "{}"))
    result["artifact"] = _artifact_metadata(
        result.pop("artifact_path", None),
        persisted_artifact,
        f"/api/training-runs/{result['id']}/artifact",
    )
    result["root_run_id"] = result.get("root_run_id") or result["id"]
    result["lineage"] = {
        "parent_run_id": result.get("parent_run_id"),
        "root_run_id": result["root_run_id"],
        "attempt": result.get("attempt", 0),
        "max_retries": result.get("max_retries", 0),
        "retryable": result.get("status") in {"failed", "cancelled"}
        and result.get("attempt", 0) < result.get("max_retries", 0),
        "claim_generation": result.get("claim_generation", 0),
        "resume_count": result.get("resume_count", 0),
    }
    result["logs_url"] = f"/api/training-runs/{result['id']}/logs"
    result["metrics_history_url"] = f"/api/training-runs/{result['id']}/metrics"
    return result


def serialize_conversion(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["cancel_requested"] = bool(result.get("cancel_requested"))
    result["settings"] = _parse_json(result.pop("settings_json", "{}"))
    result["resource"] = _parse_json(result.pop("resource_json", "{}"))
    persisted_artifact = _parse_json(result.pop("artifact_metadata_json", "{}"))
    result["artifact"] = _artifact_metadata(
        result.pop("artifact_path", None),
        persisted_artifact,
        f"/api/conversions/{result['id']}/artifact",
    )
    result["root_conversion_id"] = result.get("root_conversion_id") or result["id"]
    result["lineage"] = {
        "parent_conversion_id": result.get("parent_conversion_id"),
        "root_conversion_id": result["root_conversion_id"],
        "attempt": result.get("attempt", 0),
        "max_retries": result.get("max_retries", 0),
        "retryable": result.get("status") in {"failed", "cancelled"}
        and result.get("attempt", 0) < result.get("max_retries", 0),
        "claim_generation": result.get("claim_generation", 0),
    }
    result["logs_url"] = f"/api/conversions/{result['id']}/logs"
    result["manifest_available"] = bool(result.get("status") == "completed" and persisted_artifact.get("deployment"))
    result["manifest_url"] = f"/api/conversions/{result['id']}/manifest" if result["manifest_available"] else None
    return result


def _append_training_log(
    run_id: int,
    level: str,
    event: str,
    message: str,
    data: dict[str, Any] | None = None,
) -> None:
    with db() as conn:
        conn.execute(
            """INSERT INTO training_run_logs
               (run_id, level, event, message, data_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                level,
                event,
                message[:2000],
                json.dumps(data or {}, ensure_ascii=False),
                now_iso(),
            ),
        )


def _append_conversion_log(
    conversion_id: int,
    level: str,
    event: str,
    message: str,
    data: dict[str, Any] | None = None,
) -> None:
    with db() as conn:
        conn.execute(
            """INSERT INTO conversion_logs
               (conversion_id, level, event, message, data_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                conversion_id,
                level,
                event,
                message[:2000],
                json.dumps(data or {}, ensure_ascii=False),
                now_iso(),
            ),
        )


def _serialize_log(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["data"] = _parse_json(item.pop("data_json", "{}"))
    return item


def _require_project(project_id: int) -> dict[str, Any]:
    project = one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not project:
        raise HTTPException(404, "项目不存在")
    return project


def _require_training_run(run_id: int) -> dict[str, Any]:
    run = one("SELECT * FROM training_runs WHERE id = ?", (run_id,))
    if not run:
        raise HTTPException(404, "训练任务不存在")
    return run


def _require_conversion(conversion_id: int) -> dict[str, Any]:
    conversion = one("SELECT * FROM model_conversions WHERE id = ?", (conversion_id,))
    if not conversion:
        raise HTTPException(404, "模型转换任务不存在")
    return conversion


def _version_task(version: dict[str, Any]) -> str:
    stats = _parse_json(version.get("stats_json"))
    return stats.get("task_type") if stats.get("task_type") in SUPPORTED_TRAINING_TASKS else "detect"


def _version_compatibility(version: dict[str, Any], task: str) -> dict[str, Any]:
    stats = _parse_json(version.get("stats_json"))
    shapes = stats.get("shape_types", {})
    rectangles = int(shapes.get("rectangle") or 0)
    polygons = int(shapes.get("polygon") or 0)
    transformations: list[str] = []
    warnings: list[str] = []
    augmentation = stats.get("augmentation") or {}
    if (augmentation.get("counts") or {}).get("generated", 0):
        warnings.append("此版本已包含增强图片；训练器仍会执行默认在线增强，两者会叠加。建议先与未增强版本对比，再逐步增加增强强度。")
    if task == "detect" and polygons:
        transformations.append("polygon_to_bbox")
        warnings.append(f"{polygons} 个多边形将在训练导出时转换为外接矩形，轮廓信息会丢失")
    if task == "segment" and rectangles:
        transformations.append("rectangle_to_polygon")
        warnings.append(f"{rectangles} 个矩形将在训练导出时转换为四角多边形")
    compatible = task in {"detect", "segment"} and (rectangles + polygons) > 0
    if task == "classify":
        compatible = stats.get("task_type") == "classify" and int(shapes.get("classification") or 0) > 0 and not stats.get("empty_asset_count")
    elif task == "pose":
        compatible = stats.get("task_type") == "pose" and int(shapes.get("keypoints") or 0) > 0
    return {
        "task": task,
        "compatible": compatible,
        "quality": "degraded" if transformations else "native",
        "shape_counts": shapes,
        "transformations": transformations,
        "warnings": warnings,
        "reason": "" if compatible else "数据版本不包含此任务的完整有效标注",
    }


def _resolve_base_model(model_arch: str, task: str) -> tuple[Path, dict[str, Any]] | None:
    catalog_item = next((item for item in model_catalog() if item["id"] == model_arch), None)
    if not catalog_item or not catalog_item["available"] or catalog_item["task"] != task:
        return None
    path = next((item for item in _model_candidates() if item.name == model_arch), None)
    if not path or _checkpoint_probe(path).get("task") != task:
        return None
    return path, catalog_item


def _canonical_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@router.get("/training/catalog")
def get_training_catalog(_: Actor = Depends(get_actor)) -> dict[str, Any]:
    models = model_catalog()
    availability = training_availability()
    return {
        "backend": {
            "name": availability["backend"],
            "version": availability["backend_version"],
            "available": availability["available"],
            "reason": availability["reason"],
        },
        "models": models,
        "tasks": availability["tasks"],
        "resources": training_resources(),
        "conversion_formats": availability["conversion_formats"],
        "resource_profiles": {
            "balanced": {
                "name": "均衡",
                "description": "优先选择空闲 GPU；无 CUDA 的主机回退 CPU",
            },
            "performance": {
                "name": "性能优先",
                "description": "要求 GPU，并优先租用空闲显存最多的设备",
            },
            "low_memory": {
                "name": "显存友好",
                "description": "优先租用空闲显存比例最高的 GPU；训练参数仍由用户明确设置",
            },
        },
        "licensing": LICENSING_NOTICE,
        "allowed_hyperparameters": [
            "epochs", "image_size", "batch_size", "device", "workers", "patience",
            "optimizer", "lr0", "weight_decay", "seed", "deterministic", "amp",
            "cache", "save_period", "freeze", "cos_lr", "close_mosaic", "resource_profile",
        ],
    }


@router.get("/training/resources")
def get_training_resources(_: Actor = Depends(get_actor)) -> dict[str, Any]:
    return training_resources()


@router.get("/versions/{version_id}/training-compatibility")
def get_version_training_compatibility(
    version_id: int, _: Actor = Depends(get_actor)
) -> dict[str, Any]:
    version = one("SELECT * FROM dataset_versions WHERE id = ?", (version_id,))
    if not version:
        raise HTTPException(404, "数据集版本不存在")
    models = model_catalog()
    return {
        "version_id": version_id,
        "inferred_task": _version_task(version),
        "tasks": {
            task: {
                **_version_compatibility(version, task),
                "model_ids": [
                    item["id"] for item in models if item["task"] == task and item["available"]
                ],
            }
            for task in SUPPORTED_TRAINING_TASKS
        },
    }


@router.get("/projects/{project_id}/training-runs")
def list_training_runs(
    project_id: int,
    page: int | None = Query(default=None, ge=1, le=2147483647),
    page_size: int = Query(default=10, ge=1, le=50),
    q: str = Query(default="", max_length=200),
    status: Literal["all", "active", "queued", "running", "completed", "failed", "cancelled"] = "all",
    task: Literal["all", "detect", "segment", "pose", "classify"] = "all",
    locate_id: int | None = Query(default=None, ge=1, le=9223372036854775807),
    include_availability: bool = True,
    _: Actor = Depends(get_actor),
) -> dict[str, Any]:
    if locate_id is not None and page is None:
        raise HTTPException(422, "定位训练记录时需要同时指定 page")
    clauses = ["project_id = ?"]
    params: list[Any] = [project_id]
    if status == "active":
        clauses.append("status IN ('queued', 'running')")
    elif status != "all":
        clauses.append("status = ?")
        params.append(status)
    if task != "all":
        clauses.append("task = ?")
        params.append(task)
    if q.strip():
        # Treat wildcard characters as text, including the escape character.
        text = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("(name LIKE ? ESCAPE '\\' OR model_arch LIKE ? ESCAPE '\\')")
        params.extend([f"%{text}%", f"%{text}%"])
    where = " AND ".join(clauses)
    pagination = None
    locate_found = False
    with db() as conn:
        # Counts, location and rows must describe the same read snapshot even
        # when workers complete runs or another user starts a new run.
        conn.execute("BEGIN")
        if not conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone():
            raise HTTPException(404, "项目不存在")
        counts = _training_counts(conn, project_id)
        if page is not None:
            matching = conn.execute(f"SELECT COUNT(*) FROM training_runs WHERE {where}", params).fetchone()[0]
            total_pages = max(1, (matching + page_size - 1) // page_size)
            if locate_id is not None:
                locate_found = conn.execute(
                    f"SELECT 1 FROM training_runs WHERE {where} AND id = ?", [*params, locate_id]
                ).fetchone() is not None
                if locate_found:
                    preceding = conn.execute(
                        f"SELECT COUNT(*) FROM training_runs WHERE {where} AND id > ?", [*params, locate_id]
                    ).fetchone()[0]
                    page = preceding // page_size + 1
            page = min(page, total_pages)
            pagination = {"page": page, "page_size": page_size, "total": matching, "total_pages": total_pages}
            rows = conn.execute(
                f"SELECT * FROM training_runs WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        else:
            # Keep the original unpaginated API for existing integrations.
            rows = conn.execute(f"SELECT * FROM training_runs WHERE {where} ORDER BY id DESC", params).fetchall()
    result = {
        "runs": [serialize_training_run(dict(row)) for row in rows],
        "total": counts["all"],
        "counts": counts,
    }
    # Polling needs current database state without waiting for model discovery
    # or the periodically refreshed conversion-environment subprocess probes.
    if include_availability:
        result["availability"] = training_availability()
    if pagination is not None:
        result["pagination"] = pagination
    if locate_id is not None:
        result["locate_found"] = locate_found
    return result


def _training_counts(conn: sqlite3.Connection, project_id: int) -> dict[str, int]:
    counts = {status: 0 for status in ("queued", "running", "completed", "failed", "cancelled")}
    rows = conn.execute(
        "SELECT status, COUNT(*) AS count FROM training_runs WHERE project_id = ? GROUP BY status", (project_id,)
    ).fetchall()
    for row in rows:
        if row["status"] in counts:
            counts[row["status"]] = row["count"]
    counts["all"] = sum(row["count"] for row in rows)
    counts["active"] = counts["queued"] + counts["running"]
    return counts


@router.get("/projects/{project_id}/training-summary")
def get_training_summary(project_id: int, _: Actor = Depends(get_actor)) -> dict[str, Any]:
    # Overview navigation needs stored progress only, not model discovery,
    # artifact filesystem checks, or full metrics/settings for every run.
    fields = "id, project_id, version_id, name, model_arch, task, status, progress, current_epoch, total_epochs, created_at, started_at, finished_at"
    with db() as conn:
        conn.execute("BEGIN")
        if not conn.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone():
            raise HTTPException(404, "项目不存在")
        counts = _training_counts(conn, project_id)
        recent = [dict(row) for row in conn.execute(
            f"SELECT {fields} FROM training_runs WHERE project_id = ? ORDER BY id DESC LIMIT 3", (project_id,)
        ).fetchall()]
        completed = conn.execute(
            f"SELECT {fields} FROM training_runs WHERE project_id = ? AND status = 'completed' "
            "ORDER BY finished_at DESC, id DESC LIMIT 1", (project_id,)
        ).fetchone()
    return {
        "project_id": project_id,
        "total": counts["all"],
        "counts": counts,
        "recent": recent,
        "latest_completed": dict(completed) if completed else None,
    }


@router.get("/projects/{project_id}/model-conversions")
def list_project_conversions(
    project_id: int,
    status: str | None = Query(default=None, max_length=20),
    before_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=200, ge=1, le=1000),
    _: Actor = Depends(get_actor),
) -> dict[str, Any]:
    _require_project(project_id)
    allowed = {"queued", "running", "completed", "failed", "cancelled", "active"}
    if status is not None and status not in allowed:
        raise HTTPException(422, f"status must be one of: {', '.join(sorted(allowed))}")
    clauses = ["r.project_id = ?"]
    params: list[Any] = [project_id]
    if status == "active":
        clauses.append("c.status IN ('queued', 'running')")
    elif status:
        clauses.append("c.status = ?")
        params.append(status)
    if before_id is not None:
        clauses.append("c.id < ?")
        params.append(before_id)
    params.append(limit + 1)
    rows = all_rows(
        f"""SELECT c.* FROM model_conversions c
             JOIN training_runs r ON r.id = c.training_run_id
             WHERE {' AND '.join(clauses)}
             ORDER BY c.id DESC LIMIT ?""",
        tuple(params),
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    counts = {
        row["status"]: row["count"]
        for row in all_rows(
            """SELECT c.status, COUNT(*) AS count FROM model_conversions c
               JOIN training_runs r ON r.id = c.training_run_id
               WHERE r.project_id = ? GROUP BY c.status""",
            (project_id,),
        )
    }
    return {
        "project_id": project_id,
        "availability": training_availability()["conversion_formats"],
        "counts": {name: int(counts.get(name, 0)) for name in sorted(TERMINAL_STATES | {"queued", "running"})},
        "conversions": [serialize_conversion(row) for row in rows],
        "next_before_id": rows[-1]["id"] if rows and has_more else None,
        "has_more": has_more,
    }


@router.post("/projects/{project_id}/training-runs", status_code=202)
def create_training_run(
    project_id: int,
    payload: TrainingRunCreate,
    actor: Actor = Depends(get_pipeline_actor),
) -> dict[str, Any]:
    _require_project(project_id)
    availability = training_availability()
    if not availability["available"]:
        raise HTTPException(503, {"message": "本机训练后端不可用", "availability": availability})
    version = one(
        "SELECT * FROM dataset_versions WHERE id = ? AND project_id = ?",
        (payload.version_id, project_id),
    )
    if not version:
        raise HTTPException(404, "项目中不存在该数据集版本")
    if version["annotation_count"] <= 0:
        raise HTTPException(409, "数据集版本没有标注，不能训练")
    stats = _parse_json(version["stats_json"])
    if not stats.get("splits", {}).get("valid", {}).get("asset_count"):
        raise HTTPException(409, "验证集为空；请用非零 valid_ratio 重新创建数据集版本")
    if not stats.get("splits", {}).get("train", {}).get("annotation_count"):
        raise HTTPException(409, "训练集没有标注对象；请调整划分种子或补充标注")
    if not stats.get("splits", {}).get("valid", {}).get("annotation_count"):
        raise HTTPException(409, "验证集没有标注对象；请调整划分种子或补充标注")
    inferred_task = _version_task(version)
    task = inferred_task if payload.task == "auto" else payload.task
    compatibility = _version_compatibility(version, task)
    if not compatibility["compatible"]:
        raise HTTPException(409, {"message": "数据集版本与所选任务不兼容", "compatibility": compatibility})
    resolved_model = _resolve_base_model(payload.model_arch, task)
    if not resolved_model:
        raise HTTPException(
            409,
            {
                "message": f"模型 {payload.model_arch} 的实际 checkpoint task 与 {task} 不兼容，或权重不可用",
                "availability": availability,
            },
        )
    base_model, model_metadata = resolved_model
    resources = training_resources()
    requested_device = "cuda:0" if payload.device == "cuda" else payload.device
    resource_ids = {item["id"] for item in resources["devices"] if item["available"]}
    if requested_device.startswith("cuda:") and requested_device not in resource_ids:
        raise HTTPException(
            422,
            {"message": f"请求的设备 {requested_device} 不存在或不可用", "resources": resources},
        )
    if payload.resource_profile == "performance" and requested_device == "cpu":
        raise HTTPException(422, "performance 资源策略不能与 CPU 设备组合")
    if (
        payload.resource_profile == "performance"
        and requested_device == "auto"
        and not resources["cuda_available"]
    ):
        raise HTTPException(422, "performance 资源策略要求可用 CUDA GPU")
    name = payload.name or f"v{version['version_number']} {base_model.stem}"
    hyperparameters = payload.model_dump(
        exclude={"version_id", "name", "model_arch", "task", "max_retries"}
    )
    settings_json = {
        "version_content_hash": version["content_hash"],
        "base_model": str(base_model),
        "base_model_sha256": model_metadata["sha256"],
        "base_model_size_bytes": model_metadata["size_bytes"],
        "backend": availability["backend"],
        "backend_version": availability["backend_version"],
        "requested_task": payload.task,
        "resolved_task": task,
        "inferred_version_task": inferred_task,
        "compatibility": compatibility,
        **hyperparameters,
    }
    config_hash = _canonical_hash(
        {
            "version_id": payload.version_id,
            "version_content_hash": version["content_hash"],
            "model_arch": base_model.name,
            "task": task,
            "settings": settings_json,
        }
    )
    resource_json = {
        "requested_device": requested_device,
        "resource_profile": payload.resource_profile,
        "state": "queued",
    }
    now = now_iso()
    with db() as conn:
        cursor = conn.execute(
            """INSERT INTO training_runs
               (project_id, version_id, name, model_arch, task, status, total_epochs,
                settings_json, metrics_json, max_retries, config_hash, resource_json,
                progress_message, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, '{}', ?, ?, ?, ?, ?, ?)""",
            (
                project_id,
                payload.version_id,
                name,
                base_model.name,
                task,
                payload.epochs,
                json.dumps(settings_json, ensure_ascii=False),
                payload.max_retries,
                config_hash,
                json.dumps(resource_json, ensure_ascii=False),
                "等待训练 worker",
                actor.id,
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        conn.execute("UPDATE training_runs SET root_run_id = ? WHERE id = ?", (run_id, run_id))
    _append_training_log(
        run_id,
        "info",
        "queued",
        "训练任务已进入队列",
        {
            "model_arch": base_model.name,
            "task": task,
            "version_id": payload.version_id,
            "config_hash": config_hash,
            "warnings": compatibility["warnings"],
        },
    )
    audit(
        actor.id,
        "enqueue",
        "training_run",
        run_id,
        project_id,
        {"version_id": payload.version_id, "task": task, "config_hash": config_hash, **settings_json},
    )
    return serialize_training_run(_require_training_run(run_id))


@router.get("/training-runs/{run_id}")
def get_training_run(run_id: int, _: Actor = Depends(get_actor)) -> dict[str, Any]:
    return serialize_training_run(_require_training_run(run_id))


@router.get("/training-runs/{run_id}/logs")
def get_training_logs(
    run_id: int,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    _: Actor = Depends(get_actor),
) -> dict[str, Any]:
    _require_training_run(run_id)
    rows = all_rows(
        """SELECT * FROM training_run_logs
           WHERE run_id = ? AND id > ? ORDER BY id LIMIT ?""",
        (run_id, after_id, limit + 1),
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "run_id": run_id,
        "logs": [_serialize_log(row) for row in rows],
        "next_after_id": rows[-1]["id"] if rows else after_id,
        "has_more": has_more,
    }


@router.get("/training-runs/{run_id}/metrics")
def get_training_metrics(run_id: int, _: Actor = Depends(get_actor)) -> dict[str, Any]:
    # Read progress and epoch history from the same snapshot while a worker is
    # committing the next epoch or restarting an interrupted attempt.
    with db() as conn:
        conn.execute("BEGIN")
        row = conn.execute("SELECT * FROM training_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "训练任务不存在")
        run = dict(row)
        rows = conn.execute("SELECT * FROM training_metrics WHERE run_id=? ORDER BY epoch", (run_id,)).fetchall()
    history = []
    for row in rows:
        item = dict(row)
        item["metrics"] = _safe_metric_json(_parse_json(item.pop("metrics_json", "{}")))
        item["losses"] = _loss_summary(item["metrics"])
        history.append(item)
    latest_epoch_metrics = history[-1]["metrics"] if history else {}
    return {
        "run_id": run_id,
        "task": run["task"],
        "status": run["status"],
        "current_epoch": run["current_epoch"],
        "total_epochs": run["total_epochs"],
        "history": history,
        # Final best-model evaluation is retained separately from the last
        # training epoch and must never be plotted as another epoch.
        "latest": _safe_metric_json(_parse_json(run.get("metrics_json"))),
        "latest_epoch": history[-1]["epoch"] if history else None,
        "latest_epoch_metrics": latest_epoch_metrics,
        "latest_losses": _loss_summary(latest_epoch_metrics),
    }


@router.post("/training-runs/{run_id}/retry", status_code=202)
def retry_training_run(
    run_id: int,
    payload: TrainingRetryCreate | None = None,
    actor: Actor = Depends(get_pipeline_actor),
) -> dict[str, Any]:
    preflight_run = _require_training_run(run_id)
    if preflight_run["status"] not in {"failed", "cancelled"}:
        raise HTTPException(409, "只有失败或已取消的训练任务可以重试")
    attempt = int(preflight_run.get("attempt") or 0)
    max_retries = int(preflight_run.get("max_retries") or 0)
    if attempt >= max_retries:
        raise HTTPException(409, f"该训练谱系已达到最大重试次数 {max_retries}")
    config = _parse_json(preflight_run["settings_json"])
    version = one(
        "SELECT * FROM dataset_versions WHERE id = ?", (preflight_run["version_id"],)
    )
    if not version or version["content_hash"] != config.get("version_content_hash"):
        raise HTTPException(409, "原始不可变数据版本缺失或内容哈希不匹配")
    resolved_model = _resolve_base_model(
        preflight_run["model_arch"], preflight_run["task"]
    )
    if not resolved_model:
        raise HTTPException(409, "原始基础模型已不可用或任务元数据不匹配")
    model_path, model_metadata = resolved_model
    if model_metadata["sha256"] != config.get("base_model_sha256"):
        raise HTTPException(409, "基础模型文件已变化；为保证谱系可复现，不能直接重试")
    resource = {
        "requested_device": config.get("device", "auto"),
        "resource_profile": config.get("resource_profile", "balanced"),
        "state": "queued",
    }
    try:
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            locked_row = conn.execute(
                "SELECT * FROM training_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if not locked_row:
                raise HTTPException(404, "训练任务不存在")
            run = dict(locked_row)
            existing = conn.execute(
                "SELECT id FROM training_runs WHERE parent_run_id = ?", (run_id,)
            ).fetchone()
            if existing:
                raise HTTPException(
                    409,
                    {
                        "message": "该训练任务已经创建过重试任务",
                        "existing_child_id": int(existing[0]),
                    },
                )
            if run["status"] not in {"failed", "cancelled"}:
                raise HTTPException(409, "只有失败或已取消的训练任务可以重试")
            attempt = int(run.get("attempt") or 0)
            max_retries = int(run.get("max_retries") or 0)
            if attempt >= max_retries:
                raise HTTPException(409, f"该训练谱系已达到最大重试次数 {max_retries}")
            if any(
                run[key] != preflight_run[key]
                for key in ("settings_json", "version_id", "model_arch", "task")
            ):
                raise HTTPException(409, "训练配置在重试校验期间发生变化，请重新请求")
            locked_version = conn.execute(
                "SELECT content_hash FROM dataset_versions WHERE id = ?", (run["version_id"],)
            ).fetchone()
            if not locked_version or locked_version[0] != config.get("version_content_hash"):
                raise HTTPException(409, "原始不可变数据版本缺失或内容哈希不匹配")
            root_run_id = run.get("root_run_id") or run_id
            new_name = (payload.name if payload else None) or f"{run['name']} · retry {attempt + 1}"
            created_at = now_iso()
            cursor = conn.execute(
                """INSERT INTO training_runs
                   (project_id, version_id, name, model_arch, task, status, total_epochs,
                    settings_json, metrics_json, parent_run_id, root_run_id, attempt,
                    max_retries, config_hash, resource_json, progress_message, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, '{}', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run["project_id"],
                    run["version_id"],
                    new_name,
                    model_path.name,
                    run["task"],
                    run["total_epochs"],
                    run["settings_json"],
                    run_id,
                    root_run_id,
                    attempt + 1,
                    max_retries,
                    run.get("config_hash") or _canonical_hash(config),
                    json.dumps(resource, ensure_ascii=False),
                    "等待训练 worker",
                    actor.id,
                    created_at,
                ),
            )
            retry_id = int(cursor.lastrowid)
            conn.executemany(
                """INSERT INTO training_run_logs
                   (run_id, level, event, message, data_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    (
                        run_id,
                        "info",
                        "retry_created",
                        f"已创建重试任务 #{retry_id}",
                        json.dumps({"retry_run_id": retry_id}, ensure_ascii=False),
                        created_at,
                    ),
                    (
                        retry_id,
                        "info",
                        "queued",
                        f"从训练任务 #{run_id} 克隆配置并进入队列",
                        json.dumps(
                            {"parent_run_id": run_id, "root_run_id": root_run_id},
                            ensure_ascii=False,
                        ),
                        created_at,
                    ),
                ),
            )
            conn.execute(
                """INSERT INTO audit_logs
                   (actor, action, resource_type, resource_id, project_id, payload_json, created_at)
                   VALUES (?, 'retry', 'training_run', ?, ?, ?, ?)""",
                (
                    actor.id,
                    str(retry_id),
                    run["project_id"],
                    json.dumps(
                        {"parent_run_id": run_id, "attempt": attempt + 1},
                        ensure_ascii=False,
                    ),
                    created_at,
                ),
            )
    except sqlite3.IntegrityError as exc:
        existing = one(
            "SELECT id FROM training_runs WHERE parent_run_id = ?", (run_id,)
        )
        if existing:
            raise HTTPException(
                409,
                {
                    "message": "该训练任务已经创建过重试任务",
                    "existing_child_id": existing["id"],
                },
            ) from exc
        raise
    return serialize_training_run(_require_training_run(retry_id))


@router.post("/training-runs/{run_id}/cancel")
def cancel_training_run(run_id: int, actor: Actor = Depends(get_actor)) -> dict[str, Any]:
    run = _require_training_run(run_id)
    if run["status"] in TERMINAL_STATES:
        raise HTTPException(409, "训练任务已经结束")
    now = now_iso()
    with db() as conn:
        conn.execute(
            """UPDATE training_runs SET cancel_requested = 1,
               status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END,
               finished_at = CASE WHEN status = 'queued' THEN ? ELSE finished_at END,
               progress_message = CASE WHEN status = 'queued' THEN '已取消' ELSE '正在安全停止' END
               WHERE id = ?""",
            (now, run_id),
        )
    _append_training_log(run_id, "warning", "cancel_requested", "用户请求取消训练任务")
    audit(actor.id, "cancel", "training_run", run_id, run["project_id"])
    return serialize_training_run(_require_training_run(run_id))


def _validated_artifact(path_value: str | None, kind: str, metadata: dict | None = None) -> Path:
    if not path_value:
        raise HTTPException(409, f"{kind}产物尚未生成")
    path = Path(path_value).resolve()
    allowed_root = settings.training_dir.resolve()
    if allowed_root not in path.parents or not path.is_file():
        raise HTTPException(409, f"{kind}产物文件不存在")
    if metadata and metadata.get("sha256"):
        if path.stat().st_size != metadata.get("size_bytes") or _file_sha256(path) != metadata["sha256"]:
            raise HTTPException(409, f"{kind}产物 SHA-256 校验失败，文件可能已损坏或被替换")
    return path


@router.get("/training-runs/{run_id}/artifact")
def download_training_artifact(
    run_id: int, _: Actor = Depends(get_actor)
) -> FileResponse:
    run = _require_training_run(run_id)
    if run["status"] != "completed":
        raise HTTPException(409, "训练尚未完成")
    path = _validated_artifact(run.get("artifact_path"), "训练", _parse_json(run.get("artifact_metadata_json")))
    return FileResponse(path, media_type="application/octet-stream", filename=f"training-{run_id}-best.pt")


@router.get("/training-runs/{run_id}/conversions")
def list_conversions(run_id: int, _: Actor = Depends(get_actor)) -> dict[str, Any]:
    _require_training_run(run_id)
    return {
        "availability": training_availability()["conversion_formats"],
        "conversions": [
            serialize_conversion(row)
            for row in all_rows(
                "SELECT * FROM model_conversions WHERE training_run_id = ? ORDER BY id DESC",
                (run_id,),
            )
        ],
    }


@router.get("/training-runs/{run_id}/conversion-options")
def conversion_options(run_id: int, _: Actor = Depends(get_actor)) -> dict[str, Any]:
    run = _require_training_run(run_id)
    count = len(conversion_tools.calibration_assets(run["version_id"]))
    image_size = _parse_json(run.get("settings_json")).get("image_size", 640)
    rknn = {"target_platform": "rk3588", "quantized": False, "quantized_algorithm": "normal", "optimization_level": 3, "mean_values": [0, 0, 0], "std_values": [255, 255, 255], "input_color": "rgb", "calibration_source": "version_train", "calibration_count": max(1, min(100, count))}
    return {"training_run_id": run_id, "task": run["task"], "formats": training_availability()["conversion_formats"],
            "calibration": {"source": "version_train", "version_id": run["version_id"], "available_count": count, "default_count": max(1, min(100, count)), "max_count": min(1000, count), "reason": "" if count else "训练使用的冻结版本没有训练集图片，无法进行 INT8 校准。"},
            "defaults": {"image_size": image_size, "device": "auto", "batch": 1, "workspace": None, "rknn": rknn}}


def _validate_conversion_config(run: dict[str, Any], payload: ConversionCreate, *, check_availability: bool = True) -> dict[str, Any]:
    if run["task"] not in SUPPORTED_TRAINING_TASKS:
        raise HTTPException(422, "当前训练任务类型暂不支持转换。")
    if check_availability:
        available = training_availability()["conversion_formats"][payload.format]
        if not available["available"]:
            raise HTTPException(503, {"message": available["reason"] or "当前格式的转换环境不可用", "availability": available})
    if payload.rknn and payload.rknn.quantized:
        count = len(conversion_tools.calibration_assets(run["version_id"]))
        if payload.rknn.calibration_count > count:
            raise HTTPException(422, f"冻结版本的训练集有 {count} 张图片，校准数量不能超过此数量。")
    resources = training_resources()
    requested_device = "cuda:0" if payload.device == "cuda" else payload.device
    if payload.format == "rknn":
        requested_device = "cpu"
    if (payload.format == "engine" or payload.half) and not resources["cuda_available"]:
        raise HTTPException(422, "TensorRT / FP16 导出需要可用的 CUDA GPU。")
    resource_ids = {item["id"] for item in resources["devices"] if item["available"]}
    if requested_device.startswith("cuda:") and requested_device not in resource_ids:
        raise HTTPException(422, "所选 CUDA 设备不存在或不可用，请重新选择。")
    return {"requested_device": requested_device, "state": "queued"}


@router.post("/training-runs/{run_id}/conversions", status_code=202)
def create_conversion(
    run_id: int,
    payload: ConversionCreate,
    actor: Actor = Depends(get_pipeline_actor),
) -> dict[str, Any]:
    run = _require_training_run(run_id)
    if run["status"] != "completed":
        raise HTTPException(409, "只有已完成的训练任务可以转换")
    _validated_artifact(run.get("artifact_path"), "训练", _parse_json(run.get("artifact_metadata_json")))
    resource_json = _validate_conversion_config(run, payload)
    settings_json = payload.model_dump()
    with db() as conn:
        cursor = conn.execute(
            """INSERT INTO model_conversions
               (training_run_id, format, status, settings_json, max_retries,
                resource_json, progress_message, created_by, created_at)
               VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                payload.format,
                json.dumps(settings_json, ensure_ascii=False),
                payload.max_retries,
                json.dumps(resource_json, ensure_ascii=False),
                "等待转换 worker",
                actor.id,
                now_iso(),
            ),
        )
        conversion_id = int(cursor.lastrowid)
        conn.execute(
            "UPDATE model_conversions SET root_conversion_id = ? WHERE id = ?",
            (conversion_id, conversion_id),
        )
    _append_conversion_log(
        conversion_id,
        "info",
        "queued",
        "模型转换任务已进入队列",
        {"training_run_id": run_id, "format": payload.format},
    )
    audit(
        actor.id,
        "enqueue",
        "model_conversion",
        conversion_id,
        run["project_id"],
        settings_json,
    )
    return serialize_conversion(_require_conversion(conversion_id))


@router.get("/conversions/{conversion_id}")
def get_conversion(conversion_id: int, _: Actor = Depends(get_actor)) -> dict[str, Any]:
    return serialize_conversion(_require_conversion(conversion_id))


@router.get("/conversions/{conversion_id}/logs")
def get_conversion_logs(
    conversion_id: int,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    _: Actor = Depends(get_actor),
) -> dict[str, Any]:
    _require_conversion(conversion_id)
    rows = all_rows(
        """SELECT * FROM conversion_logs
           WHERE conversion_id = ? AND id > ? ORDER BY id LIMIT ?""",
        (conversion_id, after_id, limit + 1),
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "conversion_id": conversion_id,
        "logs": [_serialize_log(row) for row in rows],
        "next_after_id": rows[-1]["id"] if rows else after_id,
        "has_more": has_more,
    }


@router.post("/conversions/{conversion_id}/cancel")
def cancel_conversion(
    conversion_id: int, actor: Actor = Depends(get_actor)
) -> dict[str, Any]:
    conversion = _require_conversion(conversion_id)
    if conversion["status"] in TERMINAL_STATES:
        raise HTTPException(409, "模型转换任务已经结束")
    now = now_iso()
    with db() as conn:
        conn.execute(
            """UPDATE model_conversions SET cancel_requested = 1,
               status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END,
               finished_at = CASE WHEN status = 'queued' THEN ? ELSE finished_at END,
               progress_message = CASE WHEN status = 'queued' THEN '已取消' ELSE '正在安全停止' END
               WHERE id = ?""",
            (now, conversion_id),
        )
    _append_conversion_log(
        conversion_id, "warning", "cancel_requested", "用户请求取消模型转换任务"
    )
    audit(
        actor.id,
        "cancel",
        "model_conversion",
        conversion_id,
        _require_training_run(conversion["training_run_id"])["project_id"],
    )
    return serialize_conversion(_require_conversion(conversion_id))


@router.post("/conversions/{conversion_id}/retry", status_code=202)
def retry_conversion(
    conversion_id: int,
    payload: ConversionRetryCreate | None = None,
    actor: Actor = Depends(get_pipeline_actor),
) -> dict[str, Any]:
    preflight_conversion = _require_conversion(conversion_id)
    if preflight_conversion["status"] not in {"failed", "cancelled"}:
        raise HTTPException(409, "只有失败或已取消的转换任务可以重试")
    attempt = int(preflight_conversion.get("attempt") or 0)
    max_retries = int(preflight_conversion.get("max_retries") or 0)
    if attempt >= max_retries:
        raise HTTPException(409, f"该转换谱系已达到最大重试次数 {max_retries}")
    run = _require_training_run(preflight_conversion["training_run_id"])
    if run["status"] != "completed":
        raise HTTPException(409, "源训练任务及其模型产物不再可用")
    _validated_artifact(run.get("artifact_path"), "训练", _parse_json(run.get("artifact_metadata_json")))
    config = _parse_json(preflight_conversion["settings_json"])
    if payload and payload.device is not None:
        config["device"] = payload.device
    try:
        normalized = ConversionCreate.model_validate({**config, "format": preflight_conversion["format"], "max_retries": max_retries})
    except ValidationError as exc:
        raise HTTPException(422, "原转换配置不适用于当前转换工具，请使用新配置创建转换。") from exc
    resource = _validate_conversion_config(run, normalized, check_availability=False)
    config = normalized.model_dump()
    try:
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            locked_row = conn.execute(
                "SELECT * FROM model_conversions WHERE id = ?", (conversion_id,)
            ).fetchone()
            if not locked_row:
                raise HTTPException(404, "模型转换任务不存在")
            conversion = dict(locked_row)
            existing = conn.execute(
                "SELECT id FROM model_conversions WHERE parent_conversion_id = ?",
                (conversion_id,),
            ).fetchone()
            if existing:
                raise HTTPException(
                    409,
                    {
                        "message": "该转换任务已经创建过重试任务",
                        "existing_child_id": int(existing[0]),
                    },
                )
            if conversion["status"] not in {"failed", "cancelled"}:
                raise HTTPException(409, "只有失败或已取消的转换任务可以重试")
            attempt = int(conversion.get("attempt") or 0)
            max_retries = int(conversion.get("max_retries") or 0)
            if attempt >= max_retries:
                raise HTTPException(409, f"该转换谱系已达到最大重试次数 {max_retries}")
            if any(
                conversion[key] != preflight_conversion[key]
                for key in ("settings_json", "training_run_id", "format")
            ):
                raise HTTPException(409, "转换配置在重试校验期间发生变化，请重新请求")
            locked_run = conn.execute(
                "SELECT status, artifact_path, project_id FROM training_runs WHERE id = ?",
                (conversion["training_run_id"],),
            ).fetchone()
            if (
                not locked_run
                or locked_run["status"] != "completed"
                or locked_run["artifact_path"] != run.get("artifact_path")
            ):
                raise HTTPException(409, "源训练任务及其模型产物不再可用")
            root_conversion_id = conversion.get("root_conversion_id") or conversion_id
            created_at = now_iso()
            cursor = conn.execute(
                """INSERT INTO model_conversions
                   (training_run_id, format, status, settings_json, parent_conversion_id,
                    root_conversion_id, attempt, max_retries, resource_json, progress_message,
                    created_by, created_at)
                   VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    conversion["training_run_id"],
                    conversion["format"],
                    json.dumps(config, ensure_ascii=False),
                    conversion_id,
                    root_conversion_id,
                    attempt + 1,
                    max_retries,
                    json.dumps(resource, ensure_ascii=False),
                    "等待转换 worker",
                    actor.id,
                    created_at,
                ),
            )
            retry_id = int(cursor.lastrowid)
            conn.executemany(
                """INSERT INTO conversion_logs
                   (conversion_id, level, event, message, data_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    (
                        conversion_id,
                        "info",
                        "retry_created",
                        f"已创建转换重试任务 #{retry_id}",
                        json.dumps({"retry_conversion_id": retry_id}, ensure_ascii=False),
                        created_at,
                    ),
                    (
                        retry_id,
                        "info",
                        "queued",
                        f"从转换任务 #{conversion_id} 克隆配置并进入队列",
                        json.dumps(
                            {"parent_conversion_id": conversion_id}, ensure_ascii=False
                        ),
                        created_at,
                    ),
                ),
            )
            conn.execute(
                """INSERT INTO audit_logs
                   (actor, action, resource_type, resource_id, project_id, payload_json, created_at)
                   VALUES (?, 'retry', 'model_conversion', ?, ?, ?, ?)""",
                (
                    actor.id,
                    str(retry_id),
                    int(locked_run["project_id"]),
                    json.dumps(
                        {"parent_conversion_id": conversion_id, "attempt": attempt + 1},
                        ensure_ascii=False,
                    ),
                    created_at,
                ),
            )
    except sqlite3.IntegrityError as exc:
        existing = one(
            "SELECT id FROM model_conversions WHERE parent_conversion_id = ?",
            (conversion_id,),
        )
        if existing:
            raise HTTPException(
                409,
                {
                    "message": "该转换任务已经创建过重试任务",
                    "existing_child_id": existing["id"],
                },
            ) from exc
        raise
    return serialize_conversion(_require_conversion(retry_id))


@router.get("/conversions/{conversion_id}/artifact")
def download_conversion_artifact(
    conversion_id: int, _: Actor = Depends(get_actor)
) -> FileResponse:
    conversion = _require_conversion(conversion_id)
    if conversion["status"] != "completed":
        raise HTTPException(409, "模型转换尚未完成")
    path = _validated_artifact(conversion.get("artifact_path"), "转换", _parse_json(conversion.get("artifact_metadata_json")))
    suffix = CONVERSION_SUFFIXES[conversion["format"]]
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=f"conversion-{conversion_id}{suffix}",
    )


@router.get("/conversions/{conversion_id}/manifest")
def download_conversion_manifest(conversion_id: int, _: Actor = Depends(get_actor)) -> JSONResponse:
    conversion = _require_conversion(conversion_id)
    metadata = _parse_json(conversion.get("artifact_metadata_json"))
    if conversion["status"] != "completed":
        raise HTTPException(409, "模型转换尚未完成")
    if not metadata.get("deployment"):
        raise HTTPException(404, "此历史转换没有记录部署说明，请重新转换以生成。")
    _validated_artifact(conversion.get("artifact_path"), "转换", metadata)
    return JSONResponse({"conversion_id": conversion_id, **metadata}, headers={"Content-Disposition": f'attachment; filename="conversion-{conversion_id}.manifest.json"'})


def _claim_next(table: str, worker_id: str) -> tuple[int, int, bool] | None:
    if table not in {"training_runs", "model_conversions"}:
        raise ValueError("invalid task table")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = now_iso()
        row = conn.execute(
            f"""SELECT id, status, claim_generation FROM {table}
                 WHERE (status = 'queued' AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                    OR (status = 'running' AND (lease_expires_at IS NULL OR lease_expires_at < ?))
                 ORDER BY created_at, id LIMIT 1"""
            ,
            (current, current),
        ).fetchone()
        if not row:
            return None
        task_id = int(row[0])
        old_generation = int(row[2])
        reclaimed = row[1] == "running"
        cursor = conn.execute(
            f"""UPDATE {table} SET status = 'running',
                 progress = CASE WHEN status = 'queued' THEN MAX(progress, 0.01) ELSE progress END,
                 claimed_by = ?, started_at = COALESCE(started_at, ?), error = NULL,
                 heartbeat_at = ?, lease_expires_at = ?, next_attempt_at = NULL,
                 claim_generation = claim_generation + 1,
                 progress_message = ?
                 WHERE id = ? AND claim_generation = ? AND ((status = 'queued'
                    AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                    OR (status = 'running' AND (lease_expires_at IS NULL OR lease_expires_at < ?)))""",
            (
                worker_id,
                current,
                current,
                _future_iso(settings.pipeline_lease_seconds),
                "已重新领取，准备恢复" if reclaimed else "worker 已领取任务",
                task_id,
                old_generation,
                current,
                current,
            ),
        )
        return (task_id, old_generation + 1, reclaimed) if cursor.rowcount == 1 else None


def _claim_next_pipeline(
    worker_id: str,
) -> tuple[str, int, int, bool] | None:
    """Claim the globally oldest runnable training or conversion task atomically."""

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = now_iso()
        row = conn.execute(
            """SELECT task_table, id, status, claim_generation FROM (
                 SELECT 'training_runs' AS task_table, id, status, claim_generation,
                        created_at
                   FROM training_runs
                  WHERE (status = 'queued' AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                     OR (status = 'running' AND (lease_expires_at IS NULL OR lease_expires_at < ?))
                 UNION ALL
                 SELECT 'model_conversions' AS task_table, id, status, claim_generation,
                        created_at
                   FROM model_conversions
                  WHERE (status = 'queued' AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                     OR (status = 'running' AND (lease_expires_at IS NULL OR lease_expires_at < ?))
               ) ORDER BY created_at, task_table, id LIMIT 1""",
            (current, current, current, current),
        ).fetchone()
        if not row:
            return None
        table = str(row[0])
        task_id = int(row[1])
        reclaimed = row[2] == "running"
        old_generation = int(row[3])
        cursor = conn.execute(
            f"""UPDATE {table} SET status = 'running',
                 progress = CASE WHEN status = 'queued' THEN MAX(progress, 0.01) ELSE progress END,
                 claimed_by = ?, started_at = COALESCE(started_at, ?), error = NULL,
                 heartbeat_at = ?, lease_expires_at = ?, next_attempt_at = NULL,
                 claim_generation = claim_generation + 1,
                 progress_message = ?
                 WHERE id = ? AND claim_generation = ? AND ((status = 'queued'
                    AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                    OR (status = 'running' AND (lease_expires_at IS NULL OR lease_expires_at < ?)))""",
            (
                worker_id,
                current,
                current,
                _future_iso(settings.pipeline_lease_seconds),
                "已重新领取，准备恢复" if reclaimed else "worker 已领取任务",
                task_id,
                old_generation,
                current,
                current,
            ),
        )
        if cursor.rowcount != 1:
            return None
        return table, task_id, old_generation + 1, reclaimed


def _future_iso(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


class _LeaseHeartbeat:
    def __init__(
        self, table: str, task_id: int, worker_id: str, claim_generation: int
    ) -> None:
        self.table = table
        self.task_id = task_id
        self.worker_id = worker_id
        self.claim_generation = claim_generation
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)

    def _run(self) -> None:
        interval = max(5.0, settings.pipeline_lease_seconds / 3)
        while not self.stop_event.wait(interval):
            try:
                with db() as conn:
                    cursor = conn.execute(
                        f"""UPDATE {self.table} SET heartbeat_at = ?, lease_expires_at = ?
                           WHERE id = ? AND status = 'running' AND claimed_by = ?
                             AND claim_generation = ?""",
                        (
                            now_iso(),
                            _future_iso(settings.pipeline_lease_seconds),
                            self.task_id,
                            self.worker_id,
                            self.claim_generation,
                        ),
                    )
                    task_type = "training" if self.table == "training_runs" else "conversion"
                    conn.execute(
                        """UPDATE pipeline_resource_leases
                           SET heartbeat_at = ?, expires_at = ?
                           WHERE task_type = ? AND task_id = ? AND worker_id = ?
                             AND claim_generation = ?""",
                        (
                            now_iso(),
                            _future_iso(settings.pipeline_lease_seconds),
                            task_type,
                            self.task_id,
                            self.worker_id,
                            self.claim_generation,
                        ),
                    )
                    conn.execute(
                        """UPDATE workers SET last_seen_at = ?
                           WHERE id = ? AND status = 'busy'""",
                        (now_iso(), self.worker_id),
                    )
                if cursor.rowcount != 1:
                    return
            except Exception:
                logger.exception("Pipeline lease heartbeat failed for %s %s", self.table, self.task_id)


def _run_with_lease(
    table: str,
    task_id: int,
    worker_id: str,
    claim_generation: int,
    runner: Any,
) -> None:
    heartbeat = _LeaseHeartbeat(table, task_id, worker_id, claim_generation)
    heartbeat.start()
    try:
        runner(task_id, worker_id, claim_generation)
    finally:
        heartbeat.stop()
        from .providers.execution import release_unused_cuda_memory
        release_unused_cuda_memory()
        task_type = "training" if table == "training_runs" else "conversion"
        with db() as conn:
            conn.execute(
                """DELETE FROM pipeline_resource_leases
                   WHERE task_type = ? AND task_id = ? AND worker_id = ?
                     AND claim_generation = ?""",
                (task_type, task_id, worker_id, claim_generation),
            )
        _release_file_resource_lock(task_type, task_id, claim_generation)


class ResourceBusy(RuntimeError):
    pass


_RESOURCE_FILE_LOCKS: dict[tuple[str, int, int], Any] = {}
_RESOURCE_FILE_LOCKS_GUARD = threading.Lock()


def _try_file_resource_lock(
    resource_id: str, task_type: str, task_id: int, claim_generation: int
) -> Any | None:
    lock_dir = settings.training_dir / "resource-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    from .providers.execution import device_lock_name
    handle = (lock_dir / f"{device_lock_name(resource_id)}.lock").open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    key = (task_type, task_id, claim_generation)
    with _RESOURCE_FILE_LOCKS_GUARD:
        _RESOURCE_FILE_LOCKS[key] = handle
    return handle


def _release_file_resource_lock(
    task_type: str, task_id: int, claim_generation: int
) -> None:
    key = (task_type, task_id, claim_generation)
    with _RESOURCE_FILE_LOCKS_GUARD:
        handle = _RESOURCE_FILE_LOCKS.pop(key, None)
    if handle is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _acquire_resource_lease(
    requested_device: str,
    resource_profile: str,
    task_type: str,
    task_id: int,
    worker_id: str,
    claim_generation: int,
) -> dict[str, Any]:
    snapshot = training_resources()
    gpu_devices = [
        device for device in snapshot["devices"]
        if device["type"] == "cuda" and device["available"]
    ]
    normalized = "cuda:0" if requested_device == "cuda" else requested_device
    if normalized == "cpu":
        candidates = [snapshot["cpu"]]
    elif normalized.startswith("cuda:"):
        candidates = [device for device in gpu_devices if device["id"] == normalized]
        if not candidates:
            raise RuntimeError(f"请求的 CUDA 设备 {normalized} 已不可用")
    else:
        if resource_profile == "low_memory":
            candidates = sorted(
                gpu_devices,
                key=lambda item: (item.get("free_memory_bytes") or 0)
                / max(1, item.get("total_memory_bytes") or 1),
                reverse=True,
            )
        elif resource_profile == "balanced":
            candidates = sorted(
                gpu_devices,
                key=lambda item: (
                    (item.get("free_memory_bytes") or 0)
                    / max(1, item.get("total_memory_bytes") or 1)
                    - (item.get("utilization_percent") or 0) / 100
                ),
                reverse=True,
            )
        else:
            candidates = sorted(
                gpu_devices,
                key=lambda item: item.get("free_memory_bytes") or 0,
                reverse=True,
            )
        if not candidates:
            if resource_profile == "performance":
                raise RuntimeError("performance 资源策略要求 CUDA GPU，但当前没有可用设备")
            candidates = [snapshot["cpu"]]

    selected: dict[str, Any] | None = None
    for device in candidates:
        file_lock = _try_file_resource_lock(
            device["id"], task_type, task_id, claim_generation
        )
        if file_lock is None:
            continue
        try:
            with db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "DELETE FROM pipeline_resource_leases WHERE expires_at < ?", (now_iso(),)
                )
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO pipeline_resource_leases
                       (resource_id, worker_id, task_type, task_id, claim_generation,
                        leased_at, heartbeat_at, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        device["id"],
                        worker_id,
                        task_type,
                        task_id,
                        claim_generation,
                        now_iso(),
                        now_iso(),
                        _future_iso(settings.pipeline_lease_seconds),
                    ),
                )
        except Exception:
            _release_file_resource_lock(task_type, task_id, claim_generation)
            raise
        if cursor.rowcount == 1:
            selected = device
            break
        _release_file_resource_lock(task_type, task_id, claim_generation)
    if selected is None:
        raise ResourceBusy("GPU 正在处理标注、训练或推理任务，等待资源释放。")
    resolved_device = selected["id"]
    return {
        "requested_device": normalized,
        "resolved_device": resolved_device,
        "resource_profile": resource_profile,
        "state": "leased",
        "selected": selected,
        "snapshot_at": snapshot["snapshot_at"],
        "hostname": snapshot["hostname"],
        "worker_id": worker_id,
        "claim_generation": claim_generation,
        "lease_expires_at": _future_iso(settings.pipeline_lease_seconds),
    }


def _trainer_device(resource: dict[str, Any]) -> str:
    resolved = resource["resolved_device"]
    return resolved.split(":", 1)[1] if resolved.startswith("cuda:") else "cpu"


def _requeue_for_resource(
    table: str,
    task_id: int,
    worker_id: str,
    claim_generation: int,
    message: str,
) -> None:
    with db() as conn:
        conn.execute(
            f"""UPDATE {table} SET status = 'queued', claimed_by = NULL,
                 lease_expires_at = NULL, heartbeat_at = NULL, progress_message = ?,
                 next_attempt_at = ?
                 WHERE id = ? AND status = 'running' AND claimed_by = ?
                   AND claim_generation = ?""",
            (
                message[:500],
                _future_iso(max(2, int(settings.training_poll_seconds * 5))),
                task_id,
                worker_id,
                claim_generation,
            ),
        )


def _metric_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    try:
        if hasattr(value, "item"):
            numeric = float(value.item())
        else:
            numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    except (TypeError, ValueError):
        return str(value)


def _metrics(trainer: Any) -> dict[str, Any]:
    values = getattr(trainer, "metrics", None) or {}
    result = {str(key): _metric_value(value) for key, value in values.items()}
    fitness = getattr(trainer, "fitness", None)
    if fitness is not None:
        result["fitness"] = _metric_value(fitness)
    losses = getattr(trainer, "tloss", None)
    if losses is not None:
        try:
            result["train_loss"] = [_metric_value(value) for value in losses]
        except TypeError:
            result["train_loss"] = _metric_value(losses)
        label_losses = getattr(trainer, "label_loss_items", None)
        if callable(label_losses):
            # The trainer owns component names: newer segmentation/pose models
            # may include sem_loss/rle_loss, and classification is scalar.
            labeled = label_losses(losses, prefix="train")
            if isinstance(labeled, dict):
                result.update({str(key): _metric_value(value) for key, value in labeled.items()})
    return result


def _safe_metric_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _safe_metric_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_metric_json(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _loss_summary(metrics: dict[str, Any]) -> dict[str, Any]:
    def numeric(value):
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None

    def split(prefix):
        components = {
            key.split("/", 1)[1]: numeric(value)
            for key, value in metrics.items()
            if key.startswith(prefix + "/") and (key.endswith("_loss") or key.endswith("/loss"))
        }
        if not components:
            legacy = metrics.get(f"{prefix}_loss")
            if isinstance(legacy, list):
                # The position of old unlabeled entries is known, their
                # semantic names are not. Never guess task-specific names.
                components = {f"component_{index + 1}": numeric(value) for index, value in enumerate(legacy)}
            elif legacy is not None:
                components = {"loss": numeric(legacy)}
        if "loss" in components:
            total = components["loss"]
        elif components and all(value is not None for value in components.values()):
            total = sum(components.values())
            if not math.isfinite(total):
                total = None
        else:
            total = None
        return total, components

    train, train_components = split("train")
    val, val_components = split("val")
    return {"train": train, "val": val, "train_components": train_components,
            "val_components": val_components, "aggregation": "sum_of_recorded_components"}


def _trim_epoch_history(run_id: int, worker_id: str, claim_generation: int, last_epoch: int) -> None:
    """Keep only the curve prefix represented by the checkpoint being resumed."""
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        run = conn.execute("""SELECT total_epochs FROM training_runs WHERE id=? AND status='running'
            AND claimed_by=? AND claim_generation=?""", (run_id, worker_id, claim_generation)).fetchone()
        if not run:
            return
        conn.execute("DELETE FROM training_metrics WHERE run_id=? AND epoch>?", (run_id, last_epoch))
        latest = conn.execute("SELECT * FROM training_metrics WHERE run_id=? ORDER BY epoch DESC LIMIT 1", (run_id,)).fetchone()
        conn.execute("UPDATE training_runs SET current_epoch=?,progress=?,metrics_json=? WHERE id=?",
                     (last_epoch, min(0.99, max(0.01, last_epoch / max(1, run["total_epochs"]))),
                      latest["metrics_json"] if latest else "{}", run_id))


def _prepare_training_dataset(
    run_id: int, version_id: int, run_dir: Path, task: str
) -> Path:
    archive_path = run_dir / "dataset.zip"
    dataset_dir = run_dir / "dataset"
    build_version_export(version_id, "yolo", archive_path, yolo_task=task)
    dataset_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        dataset_root = dataset_dir.resolve()
        for member in archive.infolist():
            target = (dataset_dir / member.filename).resolve()
            if target != dataset_root and dataset_root not in target.parents:
                raise RuntimeError("数据集归档包含不安全路径")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
    archive_path.unlink(missing_ok=True)
    if task == "classify":
        return dataset_dir.resolve()
    yaml_path = dataset_dir / "data.yaml"
    yaml_text = yaml_path.read_text(encoding="utf-8")
    yaml_path.write_text(
        yaml_text.replace(
            "path: .", f"path: {json.dumps(str(dataset_dir.resolve()))}", 1
        ),
        encoding="utf-8",
    )
    return yaml_path


def run_training(run_id: int, worker_id: str, claim_generation: int) -> None:
    run = _require_training_run(run_id)
    run_root = settings.training_dir / f"run-{run_id}"
    attempt_dir = run_root / f"attempt-{claim_generation}"
    config = _parse_json(run["settings_json"])
    try:
        resource = _acquire_resource_lease(
            config.get("device", "auto"),
            config.get("resource_profile", "balanced"),
            "training",
            run_id,
            worker_id,
            claim_generation,
        )
    except ResourceBusy as exc:
        _requeue_for_resource(
            "training_runs", run_id, worker_id, claim_generation, str(exc)
        )
        _append_training_log(
            run_id,
            "info",
            "resource_wait",
            str(exc),
            {"claim_generation": claim_generation},
        )
        return
    except Exception as exc:
        with db() as conn:
            cursor = conn.execute(
                """UPDATE training_runs SET status = 'failed', error = ?, finished_at = ?,
                   lease_expires_at = NULL, progress_message = '资源选择失败'
                   WHERE id = ? AND status = 'running' AND claimed_by = ?
                     AND claim_generation = ?""",
                (str(exc)[:2000], now_iso(), run_id, worker_id, claim_generation),
            )
        if cursor.rowcount == 1:
            _append_training_log(
                run_id, "error", "resource_failed", str(exc),
                {"exception_type": type(exc).__name__},
            )
        return
    try:
        run_root.mkdir(parents=True, exist_ok=True)
        attempt_dir.mkdir(parents=True, exist_ok=False)
        with db() as conn:
            cursor = conn.execute(
                """UPDATE training_runs SET resource_json = ?, progress_message = ?
                   WHERE id = ? AND status = 'running' AND claimed_by = ?
                     AND claim_generation = ?""",
                (
                    json.dumps(resource, ensure_ascii=False),
                    f"在 {resource['resolved_device']} 上准备数据集",
                    run_id,
                    worker_id,
                    claim_generation,
                ),
            )
        if cursor.rowcount != 1:
            return
        _append_training_log(
            run_id,
            "info",
            "resource_acquired",
            f"已选择训练设备 {resource['resolved_device']}",
            resource,
        )

        resolved_model = _resolve_base_model(run["model_arch"], run["task"])
        if not resolved_model:
            raise RuntimeError("基础模型已不可用或 checkpoint task 不匹配")
        base_model, model_metadata = resolved_model
        if model_metadata["sha256"] != config.get("base_model_sha256"):
            raise RuntimeError("基础模型 SHA-256 已变化，拒绝执行不可复现的训练")
        dataset_yaml = _prepare_training_dataset(
            run_id, run["version_id"], attempt_dir, run["task"]
        )
        _append_training_log(
            run_id,
            "info",
            "dataset_ready",
            "不可变数据版本已校验并构建训练数据集",
            {
                "version_id": run["version_id"],
                "version_content_hash": config.get("version_content_hash"),
                "task": run["task"],
                "attempt_dir": attempt_dir.name,
            },
        )

        # Pipeline workers render plots without a display. Backend auto-detection
        # can block on a stale X/Wayland session even when training itself is done.
        import matplotlib
        matplotlib.use("Agg", force=True)
        import torch
        import ultralytics
        from ultralytics import YOLO
        from ultralytics.engine import trainer as trainer_module
        from .plot_fonts import prepare_training_fonts
        prepare_training_fonts()

        resume_candidates = sorted(
            (
                path
                for path in run_root.glob("attempt-*/ultralytics/weights/last.pt")
                if attempt_dir not in path.parents
            ),
            key=lambda path: int(path.parents[2].name.split("-")[-1]),
            reverse=True,
        )
        legacy_last = run_root / "ultralytics" / "weights" / "last.pt"
        if legacy_last.is_file():
            resume_candidates.append(legacy_last)
        resume_source = next((path for path in resume_candidates if path.is_file()), None)
        resume_checkpoint: Path | None = None
        if resume_source and int(run.get("current_epoch") or 0) > 0:
            checkpoint = torch.load(resume_source, map_location="cpu", weights_only=False)
            resumed_without_optimizer = False
            if int(checkpoint.get("epoch", -1)) < 0:
                # Ultralytics strips optimizer state and marks epoch=-1 when a trainer
                # shuts down cleanly. The DB epoch is fenced and was committed only
                # after that same attempt completed the epoch, so it is the safe
                # recovery cursor for a weights-only continuation.
                checkpoint["epoch"] = int(run["current_epoch"]) - 1
                resumed_without_optimizer = checkpoint.get("optimizer") is None
            train_args = checkpoint.get("train_args") or checkpoint.get("args")
            if not isinstance(train_args, dict):
                raise RuntimeError("last.pt 缺少可恢复的训练参数")
            train_args.update(
                {
                    "project": str(attempt_dir),
                    "name": "ultralytics",
                    "exist_ok": True,
                    "data": str(dataset_yaml),
                    "epochs": config["epochs"],
                    "val": True,
                }
            )
            resume_epoch = int(checkpoint["epoch"]) + 1
            _trim_epoch_history(run_id, worker_id, claim_generation, resume_epoch)
            resume_checkpoint = attempt_dir / "resume.pt"
            torch.save(checkpoint, resume_checkpoint)
            model = YOLO(str(resume_checkpoint), task=run["task"], verbose=False)
            with db() as conn:
                conn.execute(
                    """UPDATE training_runs SET resume_count = resume_count + 1,
                       progress_message = '正在恢复 checkpoint'
                       WHERE id = ? AND claimed_by = ? AND claim_generation = ?""",
                    (run_id, worker_id, claim_generation),
                )
            _append_training_log(
                run_id,
                "warning",
                "resumed_without_optimizer" if resumed_without_optimizer else "resumed",
                (
                    f"从 epoch {resume_epoch} 的 last.pt 权重恢复；优化器状态已被框架剥离，将重新初始化"
                    if resumed_without_optimizer
                    else f"租约重领后从 epoch {resume_epoch} 的 last.pt 完整恢复"
                ),
                {
                    "source_attempt": resume_source.parents[2].name,
                    "claim_generation": claim_generation,
                    "optimizer_state_restored": not resumed_without_optimizer,
                },
            )
        else:
            _trim_epoch_history(run_id, worker_id, claim_generation, 0)
            if int(run.get("current_epoch") or 0) > 0:
                _append_training_log(
                    run_id,
                    "warning",
                    "restart_without_checkpoint",
                    "租约重领但未找到 last.pt，本次从基础权重重新开始",
                    {"claim_generation": claim_generation},
                )
            model = YOLO(str(base_model), task=run["task"], verbose=False)

        def on_train_batch_end(trainer: Any) -> None:
            state = one(
                """SELECT cancel_requested, status, claimed_by, claim_generation
                   FROM training_runs WHERE id = ?""",
                (run_id,),
            )
            if (
                not state
                or state["cancel_requested"]
                or state["status"] != "running"
                or state["claimed_by"] != worker_id
                or state["claim_generation"] != claim_generation
            ):
                trainer.stop = True

        epoch_awaiting_validation: int | None = None

        def on_train_epoch_end(trainer: Any) -> None:
            nonlocal epoch_awaiting_validation
            epoch_awaiting_validation = int(getattr(trainer, "epoch", -1)) + 1

        def on_fit_epoch_end(trainer: Any) -> None:
            nonlocal epoch_awaiting_validation
            current_epoch = int(getattr(trainer, "epoch", -1)) + 1
            if current_epoch <= 0 or current_epoch != epoch_awaiting_validation:
                # Ultralytics also calls this hook after evaluating best.pt.
                # Those metrics belong to the artifact, not to the last epoch.
                return
            epoch_awaiting_validation = None
            total_epochs = max(1, int(run["total_epochs"]))
            metrics = _metrics(trainer)
            progress = min(0.99, current_epoch / total_epochs)
            with db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                first_snapshot = conn.execute(
                    "SELECT 1 FROM training_metrics WHERE run_id = ? AND epoch = ?",
                    (run_id, current_epoch),
                ).fetchone() is None
                cursor = conn.execute(
                    """UPDATE training_runs SET current_epoch = ?, progress = ?,
                       metrics_json = ?, progress_message = ?
                       WHERE id = ? AND status = 'running' AND claimed_by = ?
                         AND claim_generation = ?""",
                    (
                        current_epoch,
                        progress,
                        json.dumps(metrics, ensure_ascii=False, allow_nan=False),
                        f"epoch {current_epoch}/{total_epochs}",
                        run_id,
                        worker_id,
                        claim_generation,
                    ),
                )
                if cursor.rowcount == 1:
                    conn.execute(
                        """INSERT INTO training_metrics
                           (run_id, epoch, progress, metrics_json, created_at)
                           VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(run_id, epoch) DO UPDATE SET
                             progress = excluded.progress,
                             metrics_json = excluded.metrics_json,
                             created_at = excluded.created_at""",
                        (
                            run_id,
                            current_epoch,
                            progress,
                            json.dumps(metrics, ensure_ascii=False, allow_nan=False),
                            now_iso(),
                        ),
                    )
            if cursor.rowcount == 1 and first_snapshot:
                _append_training_log(
                    run_id,
                    "info",
                    "epoch_end",
                    f"epoch {current_epoch}/{total_epochs} 完成",
                    {"epoch": current_epoch, "progress": progress, "metrics": metrics},
                )

        if run["task"] == "classify":
            class_labels = json.loads((dataset_yaml / "classes.json").read_text())
            def set_class_names(trainer):
                names = {item["index"]:item["name"] for item in class_labels}
                trainer.data["names"] = names
                trainer.model.names = names
                if getattr(trainer, "ema", None):
                    trainer.ema.ema.names = names
            model.add_callback("on_pretrain_routine_end", set_class_names)
        model.add_callback("on_train_batch_end", on_train_batch_end)
        model.add_callback("on_train_epoch_end", on_train_epoch_end)
        model.add_callback("on_fit_epoch_end", on_fit_epoch_end)
        original_check_amp = trainer_module.check_amp

        def check_amp_with_local_model(torch_model: Any) -> bool:
            original_yolo = ultralytics.YOLO

            def local_yolo(value: Any = "yolo26n.pt", *args: Any, **kwargs: Any) -> Any:
                if str(value) == "yolo26n.pt":
                    value = str(base_model)
                return original_yolo(value, *args, **kwargs)

            ultralytics.YOLO = local_yolo
            try:
                return bool(original_check_amp(torch_model))
            finally:
                ultralytics.YOLO = original_yolo

        trainer_module.check_amp = check_amp_with_local_model
        train_kwargs: dict[str, Any] = {
            "data": str(dataset_yaml),
            "epochs": config["epochs"],
            "imgsz": config["image_size"],
            "batch": config["batch_size"],
            "device": _trainer_device(resource),
            "workers": config["workers"],
            "patience": config["patience"],
            "val": True,
            "optimizer": config.get("optimizer", "auto"),
            "lr0": config.get("lr0", 0.01),
            "weight_decay": config.get("weight_decay", 0.0005),
            "seed": config.get("seed", 0),
            "deterministic": config.get("deterministic", True),
            "amp": config.get("amp", True),
            "cache": config.get("cache", False),
            "save_period": config.get("save_period", -1),
            "cos_lr": config.get("cos_lr", False),
            "close_mosaic": config.get("close_mosaic", 10),
            "project": str(attempt_dir),
            "name": "ultralytics",
            "exist_ok": True,
            "verbose": False,
            "plots": True,
        }
        if run["task"] == "pose":
            version = one("SELECT keypoint_schema_json FROM dataset_versions WHERE id=?", (run["version_id"],))
            if not _parse_json(version["keypoint_schema_json"]).get("flip_idx"):
                train_kwargs["fliplr"] = 0.0
        if config.get("freeze") is not None:
            train_kwargs["freeze"] = config["freeze"]
        if resume_checkpoint:
            train_kwargs["resume"] = str(resume_checkpoint)
        try:
            model.train(**train_kwargs)
        finally:
            trainer_module.check_amp = original_check_amp

        state = one(
            """SELECT cancel_requested, claimed_by, claim_generation
               FROM training_runs WHERE id = ?""",
            (run_id,),
        )
        if (
            state
            and state["cancel_requested"]
            and state["claimed_by"] == worker_id
            and state["claim_generation"] == claim_generation
        ):
            with db() as conn:
                cursor = conn.execute(
                    """UPDATE training_runs SET status = 'cancelled', finished_at = ?,
                       progress = MIN(progress, 0.99), lease_expires_at = NULL,
                       progress_message = '已取消'
                       WHERE id = ? AND status = 'running' AND claimed_by = ?
                         AND claim_generation = ?""",
                    (now_iso(), run_id, worker_id, claim_generation),
                )
            if cursor.rowcount == 1:
                _append_training_log(run_id, "warning", "cancelled", "训练任务已安全停止")
            return
        best = Path(getattr(model.trainer, "best", ""))
        if not best.is_file():
            best = attempt_dir / "ultralytics" / "weights" / "best.pt"
        if not best.is_file():
            raise RuntimeError("训练结束但未生成 best.pt")
        artifact = attempt_dir / "best.pt"
        shutil.copyfile(best, artifact)
        artifact.chmod(0o444)
        metrics = _metrics(model.trainer)
        final_epoch = int(getattr(model.trainer, "epoch", -1)) + 1
        artifact_metadata = _build_artifact_metadata(
            artifact,
            format="pytorch",
            media_type="application/octet-stream",
            framework="ultralytics",
            framework_version=config.get("backend_version"),
            model_arch=run["model_arch"],
            task=run["task"],
            project_id=run["project_id"],
            dataset_version_id=run["version_id"],
            dataset_content_hash=config.get("version_content_hash"),
            config_hash=run.get("config_hash"),
            claim_generation=claim_generation,
        )
        with db() as conn:
            cursor = conn.execute(
                """UPDATE training_runs SET status = 'completed', progress = 1,
                   current_epoch = ?, metrics_json = ?, artifact_path = ?,
                   artifact_metadata_json = ?, finished_at = ?, lease_expires_at = NULL,
                   progress_message = '训练完成'
                   WHERE id = ? AND status = 'running' AND claimed_by = ?
                     AND claim_generation = ?""",
                (
                    final_epoch,
                    json.dumps(metrics, ensure_ascii=False, allow_nan=False),
                    str(artifact),
                    json.dumps(artifact_metadata, ensure_ascii=False),
                    now_iso(),
                    run_id,
                    worker_id,
                    claim_generation,
                ),
            )
        if cursor.rowcount != 1:
            return
        _append_training_log(
            run_id,
            "info",
            "completed",
            "训练完成并生成已校验的 best.pt",
            {"artifact": artifact_metadata, "metrics": metrics},
        )
        audit(
            f"worker:{worker_id}",
            "complete",
            "training_run",
            run_id,
            run["project_id"],
            {"artifact": artifact_metadata, "metrics": metrics},
        )
    except Exception as exc:
        logger.exception("Training run %s failed", run_id)
        with db() as conn:
            cursor = conn.execute(
                """UPDATE training_runs SET status = 'failed', error = ?, finished_at = ?,
                   lease_expires_at = NULL, progress_message = '训练失败'
                   WHERE id = ? AND status = 'running' AND claimed_by = ?
                     AND claim_generation = ?""",
                (str(exc)[:2000], now_iso(), run_id, worker_id, claim_generation),
            )
        if cursor.rowcount == 1:
            _append_training_log(
                run_id,
                "error",
                "failed",
                str(exc),
                {"exception_type": type(exc).__name__, "claim_generation": claim_generation},
            )


def _check_conversion_claim(conversion_id: int, worker_id: str, generation: int) -> None:
    state = one("SELECT status,cancel_requested,claimed_by,claim_generation FROM model_conversions WHERE id=?", (conversion_id,))
    if not state or state["status"] != "running" or state["claimed_by"] != worker_id or state["claim_generation"] != generation:
        raise conversion_tools.ConversionStopped(lost_claim=True)
    if state["cancel_requested"]:
        raise conversion_tools.ConversionStopped()


def _cancel_conversion_claim(conversion_id: int, worker_id: str, generation: int) -> None:
    with db() as conn:
        cursor = conn.execute("""UPDATE model_conversions SET status='cancelled',finished_at=?,lease_expires_at=NULL,
            progress=MIN(progress,0.99),progress_message='已取消，未发布模型'
            WHERE id=? AND status='running' AND claimed_by=? AND claim_generation=?""", (now_iso(), conversion_id, worker_id, generation))
    if cursor.rowcount:
        _append_conversion_log(conversion_id, "warning", "cancelled", "已停止转换，未发布模型文件。")


def _conversion_stage(conversion_id: int, worker_id: str, generation: int, progress: float, message: str) -> None:
    _check_conversion_claim(conversion_id, worker_id, generation)
    with db() as conn:
        conn.execute("""UPDATE model_conversions SET progress=?,progress_message=?
            WHERE id=? AND status='running' AND claimed_by=? AND claim_generation=? AND cancel_requested=0""", (progress, message, conversion_id, worker_id, generation))
    _append_conversion_log(conversion_id, "info", "stage", message)


def run_conversion(
    conversion_id: int, worker_id: str, claim_generation: int
) -> None:
    conversion = _require_conversion(conversion_id)
    run = _require_training_run(conversion["training_run_id"])
    conversion_root = settings.training_dir / f"conversion-{conversion_id}"
    attempt_dir = conversion_root / f"attempt-{claim_generation}"
    config = _parse_json(conversion["settings_json"])
    try:
        resource = _acquire_resource_lease(
            "cpu" if conversion["format"] == "rknn" else config.get("device", "auto"),
            "performance" if conversion["format"] == "engine" or config.get("half") else "balanced",
            "conversion",
            conversion_id,
            worker_id,
            claim_generation,
        )
    except ResourceBusy as exc:
        _requeue_for_resource(
            "model_conversions", conversion_id, worker_id, claim_generation, str(exc)
        )
        _append_conversion_log(
            conversion_id,
            "info",
            "resource_wait",
            str(exc),
            {"claim_generation": claim_generation},
        )
        return
    except Exception as exc:
        with db() as conn:
            cursor = conn.execute(
                """UPDATE model_conversions SET status = 'failed', error = ?, finished_at = ?,
                   lease_expires_at = NULL, progress_message = '资源选择失败'
                   WHERE id = ? AND status = 'running' AND claimed_by = ?
                     AND claim_generation = ?""",
                (
                    str(exc)[:2000], now_iso(), conversion_id, worker_id, claim_generation,
                ),
            )
        if cursor.rowcount == 1:
            _append_conversion_log(
                conversion_id, "error", "resource_failed", str(exc),
                {"exception_type": type(exc).__name__},
            )
        return
    try:
        source = _validated_artifact(run.get("artifact_path"), "训练", _parse_json(run.get("artifact_metadata_json")))
        conversion_root.mkdir(parents=True, exist_ok=True)
        attempt_dir.mkdir(parents=True, exist_ok=False)
        with db() as conn:
            cursor = conn.execute(
                """UPDATE model_conversions SET resource_json = ?, progress = 0.1,
                   progress_message = ?
                   WHERE id = ? AND status = 'running' AND claimed_by = ?
                     AND claim_generation = ?""",
                (
                    json.dumps(resource, ensure_ascii=False),
                    f"在 {resource['resolved_device']} 上转换模型",
                    conversion_id,
                    worker_id,
                    claim_generation,
                ),
            )
        if cursor.rowcount != 1:
            return
        _append_conversion_log(
            conversion_id,
            "info",
            "resource_acquired",
            f"已选择转换设备 {resource['resolved_device']}",
            resource,
        )
        state = one(
            """SELECT cancel_requested, claimed_by, claim_generation
               FROM model_conversions WHERE id = ?""",
            (conversion_id,),
        )
        if not state or state["cancel_requested"]:
            with db() as conn:
                conn.execute(
                    """UPDATE model_conversions SET status = 'cancelled', finished_at = ?,
                       lease_expires_at = NULL, progress_message = '已取消'
                       WHERE id = ? AND claimed_by = ? AND claim_generation = ?""",
                    (now_iso(), conversion_id, worker_id, claim_generation),
                )
            return
        _check_conversion_claim(conversion_id, worker_id, claim_generation)
        local_source = attempt_dir / "source.pt"
        shutil.copyfile(source, local_source)
        artifact = attempt_dir / f"model{CONVERSION_SUFFIXES[conversion['format']]}"
        check = lambda: _check_conversion_claim(conversion_id, worker_id, claim_generation)
        log = lambda line: _append_conversion_log(conversion_id, "info", "converter_output", line, {"claim_generation": claim_generation})
        kwargs: dict[str, Any] = {
            "format": conversion["format"], "imgsz": config["image_size"],
            "dynamic": config.get("dynamic", False), "half": config.get("half", False),
            "batch": config.get("batch", 1), "device": _trainer_device(resource),
        }
        if conversion["format"] in {"onnx", "engine"}:
            kwargs["simplify"] = config.get("simplify", True)
            if config.get("dynamic"):
                # End-to-end YOLO heads can trace a fixed TopK at the example
                # size. Smaller dynamic inputs then have fewer candidates than
                # K and fail at inference. Export decoded outputs and let the
                # deployment application apply filtering/NMS at each size.
                kwargs.update(end2end=False, nms=False)
        if conversion["format"] == "engine":
            kwargs["workspace"] = config.get("workspace")
        calibration = None
        if conversion["format"] == "rknn":
            rknn = config["rknn"]
            preprocessing = conversion_tools.preprocessing_contract(run["task"], config["image_size"], rknn)
            dataset = None
            if rknn["quantized"]:
                _conversion_stage(conversion_id, worker_id, claim_generation, 0.15, "正在准备冻结训练集的校准图片")
                dataset, calibration = conversion_tools.prepare_calibration(run["version_id"], run["task"], config["image_size"], rknn["calibration_count"], attempt_dir, check)
            onnx_path = attempt_dir / "intermediate.onnx"
            kwargs.update(format="onnx", device="cpu", half=False, dynamic=False, batch=1, nms=False, end2end=False, opset=12, simplify=config.get("simplify", True))
            _conversion_stage(conversion_id, worker_id, claim_generation, 0.25, "正在导出 RKNN 使用的静态 ONNX 模型")
            conversion_tools.run_converter_process(sys.executable, {"operation": "ultralytics", "source": str(local_source.resolve()), "output": str(onnx_path.resolve()), "task": run["task"], "kwargs": kwargs, "preprocessing": preprocessing}, attempt_dir, "onnx", check, log)
            _conversion_stage(conversion_id, worker_id, claim_generation, 0.5, "正在量化并构建 RKNN 模型" if rknn["quantized"] else "正在构建 RKNN 浮点模型")
            result = conversion_tools.run_converter_process(conversion_tools.rknn_python(), {"operation": "rknn", "source": str(onnx_path.resolve()), "output": str(artifact.resolve()), "task": run["task"], "rknn": rknn, "dataset_path": str(dataset.resolve()) if dataset else None, "preprocessing": preprocessing}, attempt_dir, "rknn", check, log)
        else:
            _conversion_stage(conversion_id, worker_id, claim_generation, 0.25, "正在构建 TensorRT 推理引擎" if conversion["format"] == "engine" else "正在导出模型")
            result = conversion_tools.run_converter_process(sys.executable, {"operation": "ultralytics", "source": str(local_source.resolve()), "output": str(artifact.resolve()), "task": run["task"], "kwargs": kwargs}, attempt_dir, "export", check, log)
        if Path(result["artifact"]).resolve() != artifact.resolve():
            raise RuntimeError("转换工具返回了意外的模型文件。")
        deployment = result.get("metadata", {})
        deployment["calibration"] = calibration
        deployment["labels"] = all_rows("SELECT category_index,name FROM dataset_version_labels WHERE version_id=? ORDER BY category_index", (run["version_id"],))
        deployment["version_id"] = run["version_id"]
        version = one("SELECT * FROM dataset_versions WHERE id=?", (run["version_id"],)) or {}
        deployment["version_content_hash"] = version.get("content_hash")
        deployment["keypoint_schema"] = _parse_json(version.get("keypoint_schema_json"))
        artifact.chmod(0o444)
        state = one(
            """SELECT cancel_requested, claimed_by, claim_generation
               FROM model_conversions WHERE id = ?""",
            (conversion_id,),
        )
        if (
            not state
            or state["claimed_by"] != worker_id
            or state["claim_generation"] != claim_generation
        ):
            return
        if state["cancel_requested"]:
            with db() as conn:
                cursor = conn.execute(
                    """UPDATE model_conversions SET status = 'cancelled', finished_at = ?,
                       lease_expires_at = NULL, progress = MIN(progress, 0.99),
                       progress_message = '已取消'
                       WHERE id = ? AND status = 'running' AND claimed_by = ?
                         AND claim_generation = ?""",
                    (now_iso(), conversion_id, worker_id, claim_generation),
                )
            if cursor.rowcount == 1:
                _append_conversion_log(
                    conversion_id, "warning", "cancelled", "转换完成前收到取消请求，产物未发布"
                )
            return
        source_metadata = _parse_json(run.get("artifact_metadata_json"))
        artifact_metadata = _build_artifact_metadata(
            artifact,
            format=conversion["format"],
            media_type="application/octet-stream",
            task=run["task"],
            training_run_id=run["id"],
            source_sha256=source_metadata.get("sha256"),
            source_config_hash=run.get("config_hash"),
            claim_generation=claim_generation,
            settings=config,
            deployment=deployment,
        )
        with db() as conn:
            cursor = conn.execute(
                """UPDATE model_conversions SET status = 'completed', progress = 1,
                   artifact_path = ?, artifact_metadata_json = ?, finished_at = ?,
                   lease_expires_at = NULL, progress_message = '转换完成'
                   WHERE id = ? AND status = 'running' AND claimed_by = ?
                     AND claim_generation = ? AND cancel_requested = 0""",
                (
                    str(artifact),
                    json.dumps(artifact_metadata, ensure_ascii=False),
                    now_iso(),
                    conversion_id,
                    worker_id,
                    claim_generation,
                ),
            )
        if cursor.rowcount != 1:
            _check_conversion_claim(conversion_id, worker_id, claim_generation)
            return
        _append_conversion_log(
            conversion_id,
            "info",
            "completed",
            f"{conversion['format']} 转换完成",
            {"artifact": artifact_metadata},
        )
        audit(
            f"worker:{worker_id}",
            "complete",
            "model_conversion",
            conversion_id,
            run["project_id"],
            {"format": conversion["format"], "artifact": artifact_metadata},
        )
    except conversion_tools.ConversionStopped as exc:
        if not exc.lost_claim:
            _cancel_conversion_claim(conversion_id, worker_id, claim_generation)
    except Exception as exc:
        current = one("SELECT cancel_requested FROM model_conversions WHERE id=?", (conversion_id,))
        if current and current["cancel_requested"]:
            _cancel_conversion_claim(conversion_id, worker_id, claim_generation)
            return
        logger.exception("Conversion %s failed", conversion_id)
        with db() as conn:
            cursor = conn.execute(
                """UPDATE model_conversions SET status = 'failed', error = ?, finished_at = ?,
                   lease_expires_at = NULL, progress_message = '转换失败'
                   WHERE id = ? AND status = 'running' AND claimed_by = ?
                     AND claim_generation = ?""",
                (
                    str(exc)[:2000],
                    now_iso(),
                    conversion_id,
                    worker_id,
                    claim_generation,
                ),
            )
        if cursor.rowcount == 1:
            _append_conversion_log(
                conversion_id,
                "error",
                "failed",
                str(exc),
                {"exception_type": type(exc).__name__, "claim_generation": claim_generation},
            )


class PipelineWorker:
    def __init__(self, name: str = "pipeline") -> None:
        self.worker_id = f"{name}:{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        self._stop = asyncio.Event()
        self.started_at = now_iso()

    def stop(self) -> None:
        self._stop.set()

    def _touch(
        self,
        status: str,
        task_id: int | None,
        task_type: str | None = None,
        claim_generation: int | None = None,
    ) -> None:
        metadata = {
            "worker_type": "pipeline",
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "task_type": task_type,
            "claim_generation": claim_generation,
        }
        with db() as conn:
            conn.execute(
                """INSERT INTO workers
                   (id, started_at, last_seen_at, status, current_job_id, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     last_seen_at = excluded.last_seen_at,
                     status = excluded.status,
                     current_job_id = excluded.current_job_id,
                     metadata_json = excluded.metadata_json""",
                (
                    self.worker_id,
                    self.started_at,
                    now_iso(),
                    status,
                    task_id,
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )

    async def run_forever(self) -> None:
        self._touch("idle", None)
        try:
            while not self._stop.is_set():
                try:
                    claim = _claim_next_pipeline(self.worker_id)
                    if claim is not None:
                        table, task_id, generation, reclaimed = claim
                        task_type = "training" if table == "training_runs" else "conversion"
                        self._touch("busy", task_id, task_type, generation)
                        if table == "training_runs":
                            _append_training_log(
                                task_id,
                                "warning" if reclaimed else "info",
                                "lease_reclaimed" if reclaimed else "claimed",
                                (
                                    "旧租约失效，任务已由新 fencing generation 重领"
                                    if reclaimed
                                    else "训练任务已被 pipeline worker 领取"
                                ),
                                {"worker_id": self.worker_id, "claim_generation": generation},
                            )
                            runner = run_training
                        else:
                            _append_conversion_log(
                                task_id,
                                "warning" if reclaimed else "info",
                                "lease_reclaimed" if reclaimed else "claimed",
                                (
                                    "旧租约失效，转换已由新 fencing generation 重领"
                                    if reclaimed
                                    else "转换任务已被 pipeline worker 领取"
                                ),
                                {"worker_id": self.worker_id, "claim_generation": generation},
                            )
                            runner = run_conversion
                        await asyncio.to_thread(
                            _run_with_lease,
                            table,
                            task_id,
                            self.worker_id,
                            generation,
                            runner,
                        )
                        self._touch("idle", None)
                        # A busy GPU requeues the task. Back off before reclaiming
                        # it so waiting does not flood logs or consume generations.
                        current = one(f"SELECT status FROM {table} WHERE id=?", (task_id,))
                        if current and current["status"] == "queued":
                            try:
                                await asyncio.wait_for(self._stop.wait(), timeout=settings.training_poll_seconds)
                            except TimeoutError:
                                pass
                        continue
                    self._touch("idle", None)
                    try:
                        await asyncio.wait_for(
                            self._stop.wait(), timeout=settings.training_poll_seconds
                        )
                    except TimeoutError:
                        pass
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Pipeline worker loop recovered from an unexpected failure")
                    self._touch("degraded", None)
                    try:
                        await asyncio.wait_for(
                            self._stop.wait(), timeout=max(1.0, settings.training_poll_seconds)
                        )
                    except TimeoutError:
                        pass
        finally:
            self._touch("offline", None)


async def _main() -> None:
    from .database import init_db

    settings.validate()
    init_db()
    worker = PipelineWorker("standalone")
    try:
        await worker.run_forever()
    finally:
        worker.stop()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
