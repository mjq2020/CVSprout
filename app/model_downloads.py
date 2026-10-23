"""Checksum-verified, resumable downloads from a maintained model catalog."""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import stat
import threading
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File

from .auth import Actor, get_actor, require_admin
from .config import BASE_DIR, settings

router = APIRouter(prefix="/api/model-downloads", tags=["models"])
logger = logging.getLogger(__name__)


class ModelDownloadCancelled(RuntimeError):
    """The consumer cancelled a download without discarding its partial file."""


def check_cancelled(cancelled=None):
    if cancelled is not None and cancelled():
        raise ModelDownloadCancelled("模型下载已取消，已下载的部分会保留供下次续传。")


def catalog():
    return json.loads((BASE_DIR / "model-catalog" / "models.json").read_text())


def digest(path, cancelled=None):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            check_cancelled(cancelled)
            h.update(block)
    return h.hexdigest()


def destination(relative):
    root = settings.models_dir.resolve()
    candidate = root / relative
    for component in (candidate, *candidate.parents):
        if component == root:
            break
        if component.is_symlink():
            raise ValueError("模型保存路径不能经过符号链接。")
    path = candidate.resolve()
    if root not in path.parents:
        raise ValueError("Model destination outside model directory")
    return path


def state_dir():
    path = destination(".downloads")
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_state(model_id, **value):
    value["updated_at"] = time.time()
    path = state_dir() / f"{model_id}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False))
    os.replace(temporary, path)


def download_file(item, progress, cancelled=None):
    check_cancelled(cancelled)
    path = destination(item["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        if path.stat().st_size == item["size"] and digest(path, cancelled) == item["sha256"]:
            progress(item["size"])
            return
        raise ValueError(f"{path.name} 已存在且内容不同；请先移走该文件，避免覆盖自己的权重。")
    if path.exists():
        raise ValueError(f"{path.name} 已存在且不是普通文件，请先检查模型目录。")
    partial = path.with_name(path.name + ".part")
    if partial.is_symlink() or (partial.exists() and not partial.is_file()):
        raise ValueError("模型续传文件不是普通文件，请先检查模型目录。")
    start = partial.stat().st_size if partial.exists() else 0
    if start > item["size"]:
        partial.unlink()
        start = 0
    if start < item["size"]:
        check_cancelled(cancelled)
        with httpx.stream("GET", item["url"], headers={"Range": f"bytes={start}-"} if start else {},
                          follow_redirects=True, timeout=60, trust_env=False,
                          proxy=os.getenv("MDP_DOWNLOAD_PROXY") or None) as response:
            response.raise_for_status()
            append = start > 0 and response.status_code == 206
            if append and not response.headers.get("content-range", "").startswith(f"bytes {start}-"):
                raise ValueError("下载服务器返回的续传位置不正确，请重试。")
            downloaded = start if append else 0
            flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_APPEND if append else os.O_TRUNC)
            with os.fdopen(os.open(partial, flags, 0o600), "ab" if append else "wb") as stream:
                for block in response.iter_bytes(1024 * 1024):
                    check_cancelled(cancelled)
                    downloaded += len(block)
                    if downloaded > item["size"]:
                        raise ValueError("下载文件超出清单记录大小")
                    stream.write(block)
                    progress(downloaded)
    check_cancelled(cancelled)
    if partial.stat().st_size != item["size"] or digest(partial, cancelled) != item["sha256"]:
        partial.unlink(missing_ok=True)
        raise ValueError("模型完整性校验失败，请重新下载。")
    try:
        check_cancelled(cancelled)
        os.link(partial, path)
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.stat().st_size != item["size"] or digest(path, cancelled) != item["sha256"]:
            raise ValueError(f"{path.name} 在下载期间被创建，已保留该文件；请重命名后重试。")
    partial.unlink(missing_ok=True)


def _download(model, handle):
    total = sum(item["size"] for item in model["files"])
    completed = 0
    try:
        for item in model["files"]:
            def progress(amount):
                write_state(model["id"], status="downloading", downloaded=completed + amount, total=total, error="")
            download_file(item, progress)
            completed += item["size"]
        write_state(model["id"], status="complete", downloaded=total, total=total, error="")
        from .providers import seed_builtin_models
        seed_builtin_models()
    except Exception as exc:
        try:
            write_state(model["id"], status="failed", downloaded=completed, total=total,
                        error=f"下载失败，可重试续传：{type(exc).__name__}: {str(exc)[:300]}")
        except (OSError, ValueError):
            # A full/read-only disk may also prevent writing the error state.
            # Releasing the lock lets readers report the abandoned download.
            logger.exception("Could not persist failed download state for %s", model["id"])
    finally:
        handle.close()


def _model_files_installed(model):
    if not model["files"]:
        return False
    for item in model["files"]:
        try:
            info = destination(item["path"]).stat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
            return False
    return True


def _download_snapshot(model):
    """Read existing files only, keeping filesystem failures local to a model."""
    state, installed = {}, False
    try:
        path = destination(f".downloads/{model['id']}.json")
        try:
            state = json.loads(path.read_text())
        except (FileNotFoundError, ValueError, UnicodeError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        installed = _model_files_installed(model)
        if state.get("status") == "downloading":
            lock_path = destination(f".downloads/{model['id']}.lock")
            try:
                # Read-only mounts must remain usable. Never create a missing
                # lock as part of a GET, and never replace the downloader inode.
                with lock_path.open("rb") as lock:
                    try:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass
                    else:
                        state.update(status="interrupted", error="上次下载已中断，点击下载继续。")
            except FileNotFoundError:
                state.update(status="interrupted", error="上次下载已中断，点击下载继续。")
        if not installed and state.get("status") == "complete":
            state.update(status="missing", error="模型文件已移走，请重新下载。")
        state.setdefault("status", "complete" if installed else "missing")
        state.setdefault("error", "")
    except (OSError, ValueError):
        state.update(status="failed", error="无法读取模型准备状态，请检查模型目录权限、磁盘空间及文件路径后重新检查。")
    return state, installed


def download_status(model):
    """Read progress without treating an abandoned downloader as still alive."""
    return _download_snapshot(model)[0]


@router.get("")
def list_downloads(actor: Actor = Depends(get_actor)):
    require_admin(actor)
    result = []
    for model in catalog():
        if model.get("download_policy") == "manual":
            from .providers import get_model
            service = get_model(model["annotation_model_id"])
            plan = service.get("preparation") if service else None
            result.append({key: value for key, value in model.items() if key != "files"} | {
                "size_bytes": 0, "installed": bool(plan and plan["installed"]),
                "state": plan["state"] if plan else {"status": "missing", "error": "请检查模型服务配置"},
                "preparation": plan, "availability_error": service.get("availability_error", "") if service else "",
            })
            continue
        state, installed = _download_snapshot(model)
        result.append({key: value for key, value in model.items() if key != "files"} | {
            "size_bytes": sum(item["size"] for item in model["files"]),
            "installed": installed, "state": state,
        })
    return result


@router.post("/{model_id}", status_code=202)
def start_download(model_id: str, actor: Actor = Depends(get_actor)):
    require_admin(actor)
    return queue_download(model_id)


def queue_download(model_id: str, *, idempotent: bool = False):
    model = next((item for item in catalog() if item["id"] == model_id), None)
    if model is None:
        raise HTTPException(404, "下载目录中没有这个模型")
    if model.get("download_policy") == "manual":
        raise HTTPException(409, "SAM 权重需自行下载并放到指定目录，请在标注模型服务中查看文件名并重新检查。")
    handle, handed_off = None, False
    try:
        handle = (state_dir() / f"{model_id}.lock").open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if idempotent:
                return {"id": model_id, "status": "downloading"}
            raise HTTPException(409, "该模型正在下载")
        write_state(model_id, status="downloading", downloaded=0, total=sum(f["size"] for f in model["files"]), error="")
        threading.Thread(target=_download, args=(model, handle), daemon=True).start()
        handed_off = True
        return {"id": model_id, "status": "downloading"}
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(503, "无法启动模型下载，请检查模型目录写入权限、磁盘空间和进程资源后重试。") from exc
    finally:
        if handle is not None and not handed_off:
            handle.close()


@router.post("/import/checkpoint", status_code=201)
async def import_checkpoint(file: UploadFile = File(...), actor: Actor = Depends(get_actor)):
    require_admin(actor)
    import re
    import tempfile
    filename = file.filename or ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.pt", filename):
        raise HTTPException(400, "请使用英文字母、数字、点、横线或下划线命名 .pt 模型文件。")
    directory = settings.models_dir / "training"
    directory.mkdir(parents=True, exist_ok=True)
    destination_path = directory / filename
    handle = tempfile.NamedTemporaryFile(dir=directory, suffix=".upload", delete=False)
    temporary = Path(handle.name)
    size = 0
    try:
        with handle:
            while block := await file.read(1024 * 1024):
                size += len(block)
                if size > 2 * 1024**3:
                    raise HTTPException(413, "模型文件不能超过 2 GiB")
                handle.write(block)
        if size == 0:
            raise HTTPException(400, "模型文件为空")
        try:
            os.link(temporary, destination_path)
        except FileExistsError:
            raise HTTPException(409, "同名模型已存在，请重命名后导入。")
        from .providers import seed_builtin_models
        seed_builtin_models()
        return {"filename": filename, "size_bytes": size, "sha256": digest(destination_path),
                "message": "已导入；训练目录会检查该权重是否可加载及其任务类型。"}
    finally:
        temporary.unlink(missing_ok=True)
