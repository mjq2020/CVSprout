from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx

from .base import AnnotationProvider, ModelInfo, Prediction, normalize_predictions


class RemoteVisionProvider(AnnotationProvider):
    def __init__(
        self,
        info: ModelInfo,
        endpoint: str,
        api_key: str = "",
        timeout_seconds: int = 180,
        extra_config: dict[str, Any] | None = None,
    ) -> None:
        self.info = info
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.extra_config = extra_config or {}

    async def predict(
        self,
        image_path: Path,
        width: int,
        height: int,
        prompt: str,
        labels: list[str],
        hints: dict[str, Any] | None = None,
    ) -> list[Prediction]:
        if not self.endpoint:
            raise RuntimeError(f"{self.info.name} 推理端点未配置")
        payload = {
            "image_base64": base64.b64encode(image_path.read_bytes()).decode("ascii"),
            "prompt": prompt,
            "labels": labels,
            "hints": hints or {},
            "config": self.extra_config,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        # Inference endpoints commonly live on a private network. Do not leak image payloads
        # through ambient HTTP_PROXY settings inherited by the worker process.
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.post(self.endpoint, json=payload, headers=headers)
            response.raise_for_status()
        body = response.json()
        coordinate_space = body.get("coordinate_space", "auto") if isinstance(body, dict) else "auto"
        return normalize_predictions(body, width, height, coordinate_space=coordinate_space,
                                     task_type=(hints or {}).get("task_type"))
