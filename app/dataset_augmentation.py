"""Deterministic image and annotation transforms for frozen dataset versions.

The caller owns splitting, files and persistence. Coordinates are expressed in
image-edge units (0..width and 0..height), matching the annotation canvas.
"""
from __future__ import annotations

import copy
import hashlib
import io
import math
from typing import Any

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

ALGORITHM_VERSION = "visionforge-augmentation-v1"
OPERATION_TYPES = {
    "horizontal_flip", "vertical_flip", "rotate90", "brightness", "contrast",
    "saturation", "grayscale", "blur", "noise",
    "gamma", "hue", "sharpness", "autocontrast", "equalize", "jpeg_compression", "downscale",
}


def _finite(value: Any, minimum: float, maximum: float, label: str) -> float:
    if type(value) not in (int, float) or not minimum <= value <= maximum or not math.isfinite(value):
        raise ValueError(f"{label}必须是 {minimum}–{maximum} 之间的有限数值")
    return float(value)


def _validated_operations(config: dict, task: str, schema: dict) -> list[dict]:
    if not isinstance(config, dict) or set(config) - {"seed", "copies_per_image", "target_count", "operations"}:
        raise ValueError("数据增强配置包含未知字段")
    if type(config.get("seed", 42)) is not int or not 0 <= config.get("seed", 42) <= 2_147_483_647:
        raise ValueError("增强随机种子必须是 0–2147483647 的整数")
    if type(config.get("copies_per_image", 1)) is not int or not 1 <= config.get("copies_per_image", 1) <= 20:
        raise ValueError("每张训练图片只能额外生成 1–20 张图片")
    target_count = config.get("target_count")
    if target_count is not None and (type(target_count) is not int or not 1 <= target_count <= 5000):
        raise ValueError("新增训练图片总数必须是 1–5000 的整数")
    operations = config.get("operations")
    if not isinstance(operations, list) or not 1 <= len(operations) <= len(OPERATION_TYPES):
        raise ValueError("请至少选择一种支持的数据增强")
    result, seen = [], set()
    for original in operations:
        if not isinstance(original, dict) or original.get("type") not in OPERATION_TYPES:
            raise ValueError("不支持的数据增强类型")
        operation = dict(original); kind = operation["type"]
        if kind in seen:
            raise ValueError("同一种数据增强不能重复配置")
        seen.add(kind)
        allowed = {"type", "probability"}
        operation["probability"] = _finite(operation.get("probability", 1), 0, 1, "应用概率")
        if operation["probability"] == 0:
            raise ValueError("已选择的增强应用概率需大于零")
        if kind == "rotate90":
            allowed.add("degrees"); angles = operation.get("degrees", [90, 180, 270])
            if not isinstance(angles, list) or not angles or any(type(angle) is not int or angle not in (90, 180, 270) for angle in angles) or len(set(angles)) != len(angles):
                raise ValueError("旋转角度只能选择不重复的 90、180、270 度")
            operation["degrees"] = angles
        elif kind in {"brightness", "contrast", "saturation", "sharpness"}:
            allowed.update(("min_factor", "max_factor"))
            minimum, maximum = (0, 3) if kind == "sharpness" else (.1, 2)
            default_min, default_max = (.5, 1.5) if kind == "sharpness" else (.8, 1.2)
            operation["min_factor"] = _finite(operation.get("min_factor", default_min), minimum, maximum, "强度下限")
            operation["max_factor"] = _finite(operation.get("max_factor", default_max), minimum, maximum, "强度上限")
            if operation["min_factor"] > operation["max_factor"]:
                raise ValueError("增强强度下限不能大于上限")
        elif kind in {"gamma", "hue", "downscale"}:
            parameter, default_min, default_max, minimum, maximum = {
                "gamma": ("gamma", .8, 1.2, .2, 3),
                "hue": ("shift", -.05, .05, -.5, .5),
                "downscale": ("scale", .5, .9, .1, 1),
            }[kind]
            low, high = f"min_{parameter}", f"max_{parameter}"
            allowed.update((low, high))
            operation[low] = _finite(operation.get(low, default_min), minimum, maximum, "参数下限")
            operation[high] = _finite(operation.get(high, default_max), minimum, maximum, "参数上限")
            if operation[low] > operation[high]:
                raise ValueError("增强参数下限不能大于上限")
        elif kind == "autocontrast":
            allowed.add("cutoff")
            operation["cutoff"] = _finite(operation.get("cutoff", 1), 0, 10, "自动对比度截断比例")
        elif kind == "jpeg_compression":
            allowed.update(("min_quality", "max_quality"))
            for field, default in (("min_quality", 60), ("max_quality", 95)):
                value = operation.get(field, default)
                if type(value) is not int or not 10 <= value <= 100:
                    raise ValueError("JPEG 质量必须是 10–100 的整数")
                operation[field] = value
            if operation["min_quality"] > operation["max_quality"]:
                raise ValueError("JPEG 质量下限不能大于上限")
        elif kind == "blur":
            allowed.add("radius"); operation["radius"] = _finite(operation.get("radius", 1.5), .1, 5, "模糊半径")
        elif kind == "noise":
            allowed.add("std"); operation["std"] = _finite(operation.get("std", 10), 1, 30, "噪声强度")
        if set(original) - allowed:
            raise ValueError(f"{kind} 包含不适用的参数")
        if task == "pose" and kind == "horizontal_flip":
            indices = schema.get("flip_idx", [])
            count = len(schema.get("names", []))
            if not count or len(indices) != count or any(type(index) is not int for index in indices) or sorted(indices) != list(range(count)) or any(indices[indices[index]] != index for index in range(count)):
                raise ValueError("关键点水平翻转需要完整、对称的左右点映射 flip_idx")
        result.append(operation)
    return result


def _transform_point(x: float, y: float, width: int, height: int, kind: str, degrees: int = 0) -> tuple[float, float]:
    if kind == "horizontal_flip": return width - x, y
    if kind == "vertical_flip": return x, height - y
    if degrees == 90: return y, width - x
    if degrees == 180: return width - x, height - y
    if degrees == 270: return height - y, x
    return x, y


def _geometry(annotations: list[dict], width: int, height: int, kind: str, degrees: int, schema: dict) -> list[dict]:
    rotated = kind == "rotate90" and degrees in (90, 270)
    new_width, new_height = (height, width) if rotated else (width, height)
    def point(x: float, y: float) -> tuple[float, float]:
        x, y = _transform_point(x, y, width, height, kind, degrees)
        return round(min(new_width, max(0, x)), 8), round(min(new_height, max(0, y)), 8)
    for row in annotations:
        shape = row["shape_type"]
        if shape == "classification": continue
        coords = row.get("points", [])
        if shape in {"rectangle", "keypoints"}:
            x1, y1, x2, y2 = coords
            corners = [point(x1, y1), point(x2, y1), point(x2, y2), point(x1, y2)]
            row["points"] = [min(x for x, _ in corners), min(y for _, y in corners), max(x for x, _ in corners), max(y for _, y in corners)]
        else:
            row["points"] = [value for x, y in zip(coords[::2], coords[1::2]) for value in point(x, y)]
        if shape == "keypoints":
            points = []
            for keypoint in row["keypoints"]:
                item = dict(keypoint)
                item["x"], item["y"] = point(item["x"], item["y"]) if item["visibility"] else (0, 0)
                points.append(item)
            if kind == "horizontal_flip":
                indices = schema.get("flip_idx", [])
                if len(indices) != len(points): raise ValueError("关键点数与左右点映射不一致")
                points = [points[index] for index in indices]
            row["keypoints"] = points
    return annotations


def _validate_annotations(annotations: list[dict], width: int, height: int, task: str, schema: dict) -> None:
    allowed = {"detect": {"rectangle"}, "segment": {"polygon"}, "classify": {"classification"}, "pose": {"keypoints"}, "mixed": {"rectangle", "polygon"}}
    if task not in allowed: raise ValueError("不支持的数据集任务类型")
    for row in annotations:
        shape = row.get("shape_type")
        if shape not in allowed[task]: raise ValueError("标注类型与数据增强任务不一致")
        coords = row.get("points", [])
        points = row.get("keypoints", [])
        if shape == "classification":
            if coords or points: raise ValueError("分类标签不能包含几何坐标")
            continue
        if not isinstance(coords, list) or (len(coords) != 4 if shape in {"rectangle", "keypoints"} else len(coords) < 6 or len(coords) % 2):
            raise ValueError("标注坐标数量无效")
        for index, value in enumerate(coords): _finite(value, 0, width if index % 2 == 0 else height, "标注坐标")
        if shape == "keypoints":
            if len(points) != len(schema.get("names", [])) or not points: raise ValueError("关键点数量与项目定义不一致")
            for item in points:
                if item.get("visibility") not in (0, 1, 2): raise ValueError("关键点可见性无效")
                _finite(item.get("x"), 0, width, "关键点横坐标"); _finite(item.get("y"), 0, height, "关键点纵坐标")
                if not item["visibility"] and (item["x"] or item["y"]): raise ValueError("未标注关键点的坐标必须为零")


def augment_image(image: Image.Image, annotations: list[dict], config: dict, *, task: str, keypoint_schema: dict,
                  source_key: str, variant_index: int) -> tuple[Image.Image, list[dict], dict]:
    """Return a new RGB image, transformed annotation copies and replay metadata.

    Operations are composed in the configured order. A probability miss is
    recorded, and no-op / duplicate outputs are deduplicated by the caller.
    Only lossless right-angle geometry is offered, so no object is cropped out.
    """
    if type(variant_index) is not int or not 1 <= variant_index <= 20:
        raise ValueError("增强副本编号必须为 1–20")
    if not isinstance(source_key, str) or not source_key: raise ValueError("增强缺少原图标识")
    operations = _validated_operations(config, task, keypoint_schema)
    _validate_annotations(annotations, *image.size, task, keypoint_schema)
    seed_material = f"{ALGORITHM_VERSION}:{config.get('seed', 42)}:{source_key}:{variant_index}"
    derived_seed = hashlib.sha256(seed_material.encode()).hexdigest()[:16]
    rng = np.random.default_rng(int(derived_seed, 16))
    result = image.convert("RGB").copy(); rows = copy.deepcopy(annotations)
    applied, decisions = [], []
    for operation in operations:
        kind = operation["type"]
        if float(rng.random()) >= operation["probability"]:
            decisions.append({"type": kind, "applied": False}); continue
        actual = {"type": kind}
        if kind in {"horizontal_flip", "vertical_flip", "rotate90"}:
            width, height = result.size
            degrees = int(rng.choice(operation["degrees"])) if kind == "rotate90" else 0
            if kind == "horizontal_flip": result = ImageOps.mirror(result)
            elif kind == "vertical_flip": result = ImageOps.flip(result)
            else:
                result = result.transpose({90: Image.Transpose.ROTATE_90, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_270}[degrees])
                actual["degrees"] = degrees
            rows = _geometry(rows, width, height, kind, degrees, keypoint_schema)
        elif kind in {"brightness", "contrast", "saturation", "sharpness"}:
            factor = float(rng.uniform(operation["min_factor"], operation["max_factor"]))
            enhancer = {"brightness": ImageEnhance.Brightness, "contrast": ImageEnhance.Contrast,
                        "saturation": ImageEnhance.Color, "sharpness": ImageEnhance.Sharpness}[kind]
            result = enhancer(result).enhance(factor); actual["factor"] = factor
        elif kind == "gamma":
            gamma = float(rng.uniform(operation["min_gamma"], operation["max_gamma"]))
            table = [round(255 * (value / 255) ** gamma) for value in range(256)]
            result = result.point(table * 3); actual["gamma"] = gamma
        elif kind == "hue":
            shift = float(rng.uniform(operation["min_shift"], operation["max_shift"]))
            offset = round(shift * 256)
            if offset:
                channels = list(result.convert("HSV").split())
                channels[0] = channels[0].point([(value + offset) % 256 for value in range(256)])
                result = Image.merge("HSV", channels).convert("RGB")
            actual.update(shift=shift, hue_offset=offset)
        elif kind == "autocontrast":
            result = ImageOps.autocontrast(result, cutoff=operation["cutoff"])
            actual["cutoff"] = operation["cutoff"]
        elif kind == "equalize":
            result = ImageOps.equalize(result)
        elif kind == "jpeg_compression":
            quality = int(rng.integers(operation["min_quality"], operation["max_quality"] + 1))
            with io.BytesIO() as buffer:
                result.save(buffer, format="JPEG", quality=quality, subsampling=2, optimize=False, progressive=False)
                buffer.seek(0)
                with Image.open(buffer) as compressed:
                    result = compressed.convert("RGB")
            actual["quality"] = quality
        elif kind == "downscale":
            scale = float(rng.uniform(operation["min_scale"], operation["max_scale"]))
            original_size = result.size
            reduced_size = tuple(max(1, round(dimension * scale)) for dimension in original_size)
            result = result.resize(reduced_size, Image.Resampling.BOX).resize(original_size, Image.Resampling.BILINEAR)
            actual.update(scale=scale, reduced_size=list(reduced_size))
        elif kind == "grayscale": result = ImageOps.grayscale(result).convert("RGB")
        elif kind == "blur":
            result = result.filter(ImageFilter.GaussianBlur(operation["radius"])); actual["radius"] = operation["radius"]
        elif kind == "noise":
            pixels = np.asarray(result)
            noisy = np.empty_like(pixels)
            # Keep floating-point work bounded even for the largest allowed
            # uploads. Row-order draws preserve the same seeded noise sequence.
            block_rows = max(1, 1_000_000 // (pixels.shape[1] * 3))
            for start in range(0, pixels.shape[0], block_rows):
                block = pixels[start:start + block_rows]
                noise = rng.normal(0, operation["std"], block.shape)
                noisy[start:start + block_rows] = np.clip(np.rint(block + noise), 0, 255).astype(np.uint8)
            result = Image.fromarray(noisy); actual["std"] = operation["std"]
        applied.append(actual); decisions.append({**actual, "applied": True})
    _validate_annotations(rows, *result.size, task, keypoint_schema)
    return result, rows, {"algorithm_version": ALGORITHM_VERSION, "seed": config.get("seed", 42),
        "derived_seed": derived_seed, "variant_index": variant_index, "source_key": source_key,
        "operations": decisions, "applied": applied, "input_size": list(image.size), "output_size": list(result.size)}
