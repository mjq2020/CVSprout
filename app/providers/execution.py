"""Serialize cached local processors across batch jobs and interactive previews."""
from functools import wraps
import gc
import hashlib
import os
import sys
import threading
import uuid

_LOCAL_INFERENCE_LOCK = threading.Lock()


def local_inference_busy() -> bool:
    return _LOCAL_INFERENCE_LOCK.locked()


def device_lock_name(device: str) -> str:
    """Use a physical device identity across containers with different ordinals."""
    if not device.startswith("cuda"):
        return device.replace(":", "-")
    import torch
    index = int(device.split(":")[1]) if ":" in device else 0
    properties = torch.cuda.get_device_properties(index)
    identity = getattr(properties, "uuid", None)
    if identity is None:
        visible = os.environ.get("CUDA_VISIBLE_DEVICES", "").split(",")
        identity = visible[index] if visible != [""] and index < len(visible) else str(index)
    return "gpu-" + hashlib.sha256(str(identity).encode()).hexdigest()[:32]


def release_unused_cuda_memory():
    # Never import torch into the lightweight Web process just for cleanup.
    torch = sys.modules.get("torch")
    if torch is not None and hasattr(torch, "cuda") and torch.cuda.is_initialized():
        gc.collect()
        torch.cuda.empty_cache()


def unload_annotation_models():
    local = sys.modules.get("app.providers.local")
    yolo = sys.modules.get("app.providers.yolo")
    if local:
        with local._CACHE_LOCK:
            local._MODEL_CACHE.clear()
        with local._DEVICE_LOCK:
            local._MODEL_DEVICES.clear()
    if yolo:
        with yolo._lock:
            yolo._cache.clear()
    release_unused_cuda_memory()


def serialized_local_inference(function):
    @wraps(function)
    def run(*args, **kwargs):
        # Lock inside the worker thread. asyncio cancellation cannot release it
        # before native model inference finishes using shared processor state.
        with _LOCAL_INFERENCE_LOCK:
            from ..config import settings
            owner = args[0] if args and hasattr(args[0], "device") else None
            requested = owner.device if owner else None
            torch = sys.modules.get("torch")
            # Plain decorated functions are also used to serialize native calls.
            if owner is None or (torch is not None and not hasattr(torch, "cuda")):
                return function(*args, **kwargs)
            from .local import _device
            from ..training import ResourceBusy, _try_file_resource_lock, _release_file_resource_lock
            resolve = getattr(sys.modules.get(function.__module__), "_device", _device)
            resolved = resolve(requested)
            if resolved == "cpu":
                return function(*args, **kwargs)
            task_id = uuid.uuid4().int
            handle = _try_file_resource_lock(resolved, "inference", task_id, 0)
            if handle is None:
                raise ResourceBusy("GPU 正在处理其他任务。批量标注会等待；请稍后再预览。")
            owner.device = resolved
            try:
                return function(*args, **kwargs)
            finally:
                try:
                    if resolved.startswith("cuda") and settings.release_gpu_after_inference:
                        unload_annotation_models()
                finally:
                    owner.device = requested
                    _release_file_resource_lock("inference", task_id, 0)
    return run
