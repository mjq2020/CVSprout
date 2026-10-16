from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from .base import (
    AnnotationProvider,
    ModelInfo,
    Prediction,
    image_data_url,
    normalize_predictions,
    parse_json_response,
)


class QwenProvider(AnnotationProvider):
    def __init__(
        self,
        endpoint: str,
        model_name: str,
        api_key: str = "",
        timeout_seconds: int = 180,
        info: ModelInfo | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model_name = model_name
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.info = info or ModelInfo(
            id="qwen3.5-35b-a3b",
            name="Qwen3.5-35B-A3B",
            family="视觉语言模型",
            description="自然语言理解、框定位、属性和关系判断，适合复杂业务规则。",
            capabilities=["检测框", "属性", "关系", "场景分类", "视频理解"],
            modes=["auto", "assist"],
            configured=bool(self.endpoint),
            badge="推荐语义",
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
        if not self.endpoint:
            raise RuntimeError("QWEN_BASE_URL 未配置")
        hint_text = f"\n交互提示（原图像素坐标）：{hints}" if hints else ""
        task = (hints or {}).get("task_type", "detect")
        contracts = {
            "detect": '{"label":"类别名","bbox":[x1,y1,x2,y2],"confidence":0.9,"attributes":{}}',
            "segment": '{"label":"类别名","polygon":[x1,y1,x2,y2,x3,y3],"confidence":0.9,"attributes":{}}',
            "classify": '{"label":"类别名","shape_type":"classification","confidence":0.9,"attributes":{}}',
            "pose": '{"label":"类别名","bbox":[x1,y1,x2,y2],"keypoints":[{"x":100,"y":200,"visibility":2}],"confidence":0.9,"attributes":{}}',
        }
        instruction = f"""你是严格的图像数据标注器。请完整找出所有符合要求的目标，不要遗漏。
标注要求：{prompt}
允许的类别：{', '.join(labels)}
{hint_text}

只输出 JSON，不要输出解释或 Markdown。格式必须是：
{{"objects":[{contracts.get(task, contracts['detect'])}]}}
当前任务：{task}。分类时整张图只输出一个类别。
关键点时严格遵循提示中的 keypoint_schema.names 顺序，每个对象输出完整数量的点。
visibility：0=未标注（x=y=0），1=遮挡但可定位，2=可见。分割时使用完整轮廓多边形。
坐标按原图位置归一化到 0 到 1000。没有目标时输出 {{"objects":[]}}。
不得创造类别；不确定的目标不要输出。"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_data_url(image_path)}},
                        {"type": "text", "text": instruction},
                    ],
                }
            ],
            "temperature": 0,
            "max_tokens": 4096,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        endpoint = self.endpoint
        if not endpoint.endswith("/chat/completions"):
            endpoint = f"{endpoint}/chat/completions" if endpoint.endswith("/v1") else f"{endpoint}/v1/chat/completions"
        async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
            response = await client.post(endpoint, headers=headers, json=body)
            response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return normalize_predictions(
            parse_json_response(content), width, height, coordinate_space="normalized_1000", task_type=task
        )
