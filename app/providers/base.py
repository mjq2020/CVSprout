from __future__ import annotations

import base64
import json
import math
import mimetypes
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Prediction:
    label: str
    shape_type: str
    points: list[float]
    confidence: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    keypoints: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ModelInfo:
    id: str
    name: str
    family: str
    description: str
    capabilities: list[str]
    modes: list[str]
    configured: bool
    badge: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AnnotationProvider(ABC):
    info: ModelInfo

    @abstractmethod
    async def predict(
        self,
        image_path: Path,
        width: int,
        height: int,
        prompt: str,
        labels: list[str],
        hints: dict[str, Any] | None = None,
    ) -> list[Prediction]:
        raise NotImplementedError


def image_data_url(image_path: Path, mime_type: str | None = None) -> str:
    mime_type = mime_type or mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def parse_json_response(text: str) -> Any:
    value = text.strip()
    if "</think>" in value:
        value = value.split("</think>", 1)[1].strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines)
    start_candidates = [pos for pos in (value.find("["), value.find("{")) if pos >= 0]
    if start_candidates:
        value = value[min(start_candidates) :]
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        # Some compatible servers append prose after the JSON value.
        decoder = json.JSONDecoder()
        parsed, _ = decoder.raw_decode(value)
        return parsed


def normalize_predictions(
    payload: Any,
    width: int,
    height: int,
    coordinate_space: str = "auto",
    task_type: str | None = None,
) -> list[Prediction]:
    if isinstance(payload, dict):
        objects = payload.get("objects", payload.get("annotations", []))
    else:
        objects = payload
    if not isinstance(objects, list):
        return []

    predictions: list[Prediction] = []
    for item in objects:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("class") or "").strip()
        if not label:
            continue
        confidence = item.get("confidence", item.get("score"))
        try:
            confidence = float(confidence) if confidence is not None else None
            confidence = (
                min(1.0, max(0.0, confidence))
                if confidence is not None and math.isfinite(confidence)
                else None
            )
        except (TypeError, ValueError):
            confidence = None
        attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
        if task_type == "classify" or item.get("shape_type") == "classification":
            predictions.append(Prediction(label, "classification", [], confidence, attrs))
            continue

        polygon = item.get("polygon")
        if isinstance(polygon, list) and polygon:
            flat = polygon
            if polygon and isinstance(polygon[0], list):
                flat = [coord for pair in polygon for coord in pair]
            if len(flat) < 6 or len(flat) % 2:
                continue
            try:
                points = _scale_points(flat, width, height, coordinate_space)
            except (TypeError, ValueError, OverflowError):
                continue
            if _polygon_area(points) < 1:
                continue
            predictions.append(Prediction(label, "polygon", points, confidence, attrs))
            continue

        bbox = item.get("bbox") or item.get("box") or item.get("bbox_1000")
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue
        try:
            points = _scale_points(bbox, width, height, coordinate_space)
        except (TypeError, ValueError, OverflowError):
            continue
        x1, y1, x2, y2 = points
        if x2 <= x1 or y2 <= y1:
            continue
        if task_type == "pose" or item.get("keypoints") is not None:
            raw_keypoints = item.get("keypoints")
            if not isinstance(raw_keypoints, list) or not raw_keypoints:
                continue
            keypoints = []
            try:
                for point in raw_keypoints:
                    x, y, v = ((point["x"], point["y"], point.get("visibility", 2)) if isinstance(point, dict) else point)
                    if v not in (0, 1, 2):
                        raise ValueError("invalid keypoint visibility")
                    xy = _scale_points([x,y], width, height, coordinate_space) if v else [0,0]
                    keypoints.append({"x":xy[0], "y":xy[1], "visibility":v})
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            predictions.append(Prediction(label, "keypoints", points, confidence, attrs, keypoints))
        else:
            predictions.append(Prediction(label, "rectangle", points, confidence, attrs))
    return predictions


def _scale_points(points: list[Any], width: int, height: int, coordinate_space: str) -> list[float]:
    values = [float(value) for value in points]
    if not all(math.isfinite(value) for value in values):
        raise ValueError("coordinates must be finite")
    maximum = max(values, default=0)
    # The annotation contract asks VLMs for coordinates normalized to 0..1000.
    # Generic inference services may return either normalized or absolute values.
    normalized = coordinate_space == "normalized_1000"
    if coordinate_space == "normalized_1" or (coordinate_space == "auto" and maximum <= 1):
        divisor = 1.0
    elif normalized or (coordinate_space == "auto" and maximum <= 1000 and maximum > max(width, height)):
        divisor = 1000.0
    else:
        divisor = None
    scaled: list[float] = []
    for index, value in enumerate(values):
        if divisor:
            limit = width if index % 2 == 0 else height
            value = value / divisor * limit
        limit = width if index % 2 == 0 else height
        scaled.append(round(min(float(limit), max(0.0, value)), 2))
    return scaled


def _polygon_area(points: list[float]) -> float:
    vertices = list(zip(points[0::2], points[1::2]))
    area = 0.0
    for index, (x1, y1) in enumerate(vertices):
        x2, y2 = vertices[(index + 1) % len(vertices)]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2
