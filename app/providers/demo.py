from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import AnnotationProvider, ModelInfo, Prediction


class DemoProvider(AnnotationProvider):
    info = ModelInfo(
        id="demo",
        name="开发演示模型",
        family="Demo",
        description="生成一条明确标记为演示数据的中心框，用于验证任务流程。",
        capabilities=["检测框"],
        modes=["auto", "assist"],
        configured=True,
        badge="仅演示",
    )

    async def predict(
        self,
        image_path: Path,
        width: int,
        height: int,
        prompt: str,
        labels: list[str],
        hints: dict[str, Any] | None = None,
    ) -> list[Prediction]:
        if not labels:
            return []
        margin_x, margin_y = width * 0.22, height * 0.18
        return [
            Prediction(
                label=labels[0],
                shape_type="rectangle",
                points=[margin_x, margin_y, width - margin_x, height - margin_y],
                confidence=0.51,
                attributes={"demo": True},
            )
        ]
