"""Pinned official training models and cancellable, shared-lock preparation.

Catalog reads do not import Ultralytics, load checkpoints, or access the network.
Only a worker executing an explicitly pretrained job calls the download helper.
"""
from __future__ import annotations

import fcntl
import os
import re
import time
from pathlib import Path
from typing import Callable

from . import model_downloads
from .config import settings


class TrainingModelDownloadCancelled(RuntimeError):
    """A cancelled training download keeps its verified-offset partial file."""


def builtin_training_models() -> list[dict]:
    result = []
    for entry in model_downloads.catalog():
        if entry.get("kind") != "training":
            continue
        files = entry.get("files", [])
        if len(files) != 1:
            raise ValueError("官方训练模型清单必须为每个模型指定一个权重文件。")
        item = files[0]
        filename = Path(item["path"]).name
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.pt", filename)
                or item["path"] != f"training/{filename}"
                or entry["id"] != filename[:-3]
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                or type(item["size"]) is not int or item["size"] <= 0
                or entry["architecture"] != filename[:-3] + ".yaml"
                or item["url"] != f"https://github.com/ultralytics/assets/releases/download/{entry['revision']}/{filename}"):
            raise ValueError("官方训练模型清单包含无效路径或校验信息。")
        result.append({
            "id": filename, "model_arch": filename, "download_id": entry["id"],
            "name": entry["name"], "family": entry["family"], "task": entry["task"],
            "architecture": entry["architecture"], "source": "builtin", "supports_scratch": True,
            "url": item["url"], "sha256": item["sha256"], "size_bytes": item["size"],
            "relative_path": item["path"], "path": settings.models_dir / item["path"],
            "revision": entry["revision"], "license": entry["license"],
        })
    return result


def get_builtin_training_model(filename: str) -> dict | None:
    return next((model for model in builtin_training_models() if model["model_arch"] == filename), None)


def ensure_pretrained_model(
    model: dict,
    progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Path:
    """Verify or prepare exactly the official model selected when the job was saved.

    Paths in a frozen job are never trusted. A changed/unknown snapshot fails
    explicitly instead of silently selecting a newer file or an arbitrary URL.
    The lock is shared with the administrator's model-download endpoint.
    """
    if not isinstance(model, dict):
        raise ValueError("官方训练模型记录无效。")
    trusted = get_builtin_training_model(model.get("model_arch"))
    frozen_fields = ("id", "model_arch", "download_id", "task", "architecture", "url", "sha256", "size_bytes", "relative_path")
    if trusted is None or any(model.get(key) != trusted[key] for key in frozen_fields):
        raise ValueError("任务保存的官方模型信息与受信任清单不一致，请重新创建训练任务；不会改用其他权重版本。")
    item = {"path": trusted["relative_path"], "url": trusted["url"], "sha256": trusted["sha256"], "size": trusted["size_bytes"]}
    total = item["size"]
    downloaded = 0
    locked = False
    handle = None
    try:
        model_downloads.check_cancelled(cancelled)
        if progress:
            progress(0, total)
        lock_path = model_downloads.state_dir() / f"{trusted['download_id']}.lock"
        handle = os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a+")
        while True:
            model_downloads.check_cancelled(cancelled)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError:
                time.sleep(0.1)
        model_downloads.check_cancelled(cancelled)

        def report(amount: int) -> None:
            nonlocal downloaded
            downloaded = amount
            model_downloads.write_state(trusted["download_id"], status="downloading", downloaded=amount, total=total, error="")
            if progress:
                progress(amount, total)

        model_downloads.download_file(item, report, cancelled=cancelled)
        model_downloads.write_state(trusted["download_id"], status="complete", downloaded=total, total=total, error="")
        return model_downloads.destination(item["path"])
    except model_downloads.ModelDownloadCancelled as error:
        if locked:
            model_downloads.write_state(trusted["download_id"], status="interrupted", downloaded=downloaded, total=total, error=str(error))
        raise TrainingModelDownloadCancelled(str(error)) from error
    except Exception as error:
        if locked:
            model_downloads.write_state(trusted["download_id"], status="failed", downloaded=downloaded, total=total,
                                        error=f"准备训练权重失败，可重试续传：{type(error).__name__}: {str(error)[:300]}")
        raise
    finally:
        if handle is not None:
            if locked:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
