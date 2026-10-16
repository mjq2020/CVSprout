from __future__ import annotations

import json
import math

from fastapi import HTTPException

TASK_SHAPES = {"mixed": {"rectangle", "polygon"}, "detect": {"rectangle"},
               "segment": {"polygon"}, "classify": {"classification"}, "pose": {"keypoints"}}


def validate_task_annotation(project: dict, shape: str, points: list, keypoints: list, width: int, height: int) -> None:
    if shape not in TASK_SHAPES.get(project.get("task_type", "mixed"), set()):
        raise HTTPException(422, "标注类型与项目任务不匹配")
    if shape == "classification":
        if points or keypoints:
            raise HTTPException(422, "图片分类不应包含几何坐标")
        return
    if shape != "keypoints":
        if keypoints:
            raise HTTPException(422, "只有关键点标注可包含关键点数据")
        return
    schema = json.loads(project.get("keypoint_schema_json") or "{}")
    if len(keypoints) != len(schema.get("names", [])) or not keypoints:
        raise HTTPException(422, "关键点数量及顺序必须与项目定义一致")
    visible = 0
    for point in keypoints:
        x, y, visibility = point.get("x"), point.get("y"), point.get("visibility")
        if (visibility not in (0, 1, 2) or not isinstance(x, (int, float)) or not isinstance(y, (int, float))
                or not math.isfinite(x) or not math.isfinite(y) or not 0 <= x <= width or not 0 <= y <= height):
            raise HTTPException(422, "关键点坐标或可见性无效")
        if visibility == 0 and (x != 0 or y != 0):
            raise HTTPException(422, "未标注的关键点必须为 x=0, y=0, visibility=0")
        visible += visibility > 0
    if not visible:
        raise HTTPException(422, "每个对象至少需要一个已标注关键点")
