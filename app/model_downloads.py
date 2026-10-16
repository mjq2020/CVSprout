"""Checksum-verified, resumable downloads from a maintained model catalog."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File

from .auth import Actor, get_actor, require_admin
from .config import BASE_DIR, settings

router = APIRouter(prefix="/api/model-downloads", tags=["models"])


def catalog():
    return json.loads((BASE_DIR / "model-catalog" / "models.json").read_text())


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def destination(relative):
    root = settings.models_dir.resolve()
    path = (root / relative).resolve()
    if root not in path.parents:
        raise ValueError("Model destination outside model directory")
    return path


def state_dir():
    path = settings.models_dir / ".downloads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_state(model_id, **value):
    value["updated_at"] = time.time()
    path = state_dir() / f"{model_id}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False))
    os.replace(temporary, path)


def download_file(item, progress):
    path = destination(item["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        if digest(path) == item["sha256"]:
            progress(item["size"])
            return
        raise ValueError(f"{path.name} 已存在且内容不同；请先移走该文件，避免覆盖自己的权重。")
    partial = path.with_name(path.name + ".part")
    start = partial.stat().st_size if partial.exists() else 0
    if start > item["size"]:
        partial.unlink()
        start = 0
    if start < item["size"]:
        with httpx.stream("GET", item["url"], headers={"Range": f"bytes={start}-"} if start else {},
                          follow_redirects=True, timeout=60, trust_env=False,
                          proxy=os.getenv("MDP_DOWNLOAD_PROXY") or None) as response:
            response.raise_for_status()
            append = start > 0 and response.status_code == 206
            if append and not response.headers.get("content-range", "").startswith(f"bytes {start}-"):
                raise ValueError("下载服务器返回的续传位置不正确，请重试。")
            downloaded = start if append else 0
            with partial.open("ab" if append else "wb") as stream:
                for block in response.iter_bytes(1024 * 1024):
                    downloaded += len(block)
                    if downloaded > item["size"]:
                        raise ValueError("下载文件超出清单记录大小")
                    stream.write(block)
                    progress(downloaded)
    if partial.stat().st_size != item["size"] or digest(partial) != item["sha256"]:
        partial.unlink(missing_ok=True)
        raise ValueError("模型完整性校验失败，请重新下载。")
    try:
        os.link(partial, path)
    except FileExistsError:
        if not path.is_file() or digest(path) != item["sha256"]:
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
        write_state(model["id"], status="failed", downloaded=completed, total=total,
                    error=f"下载失败，可重试续传：{type(exc).__name__}: {str(exc)[:300]}")
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@router.get("")
def list_downloads(actor: Actor = Depends(get_actor)):
    require_admin(actor)
    result = []
    for model in catalog():
        path = state_dir() / f"{model['id']}.json"
        try:
            state = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, ValueError):
            state = {}
        if state.get("status") == "downloading":
            with (state_dir() / f"{model['id']}.lock").open("a+") as lock:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    state.update(status="interrupted", error="上次下载已中断，点击下载继续。")
                except BlockingIOError:
                    pass
        installed = all(destination(item["path"]).is_file() for item in model["files"])
        if not installed and state.get("status") == "complete":
            state.update(status="missing", error="模型文件已移走，请重新下载。")
        result.append({key: value for key, value in model.items() if key != "files"} | {
            "size_bytes": sum(item["size"] for item in model["files"]),
            "installed": installed, "state": state,
        })
    return result


@router.post("/{model_id}", status_code=202)
def start_download(model_id: str, actor: Actor = Depends(get_actor)):
    require_admin(actor)
    model = next((item for item in catalog() if item["id"] == model_id), None)
    if model is None:
        raise HTTPException(404, "下载目录中没有这个模型")
    handle = (state_dir() / f"{model_id}.lock").open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise HTTPException(409, "该模型正在下载")
    write_state(model_id, status="downloading", downloaded=0, total=sum(f["size"] for f in model["files"]), error="")
    threading.Thread(target=_download, args=(model, handle), daemon=True).start()
    return {"id": model_id, "status": "downloading"}


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
