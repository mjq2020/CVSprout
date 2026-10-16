"""Private, authenticated RPC between the lightweight web and a local worker.

The two services share a local data/model volume. This is deliberately not a
cross-host filesystem protocol; remote users deploy the stack on their GPU host.
"""
from __future__ import annotations

import os
import secrets
import json
from functools import wraps

import httpx
from fastapi import HTTPException

from .config import settings


def worker_token() -> str:
    if settings.worker_token:
        return settings.worker_token
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    path = settings.data_dir / ".worker-token"
    if not path.exists():
        temporary = path.with_name(f".worker-token-{secrets.token_hex(8)}")
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
                stream.write(secrets.token_urlsafe(48))
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
    return path.read_text().strip()


def _result(response: httpx.Response):
    if response.status_code == 409:
        from .training import ResourceBusy
        raise ResourceBusy(response.json().get("detail", "GPU 正在处理其他任务"))
    if response.is_error:
        raise HTTPException(503, "计算服务暂不可用，请检查 worker 状态和日志。")
    return response.json()


def call(operation: str, *args, **kwargs):
    try:
        response = httpx.post(
            f"{settings.worker_url}/internal/call/{operation}",
            headers={"X-VisionForge-Worker": worker_token()},
            json=json.loads(json.dumps({"args": args, "kwargs": kwargs}, default=str)), timeout=180, trust_env=False,
        )
        return _result(response)
    except httpx.HTTPError as exc:
        raise HTTPException(503, "无法连接计算服务，请检查 worker 是否已启动。") from exc


def on_worker(operation: str):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            if settings.worker_url:
                return call(operation, *args, **kwargs)
            return function(*args, **kwargs)
        return wrapped
    return decorate


class WorkerProvider:
    def __init__(self, info):
        self.info = info

    async def predict(self, image_path, width, height, prompt, labels, hints=None):
        from .providers.base import Prediction
        relative = image_path.resolve().relative_to(settings.media_dir.resolve())
        try:
            async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
                response = await client.post(
                    f"{settings.worker_url}/internal/predict",
                    headers={"X-VisionForge-Worker": worker_token()},
                    json={"model_id": self.info.id, "image": str(relative), "width": width,
                          "height": height, "prompt": prompt, "labels": labels, "hints": hints},
                )
            return [Prediction(**item) for item in _result(response)]
        except httpx.HTTPError as exc:
            raise HTTPException(503, "无法连接计算服务，请检查 worker 状态。") from exc
