"""Task-aware local AI inference from administrator-managed checkpoints."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from .base import AnnotationProvider, Prediction
from .local import _device
from .execution import serialized_local_inference

_cache: dict[tuple, object] = {}
_lock = threading.Lock()


class LocalYoloProvider(AnnotationProvider):
    def __init__(self, info, path: str, task: str, device: str):
        self.info, self.path, self.task, self.device = info, path, task, device

    async def predict(self, image_path, width, height, prompt, labels, hints=None):
        return await asyncio.to_thread(self._predict, image_path, hints or {})

    @serialized_local_inference
    def _predict(self, image_path: Path, hints: dict):
        from ultralytics import YOLO

        path = Path(self.path)
        if not path.is_file():
            raise RuntimeError("本地 AI 权重不存在")
        with _lock:
            stat = path.stat()
            key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
            if key not in _cache:
                _cache.clear()
                _cache[key] = YOLO(str(path), verbose=False)
            model = _cache[key]
            if model.task != self.task:
                raise RuntimeError(f"checkpoint 实际任务为 {model.task}，与配置的 {self.task} 不一致")
            result = model.predict(source=str(image_path), device=_device(self.device), verbose=False,
                                   conf=min(0.25, float(hints.get("confidence_threshold", 0.35))))[0]
            if self.task == "classify":
                index = int(result.probs.top1)
                return [Prediction(str(result.names[index]), "classification", [], float(result.probs.top1conf))]
            predictions = []
            for index, box in enumerate(result.boxes):
                label = str(result.names[int(box.cls.item())])
                bbox = box.xyxy[0].cpu().tolist()
                confidence = float(box.conf.item())
                if self.task == "segment":
                    if result.masks is None:
                        continue
                    polygon = result.masks.xy[index].reshape(-1).tolist()
                    predictions.append(Prediction(label, "polygon", polygon, confidence))
                elif self.task == "pose":
                    raw = result.keypoints.data[index].cpu().tolist()
                    if len(raw) != len(hints.get("keypoint_schema", {}).get("names", [])):
                        raise RuntimeError("模型关键点数量与项目定义不一致；请使用相同关键点顺序的模型与项目")
                    points = []
                    for point in raw:
                        visible = len(point) < 3 or point[2] >= 0.5
                        points.append({"x":float(point[0]) if visible else 0,
                                       "y":float(point[1]) if visible else 0, "visibility":2 if visible else 0})
                    predictions.append(Prediction(label, "keypoints", bbox, confidence, keypoints=points))
                else:
                    predictions.append(Prediction(label, "rectangle", bbox, confidence))
            return predictions
