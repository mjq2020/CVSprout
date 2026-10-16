"""Shared, side-effect-free inference preparation and result validation."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass

from .task_types import validate_task_annotation


@dataclass
class InferenceContext:
    project: dict
    config: dict
    label_map: dict
    target_labels: list[str]
    prompt: str
    hint_annotation: dict | None = None


def prepare_inference(project: dict, labels: list[dict], config: dict, prompt: str,
                      hint_annotation: dict | None = None) -> InferenceContext:
    label_map = {label["name"].strip().casefold(): label for label in labels}
    targets = config.get("target_labels") or [label["name"] for label in labels]
    targets = [label_map[name.strip().casefold()]["name"] for name in targets
               if name.strip().casefold() in label_map]
    if not targets:
        raise RuntimeError("任务的目标类别已不存在，请重新创建任务")
    contracts = {label["name"]: json.loads(label.get("attributes_schema_json") or "{}")
                 for label in labels if json.loads(label.get("attributes_schema_json") or "{}")}
    model_prompt = ", ".join(targets) if config.get("prompt_kind") == "grounding_labels" else prompt
    if contracts and config.get("prompt_kind") != "grounding_labels":
        model_prompt += "\n必须严格按以下标签属性 JSON Schema 输出 attributes：" + json.dumps(contracts, ensure_ascii=False)
    return InferenceContext(project, config, label_map, targets, model_prompt, hint_annotation)


def inference_hints(context: InferenceContext, asset: dict, asset_count: int) -> dict | None:
    hints = context.config.get("hints") if asset_count == 1 else None
    hint = context.hint_annotation
    if hint and hint["asset_id"] == asset["id"]:
        hints = {"shape_type": hint["shape_type"], "points": json.loads(hint["points_json"]),
                 "label": hint["label_name"], **(hints or {})}
    project = context.project
    if project.get("task_type") in {"classify", "pose", "segment"}:
        hints = {**(hints or {}), "task_type": project["task_type"],
                 "keypoint_schema": json.loads(project.get("keypoint_schema_json") or "{}")}
    return {**(hints or {}), "confidence_threshold": float(context.config.get("confidence_threshold", 0.35))}


def empty_counts() -> dict[str, int]:
    return {key: 0 for key in ("predicted", "accepted", "filtered_unknown_label", "filtered_target_label",
                              "filtered_confidence", "filtered_geometry", "filtered_classification")}


def filter_predictions(context: InferenceContext, predictions: list, asset: dict) -> tuple[list, dict]:
    accepted = []
    counts = empty_counts()
    counts["predicted"] = len(predictions)
    targets = {name.casefold() for name in context.target_labels}
    threshold = float(context.config.get("confidence_threshold", 0.35))
    for prediction in predictions:
        label = context.label_map.get(prediction.label.strip().casefold())
        if not label:
            counts["filtered_unknown_label"] += 1
            continue
        if label["name"].strip().casefold() not in targets:
            counts["filtered_target_label"] += 1
            continue
        if prediction.confidence is not None:
            if (not isinstance(prediction.confidence, (int, float)) or isinstance(prediction.confidence, bool)
                    or not math.isfinite(prediction.confidence) or not 0 <= prediction.confidence <= 1):
                counts["filtered_geometry"] += 1
                continue
            if prediction.confidence < threshold:
                counts["filtered_confidence"] += 1
                continue
        try:
            if not valid_prediction(prediction, label, asset["width"], asset["height"]):
                counts["filtered_geometry"] += 1
                continue
            json.dumps(prediction.attributes, allow_nan=False)
            validate_task_annotation(context.project, prediction.shape_type, prediction.points,
                                     prediction.keypoints, asset["width"], asset["height"])
        except Exception:
            counts["filtered_geometry"] += 1
            continue
        accepted.append((prediction, label))
    if context.project.get("task_type") == "classify":
        counts["filtered_classification"] = max(0, len(accepted) - 1)
        accepted = sorted(accepted, key=lambda item: item[0].confidence or 0, reverse=True)[:1]
    counts["accepted"] = len(accepted)
    return accepted, counts


def valid_prediction(prediction, label: dict, width: int, height: int) -> bool:
    points = prediction.points
    if prediction.shape_type in {"rectangle", "keypoints"}:
        if len(points) != 4 or points[2] <= points[0] or points[3] <= points[1]:
            return False
    elif prediction.shape_type == "polygon":
        if len(points) < 6 or len(points) % 2:
            return False
        vertices = list(zip(points[0::2], points[1::2]))
        area = sum(
            x1 * vertices[(index + 1) % len(vertices)][1]
            - vertices[(index + 1) % len(vertices)][0] * y1
            for index, (x1, y1) in enumerate(vertices)
        )
        if abs(area) < 1:
            return False
    elif prediction.shape_type == "classification":
        if points:
            return False
    else:
        return False
    if any(
        not math.isfinite(value)
        or value < 0
        or value > (width if index % 2 == 0 else height)
        for index, value in enumerate(points)
    ):
        return False
    try:
        schema = json.loads(label.get("attributes_schema_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    attributes = prediction.attributes
    if any(name not in attributes for name in schema.get("required", [])):
        return False
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False and set(attributes) - set(properties):
        return False
    primitive_types = {
        "string": str, "number": (int, float), "integer": int,
        "boolean": bool, "array": list, "object": dict,
    }
    for name, value in attributes.items():
        rule = properties.get(name, {})
        expected = primitive_types.get(rule.get("type"))
        if expected and (
            not isinstance(value, expected)
            or rule.get("type") in {"number", "integer"} and isinstance(value, bool)
        ):
            return False
        if rule.get("type") in {"number", "integer"} and not math.isfinite(value):
            return False
        if "enum" in rule and value not in rule["enum"]:
            return False
    return True
