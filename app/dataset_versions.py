from __future__ import annotations

import hashlib
import base64
import io
import json
import math
import shutil
import tempfile
import uuid
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from PIL import Image, ImageOps

from .auth import Actor, get_actor, require_reviewer
from .config import settings
from .database import all_rows, db, now_iso, one
from .schemas import DatasetVersionCreate, DatasetVersionPreview


router = APIRouter(prefix="/api", tags=["dataset versions"])
SPLITS = ("train", "valid", "test")
AUGMENTATION_LIMITS = {"max_copies_per_image": 20, "max_generated_assets": 5000, "max_total_assets": 20000, "max_generated_pixels": 2_000_000_000}
AUGMENTATION_NAMES = {"horizontal_flip": "水平翻转", "vertical_flip": "垂直翻转", "rotate90": "90 度旋转", "brightness": "亮度", "contrast": "对比度", "saturation": "饱和度", "grayscale": "灰度", "blur": "模糊", "noise": "噪声", "gamma": "伽马亮度", "hue": "色相偏移", "sharpness": "锐度", "autocontrast": "自动对比度", "equalize": "直方图均衡", "jpeg_compression": "JPEG 压缩", "downscale": "降低清晰度"}


def _require_project(project_id: int) -> dict[str, Any]:
    project = one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not project:
        raise HTTPException(404, "项目不存在")
    return project


def _require_version(version_id: int) -> dict[str, Any]:
    version = one("SELECT * FROM dataset_versions WHERE id = ?", (version_id,))
    if not version:
        raise HTTPException(404, "数据集版本不存在")
    return version


def _json(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


def _version_result(row: dict[str, Any], detailed: bool = False) -> dict[str, Any]:
    result = dict(row)
    result["approved_only"] = bool(result["approved_only"])
    result["stats"] = _json(result.pop("stats_json", "{}"), {})
    result["splits"] = result["stats"].get("splits", {})
    result["keypoint_schema"] = _json(result.pop("keypoint_schema_json", "{}"), {})
    result["augmentation"] = _json(result.pop("augmentation_json", "null"), None)
    samples = all_rows(
        """SELECT id, filename, split, width, height, variant_index, original_asset_id FROM (
            SELECT id, filename, split, width, height, variant_index, original_asset_id,
                   ROW_NUMBER() OVER (PARTITION BY split ORDER BY id) AS sample_number
            FROM dataset_version_assets WHERE version_id = ?
        ) WHERE sample_number <= 2
        ORDER BY CASE split WHEN 'train' THEN 0 WHEN 'valid' THEN 1 ELSE 2 END, id""",
        (result["id"],),
    )
    result["preview_assets"] = [_version_asset_result(result["id"], asset) for asset in samples]
    result["exports"] = {
        export_format: f"/api/versions/{result['id']}/export/{export_format}"
        for export_format in (("yolo", "json") if row.get("task_type") == "classify" else ("coco", "yolo", "json"))
    }
    if detailed:
        labels = all_rows(
            """SELECT * FROM dataset_version_labels
               WHERE version_id = ? ORDER BY category_index""",
            (result["id"],),
        )
        label_stats = {
            item["category_index"]: item
            for item in result["stats"].get("labels", [])
        }
        for label in labels:
            label["attributes_schema"] = _json(
                label.pop("attributes_schema_json", "{}"), {}
            )
            label.update(label_stats.get(label["category_index"], {}))
        result["labels"] = labels
    return result


def _split_counts(total: int, ratios: dict[str, float]) -> dict[str, int]:
    raw = {split: total * ratios[split] for split in SPLITS}
    counts = {split: math.floor(raw[split]) for split in SPLITS}
    remaining = total - sum(counts.values())
    order = sorted(SPLITS, key=lambda split: (-(raw[split] - counts[split]), SPLITS.index(split)))
    for split in order[:remaining]:
        counts[split] += 1
    if total and counts["train"] == 0:
        donor = max(("valid", "test"), key=lambda split: counts[split])
        if counts[donor]:
            counts[donor] -= 1
            counts["train"] = 1
    return counts


def _assign_splits(
    assets: list[dict[str, Any]], seed: int, ratios: dict[str, float]
) -> dict[int, str]:
    ordered = sorted(
        assets,
        key=lambda asset: hashlib.sha256(
            f"{seed}:{asset['sha256']}:{asset['id']}".encode()
        ).hexdigest(),
    )
    counts = _split_counts(len(ordered), ratios)
    assignments: dict[int, str] = {}
    offset = 0
    for split in SPLITS:
        for asset in ordered[offset : offset + counts[split]]:
            assignments[int(asset["id"])] = split
        offset += counts[split]
    return assignments


def _canonical_snapshot(
    labels: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    annotations: list[dict[str, Any]],
    assignments: dict[int, str],
    payload: DatasetVersionCreate,
    project: dict | None = None,
) -> tuple[str, dict[str, Any]]:
    snapshot = {
        "task_type": (project or {}).get("task_type", "mixed"),
        "keypoint_schema": _json((project or {}).get("keypoint_schema_json"), {}),
        "approved_only": payload.approved_only,
        "split_seed": payload.split_seed,
        "ratios": {
            "train": payload.train_ratio,
            "valid": payload.valid_ratio,
            "test": payload.test_ratio,
        },
        "labels": [
            {
                "original_id": label["id"],
                "name": label["name"],
                "color": label["color"],
                "shortcut": label["shortcut"],
                "attributes_schema": _json(label.get("attributes_schema_json"), {}),
            }
            for label in labels
        ],
        "assets": [
            {
                "original_id": asset["id"],
                "split": assignments[asset["id"]],
                "filename": asset["filename"],
                "mime_type": asset["mime_type"],
                "width": asset["width"],
                "height": asset["height"],
                "size_bytes": asset["size_bytes"],
                "sha256": asset["sha256"],
                "status": asset["status"],
                "review_status": asset["review_status"],
                "review_reason": asset["review_reason"],
                "revision": asset["revision"],
                "created_at": asset["created_at"],
                "updated_at": asset["updated_at"],
            }
            for asset in assets
        ],
        "annotations": [
            {
                "original_id": annotation["id"],
                "asset_id": annotation["asset_id"],
                "label_id": annotation["label_id"],
                "shape_type": annotation["shape_type"],
                "keypoints": _json(annotation.get("keypoints_json"), []),
                "human_modified": bool(annotation.get("human_modified")),
                "points": _json(annotation["points_json"], []),
                "attributes": _json(annotation["attributes_json"], {}),
                "confidence": annotation["confidence"],
                "source": annotation["source"],
                "model_id": annotation["model_id"],
                "version": annotation["version"],
                "created_by": annotation["created_by"],
                "updated_by": annotation["updated_by"],
                "created_at": annotation["created_at"],
                "updated_at": annotation["updated_at"],
            }
            for annotation in annotations
        ],
    }
    encoded = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest(), snapshot


def _snapshot_stats(
    labels: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    annotations: list[dict[str, Any]],
    assignments: dict[int, str],
) -> dict[str, Any]:
    annotations_by_asset: Counter[int] = Counter(row["asset_id"] for row in annotations)
    split_assets = Counter(assignments.values())
    split_annotations: Counter[str] = Counter()
    for row in annotations:
        split_annotations[assignments[row["asset_id"]]] += 1
    label_objects: Counter[int] = Counter()
    label_images: defaultdict[int, set[int]] = defaultdict(set)
    label_splits: defaultdict[int, Counter[str]] = defaultdict(Counter)
    for row in annotations:
        label_objects[row["label_id"]] += 1
        label_images[row["label_id"]].add(row["asset_id"])
        label_splits[row["label_id"]][assignments[row["asset_id"]]] += 1
    shape_types = Counter(row["shape_type"] for row in annotations)
    sources = Counter(row["source"] for row in annotations)
    review_statuses = Counter(asset["review_status"] for asset in assets)
    statuses = Counter(asset["status"] for asset in assets)
    return {
        "splits": {
            split: {
                "asset_count": split_assets[split],
                "annotation_count": split_annotations[split],
            }
            for split in SPLITS
        },
        "labels": [
            {
                "category_index": index,
                "name": label["name"],
                "object_count": label_objects[label["id"]],
                "image_count": len(label_images[label["id"]]),
                "splits": {
                    split: label_splits[label["id"]][split] for split in SPLITS
                },
            }
            for index, label in enumerate(labels)
        ],
        "workflow": dict(statuses),
        "review": dict(review_statuses),
        "shape_types": dict(shape_types),
        "annotation_sources": dict(sources),
        "empty_asset_count": sum(not annotations_by_asset[asset["id"]] for asset in assets),
        "task_type": "segment" if shape_types["polygon"] else "detect",
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_snapshot(project_id: int, approved_only: bool) -> tuple[dict, list[dict], list[dict], list[dict]]:
    with db() as conn:
        conn.execute("BEGIN")
        project_row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if project_row is None: raise HTTPException(404, "项目不存在")
        project = dict(project_row)
        labels = [dict(row) for row in conn.execute("SELECT * FROM labels WHERE project_id=? ORDER BY id", (project_id,))]
        assets = [dict(row) for row in conn.execute("SELECT * FROM assets WHERE project_id=?" + (" AND review_status='approved'" if approved_only else "") + " ORDER BY id", (project_id,))]
        if not assets: raise HTTPException(409, "项目中没有审核通过的图片，无法创建版本" if approved_only else "项目中没有图片，无法创建版本")
        if not labels: raise HTTPException(409, "项目没有标签，无法创建版本")
        ids = {asset["id"] for asset in assets}
        annotations = [dict(row) for row in conn.execute("SELECT n.* FROM annotations n JOIN assets a ON a.id=n.asset_id WHERE a.project_id=? AND n.deleted_at IS NULL ORDER BY n.id", (project_id,)) if row["asset_id"] in ids]
    return project, labels, assets, annotations


def _augmentation_copy_plan(assets: list[dict], assignments: dict, config: dict | None) -> dict[int, int]:
    """Allocate additions evenly, independent of input row ordering.

    Stable hash ordering spreads the remainder across training originals. The
    same per-source variant indices are used by preview and frozen generation.
    Validation/test originals always receive zero additions.
    """
    copies = {asset["id"]: 0 for asset in assets}
    if not config: return copies
    training = [asset for asset in assets if assignments[asset["id"]] == "train"]
    target = config.get("target_count")
    if target is None:
        copies.update((asset["id"], config["copies_per_image"]) for asset in training)
        return copies
    capacity = len(training) * AUGMENTATION_LIMITS["max_copies_per_image"]
    if target > capacity:
        raise HTTPException(422, f"预计新增数量超过当前训练集容量：{len(training)} 张训练原图每张最多新增 20 份，合计最多新增 {capacity} 张。请减少预计新增数量或增加训练图片。")
    base, remainder = divmod(target, len(training))
    ordered = sorted(training, key=lambda asset: hashlib.sha256(f"augmentation-allocation-v1:{config['seed']}:{asset['sha256']}:{asset['id']}".encode()).hexdigest())
    copies.update((asset["id"], base + int(index < remainder)) for index, asset in enumerate(ordered))
    return copies


def _augmentation_plan(project: dict, assets: list[dict], annotations: list[dict], assignments: dict, payload: DatasetVersionCreate) -> tuple[dict | None, dict, dict[int, int]]:
    config = payload.augmentation.model_dump(exclude_none=True) if payload.augmentation else None
    original_counts = Counter(assignments.values())
    copies = _augmentation_copy_plan(assets, assignments, config)
    generated = sum(copies.values())
    counts = {"original": len(assets), "original_train": original_counts["train"], "valid": original_counts["valid"], "test": original_counts["test"], "generated": generated, "final": len(assets) + generated, "final_train": original_counts["train"] + generated}
    if config:
        schema = _json(project.get("keypoint_schema_json"), {})
        has_keypoints = project.get("task_type") == "pose" or any(item["shape_type"] == "keypoints" for item in annotations)
        if has_keypoints and any(item["type"] == "horizontal_flip" for item in config["operations"]) and not schema.get("flip_idx"):
            raise HTTPException(422, "水平翻转关键点需要先配置左右关键点映射 flip_idx；请取消水平翻转或补全项目关键点配置。")
        pixels = sum(asset["width"] * asset["height"] * copies[asset["id"]] for asset in assets)
        if generated > AUGMENTATION_LIMITS["max_generated_assets"] or counts["final"] > AUGMENTATION_LIMITS["max_total_assets"] or pixels > AUGMENTATION_LIMITS["max_generated_pixels"]:
            raise HTTPException(422, "增强数量过大：最多新增 5000 张、总计 20000 张，新增图片总像素不超过 20 亿。请减少预计新增数量或每张图片的增强副本数。")
    return config, counts, copies


def _transform_annotations(rows: list[dict]) -> list[dict]:
    return [{**row, "points": _json(row["points_json"], []), "keypoints": _json(row.get("keypoints_json"), [])} for row in rows]


def _augmentation_image(opened: Image.Image, asset: dict) -> Image.Image:
    # Existing annotation coordinates use the stored pixel dimensions. Applying
    # EXIF orientation here would move the pixels without moving those labels.
    if opened.getexif().get(274, 1) != 1:
        raise HTTPException(422, f"图片“{asset['filename']}”包含旋转或镜像方向信息，无法安全对应现有标注。请先将图片转正，重新导入并确认标注，再生成预览或增强版本。")
    return opened.convert("RGB")


def _visual_identity(image: Image.Image, annotations: list[dict]) -> str:
    def coordinate(value):
        return float(round(value, 8)) if value else 0.0
    geometry = [{"id": row["id"], "label_id": row["label_id"], "shape_type": row["shape_type"], "points": [coordinate(value) for value in row["points"]], "keypoints": [{"x": coordinate(point["x"]), "y": coordinate(point["y"]), "visibility": point["visibility"]} for point in row["keypoints"]]} for row in annotations]
    encoded = json.dumps(geometry, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(f"{image.size}:{image.mode}:{encoded}".encode())
    digest.update(image.tobytes())
    return digest.hexdigest()


def _augmented_samples(image: Image.Image, annotations: list[dict], config: dict, project: dict, asset: dict, copies: int):
    from .dataset_augmentation import augment_image
    seen = {_visual_identity(image, annotations)}
    for index in range(1, copies + 1):
        try:
            transformed, rows, metadata = augment_image(image, annotations, config, task=project["task_type"], keypoint_schema=_json(project.get("keypoint_schema_json"), {}), source_key=f"{asset['id']}:{asset['sha256']}", variant_index=index)
        except ValueError as error:
            raise HTTPException(422, f"图片“{asset['filename']}”无法增强：{error}") from error
        identity = _visual_identity(transformed, rows)
        duplicate = identity in seen
        seen.add(identity)
        yield index, transformed, rows, metadata, duplicate


@router.get("/projects/{project_id}/augmentation-options")
def augmentation_options(project_id: int, _: Actor = Depends(get_actor)) -> dict:
    project = _require_project(project_id)
    schema = _json(project.get("keypoint_schema_json"), {})
    has_pose = project["task_type"] == "pose" or one("SELECT 1 FROM annotations n JOIN assets a ON a.id=n.asset_id WHERE a.project_id=? AND n.shape_type='keypoints' AND n.deleted_at IS NULL LIMIT 1", (project_id,)) is not None
    return {"limits": AUGMENTATION_LIMITS, "operations": [{"type": name, "name": label, "available": not (name == "horizontal_flip" and has_pose and not schema.get("flip_idx")), "reason": "请先配置左右关键点映射 flip_idx，水平翻转才能正确交换左右关键点。" if name == "horizontal_flip" and has_pose and not schema.get("flip_idx") else ""} for name, label in AUGMENTATION_NAMES.items()], "train_only": True, "originals_retained": True}


def _preview_image(image: Image.Image, annotations: list[dict], labels: list[dict]) -> dict:
    colors = {row["id"]: row for row in labels}
    display = image.copy(); display.thumbnail((640, 640), Image.Resampling.LANCZOS)
    buffer = io.BytesIO(); display.save(buffer, format="PNG")
    return {"image_data_url": "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"), "width": image.width, "height": image.height, "annotations": [{"shape_type": row["shape_type"], "points": row["points"], "keypoints": row["keypoints"], "label_name": colors[row["label_id"]]["name"], "color": colors[row["label_id"]]["color"]} for row in annotations]}


@router.post("/projects/{project_id}/versions/preview")
def preview_version(project_id: int, payload: DatasetVersionPreview, actor: Actor = Depends(get_actor)) -> dict:
    require_reviewer(actor)
    project, labels, assets, annotations = _source_snapshot(project_id, payload.approved_only)
    assignments = _assign_splits(assets, payload.split_seed, {"train": payload.train_ratio, "valid": payload.valid_ratio, "test": payload.test_ratio})
    config, counts, copies = _augmentation_plan(project, assets, annotations, assignments, payload)
    source_hash, _ = _canonical_snapshot(labels, assets, annotations, assignments, payload, project)
    annotated_ids = {row["asset_id"] for row in annotations}
    asset = next((row for row in assets if row["id"] == payload.asset_id), None) if payload.asset_id else min(assets, key=lambda row: (assignments[row["id"]] != "train", bool(config) and not copies[row["id"]], row["id"] not in annotated_ids, row["id"]))
    if asset is None: raise HTTPException(404, "预览图片不在当前版本的数据范围内，请重新选择图片。")
    source = (settings.media_dir / asset["storage_name"]).resolve()
    if settings.media_dir.resolve() not in source.parents or not source.is_file(): raise HTTPException(409, "预览图片暂不可用，请刷新后重试。")
    try:
        source_bytes = source.read_bytes()
    except FileNotFoundError as error:
        raise HTTPException(409, "预览图片已被删除，请刷新图片列表后重新预览。") from error
    if hashlib.sha256(source_bytes).hexdigest() != asset["sha256"]: raise HTTPException(409, "预览图片校验失败，请刷新后重试。")
    with Image.open(io.BytesIO(source_bytes)) as opened: image = _augmentation_image(opened, asset)
    rows = _transform_annotations([row for row in annotations if row["asset_id"] == asset["id"]])
    variants = []; skipped = 0
    planned_copies = copies[asset["id"]]
    attempted = min(planned_copies, 3)
    if config and attempted:
        for index, transformed, transformed_rows, metadata, duplicate in _augmented_samples(image, rows, config, project, asset, attempted):
            if duplicate: skipped += 1; continue
            variants.append({"variant_index": index, **_preview_image(transformed, transformed_rows, labels), "transform": metadata})
    return {"source_hash": source_hash, "counts": counts, "estimated": True, "selected_asset": {"id": asset["id"], "filename": asset["filename"], "split": assignments[asset["id"]], "planned_copies": planned_copies}, "original": _preview_image(image, rows, labels), "variants": variants, "skipped_duplicates": skipped, "preview_limit": 3, "preview_attempted": attempted, "preview_truncated": planned_copies > attempted, "keypoint_schema": _json(project.get("keypoint_schema_json"), {})}


@router.get("/projects/{project_id}/versions")
def list_versions(project_id: int, _: Actor = Depends(get_actor)) -> list[dict[str, Any]]:
    _require_project(project_id)
    return [
        _version_result(row)
        for row in all_rows(
            """SELECT * FROM dataset_versions WHERE project_id = ?
               ORDER BY version_number DESC""",
            (project_id,),
        )
    ]


@router.post("/projects/{project_id}/versions", status_code=201)
def create_version(
    project_id: int,
    payload: DatasetVersionCreate,
    actor: Actor = Depends(get_actor),
) -> dict[str, Any]:
    require_reviewer(actor)
    project, labels, assets, annotations = _source_snapshot(project_id, payload.approved_only)
    ratios = {
        "train": payload.train_ratio,
        "valid": payload.valid_ratio,
        "test": payload.test_ratio,
    }
    staging_dir = Path(
        tempfile.mkdtemp(prefix=".version-staging-", dir=settings.versions_dir)
    )
    (staging_dir / "media").mkdir()
    version_dir: Path | None = None
    published = False
    committed = False
    try:
        assignments = _assign_splits(assets, payload.split_seed, ratios)
        augmentation, planned_counts, planned_copies = _augmentation_plan(project, assets, annotations, assignments, payload)
        content_hash, _ = _canonical_snapshot(
            labels, assets, annotations, assignments, payload, project
        )
        if payload.expected_source_hash is not None and payload.expected_source_hash != content_hash:
            raise HTTPException(409, {"code": "source_changed", "message": "数据或标注已变化，请重新预览后再生成版本。"})
        storage_names: dict[Any, str] = {}
        media_root = settings.media_dir.resolve()
        for asset in assets:
            source = (settings.media_dir / asset["storage_name"]).resolve()
            if media_root not in source.parents or not source.is_file():
                raise HTTPException(409, f"源图片文件缺失或路径无效：{asset['filename']}")
            suffix = source.suffix.lower()
            storage_name = f"media/{asset['id']}-{asset['sha256']}{suffix}"
            destination = staging_dir / storage_name
            try:
                shutil.copyfile(source, destination)
            except FileNotFoundError as exc:
                raise HTTPException(
                    409, f"生成版本时源图片已被删除：{asset['filename']}。请刷新图片列表后重试。"
                ) from exc
            if _file_sha256(destination) != asset["sha256"]:
                raise HTTPException(409, f"源图片校验失败：{asset['filename']}")
            destination.chmod(0o444)
            storage_names[asset["id"]] = storage_name

        generated = []; generated_annotations = []; skipped = 0
        if augmentation:
            source_annotations = defaultdict(list)
            for row in annotations: source_annotations[row["asset_id"]].append(row)
            for asset in assets:
                if not planned_copies[asset["id"]]: continue
                with Image.open(staging_dir / storage_names[asset["id"]]) as opened:
                    image = _augmentation_image(opened, asset)
                rows = _transform_annotations(source_annotations[asset["id"]])
                for index, transformed, transformed_rows, metadata, duplicate in _augmented_samples(image, rows, augmentation, project, asset, planned_copies[asset["id"]]):
                    if duplicate: skipped += 1; continue
                    key = (asset["id"], index)
                    storage = f"media/{asset['id']}-aug-{index}.png"
                    destination = staging_dir / storage
                    transformed.save(destination, format="PNG"); destination.chmod(0o444)
                    generated.append({**asset, "id": key, "_original_asset_id": asset["id"], "_variant_index": index, "_augmentation": metadata, "filename": f"{Path(asset['filename']).stem}__aug-{index}.png", "mime_type": "image/png", "width": transformed.width, "height": transformed.height, "size_bytes": destination.stat().st_size, "sha256": _file_sha256(destination)})
                    for row in transformed_rows:
                        generated_annotations.append({**row, "asset_id": key, "points_json": json.dumps(row["points"], ensure_ascii=False, allow_nan=False), "keypoints_json": json.dumps(row["keypoints"], ensure_ascii=False, allow_nan=False)})
                    assignments[key] = "train"; storage_names[key] = storage
            signature = {"original_content_hash": content_hash, "augmentation": augmentation, "engine_version": 1, "variants": [{"original_asset_id": row["_original_asset_id"], "variant_index": row["_variant_index"], "sha256": row["sha256"], "transform": row["_augmentation"]} for row in generated], "annotations": [{"original_annotation_id": row["id"], "source": row["asset_id"], "points": row["points"], "keypoints": row["keypoints"]} for row in generated_annotations]}
            content_hash = hashlib.sha256(json.dumps(signature, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            assets += generated; annotations += generated_annotations
        stats = _snapshot_stats(labels, assets, annotations, assignments)
        if project["task_type"] != "mixed": stats["task_type"] = project["task_type"]
        stats["keypoint_schema"] = _json(project.get("keypoint_schema_json"), {})
        if augmentation:
            counts = {**planned_counts, "generated": len(generated), "final": len(assets), "final_train": planned_counts["original_train"] + len(generated)}
            stats["augmentation"] = {"config": augmentation, "counts": counts, "requested_generated": planned_counts["generated"], "skipped_duplicates": skipped, "engine_version": 1}
            if augmentation.get("target_count") is not None:
                stats["augmentation"]["allocation_version"] = 1

        # Only immutable row insertion and one atomic directory rename remain in
        # this short write transaction.
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise HTTPException(404, "项目已被删除，请返回项目列表")
            next_number = int(
                conn.execute(
                    "SELECT COALESCE(MAX(version_number), 0) + 1 FROM dataset_versions WHERE project_id = ?",
                    (project_id,),
                ).fetchone()[0]
            )
            name = payload.name or f"v{next_number}"
            cursor = conn.execute(
                """INSERT INTO dataset_versions
                   (project_id, version_number, name, description, approved_only, split_seed,
                    train_ratio, valid_ratio, test_ratio, content_hash, asset_count,
                    annotation_count, stats_json, created_by, created_at, task_type, keypoint_schema_json, augmentation_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    next_number,
                    name,
                    payload.description,
                    int(payload.approved_only),
                    payload.split_seed,
                    payload.train_ratio,
                    payload.valid_ratio,
                    payload.test_ratio,
                    content_hash,
                    len(assets),
                    len(annotations),
                    json.dumps(stats, ensure_ascii=False, sort_keys=True),
                    actor.id,
                    now_iso(),
                    project["task_type"],
                    project["keypoint_schema_json"],
                    json.dumps(augmentation, ensure_ascii=False, sort_keys=True),
                ),
            )
            version_id = int(cursor.lastrowid)
            version_dir = settings.versions_dir / f"version-{version_id}"
            if version_dir.exists():
                raise RuntimeError(f"版本目录已存在：{version_dir}")
            staging_dir.rename(version_dir)
            published = True

            label_map: dict[int, int] = {}
            for category_index, label in enumerate(labels):
                label_cursor = conn.execute(
                    """INSERT INTO dataset_version_labels
                       (version_id, original_label_id, category_index, name, color, shortcut,
                        attributes_schema_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        version_id,
                        label["id"],
                        category_index,
                        label["name"],
                        label["color"],
                        label["shortcut"],
                        label["attributes_schema_json"],
                        label["created_at"],
                    ),
                )
                label_map[label["id"]] = int(label_cursor.lastrowid)

            version_asset_map: dict[Any, int] = {}
            for asset in assets:
                asset_cursor = conn.execute(
                    """INSERT INTO dataset_version_assets
                       (version_id, original_asset_id, split, filename, storage_name, mime_type,
                        width, height, size_bytes, sha256, status, review_status, review_reason,
                        revision, source_created_at, source_updated_at, variant_index, source_version_asset_id, augmentation_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        version_id,
                        asset.get("_original_asset_id", asset["id"]),
                        assignments[asset["id"]],
                        asset["filename"],
                        storage_names[asset["id"]],
                        asset["mime_type"],
                        asset["width"],
                        asset["height"],
                        asset["size_bytes"],
                        asset["sha256"],
                        asset["status"],
                        asset["review_status"],
                        asset["review_reason"],
                        asset["revision"],
                        asset["created_at"],
                        asset["updated_at"],
                        asset.get("_variant_index", 0),
                        version_asset_map.get(asset.get("_original_asset_id")),
                        json.dumps(asset.get("_augmentation", {}), ensure_ascii=False, sort_keys=True),
                    ),
                )
                version_asset_map[asset["id"]] = int(asset_cursor.lastrowid)

            for annotation in annotations:
                conn.execute(
                    """INSERT INTO dataset_version_annotations
                       (version_id, version_asset_id, version_label_id, original_annotation_id,
                        shape_type, points_json, attributes_json, confidence, source, model_id,
                        source_version, created_by, updated_by, source_created_at, source_updated_at, keypoints_json, human_modified)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        version_id,
                        version_asset_map[annotation["asset_id"]],
                        label_map[annotation["label_id"]],
                        annotation["id"],
                        annotation["shape_type"],
                        annotation["points_json"],
                        annotation["attributes_json"],
                        annotation["confidence"],
                        annotation["source"],
                        annotation["model_id"],
                        annotation["version"],
                        annotation["created_by"],
                        annotation["updated_by"],
                        annotation["created_at"],
                        annotation["updated_at"],
                        annotation.get("keypoints_json", "[]"),
                        annotation.get("human_modified", 0),
                    ),
                )
            conn.execute(
                """INSERT INTO audit_logs
                   (actor, action, resource_type, resource_id, project_id, payload_json, created_at)
                   VALUES (?, 'create', 'dataset_version', ?, ?, ?, ?)""",
                (
                    actor.id,
                    str(version_id),
                    project_id,
                    json.dumps(
                        {
                            "version_number": next_number,
                            "content_hash": content_hash,
                            "asset_count": len(assets),
                            "annotation_count": len(annotations),
                        },
                        ensure_ascii=False,
                    ),
                    now_iso(),
                ),
            )
        committed = True
        return get_version(version_id, actor)
    except Exception:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        if published and not committed and version_dir and version_dir.exists():
            shutil.rmtree(version_dir)
        raise


@router.get("/versions/{version_id}")
def get_version(version_id: int, _: Actor = Depends(get_actor)) -> dict[str, Any]:
    return _version_result(_require_version(version_id), detailed=True)


def _version_asset_result(version_id: int, row: dict[str, Any]) -> dict[str, Any]:
    return {**row,
            "thumbnail_url": f"/api/versions/{version_id}/assets/{row['id']}/thumbnail",
            "image_url": f"/api/versions/{version_id}/assets/{row['id']}/image"}


@router.get("/versions/{version_id}/assets")
def version_assets(
    version_id: int,
    split: Literal["all", "train", "valid", "test"] = "all",
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=48, ge=1, le=96),
    _: Actor = Depends(get_actor),
) -> dict[str, Any]:
    _require_version(version_id)
    condition = "a.version_id = ?" + (" AND a.split = ?" if split != "all" else "")
    params = (version_id, split) if split != "all" else (version_id,)
    total = one(f"SELECT COUNT(*) AS count FROM dataset_version_assets a WHERE {condition}", params)["count"]
    rows = all_rows(
        f"""SELECT a.id,a.filename,a.split,a.width,a.height,a.review_status,a.variant_index,a.original_asset_id,a.source_version_asset_id,
            (SELECT COUNT(*) FROM dataset_version_annotations n
             WHERE n.version_id=a.version_id AND n.version_asset_id=a.id) AS annotation_count
            FROM dataset_version_assets a WHERE {condition} ORDER BY a.id LIMIT ? OFFSET ?""",
        (*params, limit, offset),
    )
    return {"version_id": version_id, "split": split, "total": total,
            "offset": offset, "limit": limit, "has_more": offset + len(rows) < total,
            "items": [_version_asset_result(version_id, row) for row in rows]}


def _version_image(version_id: int, asset_id: int) -> tuple[dict[str, Any], Path]:
    asset = one("SELECT * FROM dataset_version_assets WHERE id=? AND version_id=?", (asset_id, version_id))
    if asset is None:
        raise HTTPException(404, "这个版本中没有这张图片")
    root = (settings.versions_dir / f"version-{version_id}").resolve()
    source = (root / asset["storage_name"]).resolve()
    if root not in source.parents or not source.is_file():
        raise HTTPException(404, "版本中的图片文件暂不可用，请联系管理员检查")
    return asset, source


@router.get("/versions/{version_id}/assets/{asset_id}/image")
def version_image(version_id: int, asset_id: int, _: Actor = Depends(get_actor)) -> FileResponse:
    asset, source = _version_image(version_id, asset_id)
    return FileResponse(source, media_type=asset["mime_type"], headers={"Cache-Control": "private, max-age=3600"})


@router.get("/versions/{version_id}/assets/{asset_id}/thumbnail")
def version_thumbnail(
    version_id: int, asset_id: int,
    size: int = Query(default=320, ge=64, le=768),
    _: Actor = Depends(get_actor),
) -> FileResponse:
    _, source = _version_image(version_id, asset_id)
    cache = settings.thumbnail_dir / f"version-{version_id}-{asset_id}-{size}.webp"
    if not cache.is_file():
        temporary = cache.with_name(f".{cache.name}.{uuid.uuid4().hex}.tmp")
        try:
            with Image.open(source) as opened:
                image = ImageOps.exif_transpose(opened)
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGB")
                image.thumbnail((size, size), Image.Resampling.LANCZOS, reducing_gap=3.0)
                image.save(temporary, "WEBP", quality=82, method=4)
            temporary.replace(cache)
        except (OSError, ValueError) as error:
            temporary.unlink(missing_ok=True)
            raise HTTPException(404, "版本图片暂时无法显示，请联系管理员检查文件") from error
    return FileResponse(cache, media_type="image/webp", headers={"Cache-Control": "private, max-age=31536000, immutable"})


def _safe_export_name(asset: dict[str, Any]) -> str:
    name = Path(asset["filename"]).name.replace("/", "_").replace("\\", "_")
    if asset.get("variant_index", 0):
        return f"{asset['original_asset_id']}_aug{asset['variant_index']}_{name}"
    return f"{asset['original_asset_id']}_{name}"


def _bbox(points: list[float]) -> tuple[float, float, float, float]:
    xs, ys = points[0::2], points[1::2]
    return min(xs), min(ys), max(xs), max(ys)


def _polygon_area(points: list[float]) -> float:
    vertices = list(zip(points[0::2], points[1::2]))
    return abs(
        sum(
            x1 * vertices[(index + 1) % len(vertices)][1]
            - vertices[(index + 1) % len(vertices)][0] * y1
            for index, (x1, y1) in enumerate(vertices)
        )
    ) / 2


def _load_snapshot_rows(version_id: int) -> tuple[dict, list[dict], list[dict], list[dict]]:
    # A project can be deleted while an export is being assembled. Keep all
    # metadata from one read snapshot; missing media is checked by the exporter.
    with db() as conn:
        conn.execute("BEGIN")
        row = conn.execute("SELECT * FROM dataset_versions WHERE id=?", (version_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "数据集版本不存在")
        version = dict(row)
        labels = [dict(row) for row in conn.execute(
            "SELECT * FROM dataset_version_labels WHERE version_id = ? ORDER BY category_index", (version_id,))]
        assets = [dict(row) for row in conn.execute(
            "SELECT * FROM dataset_version_assets WHERE version_id = ? ORDER BY id", (version_id,))]
        annotations = [dict(row) for row in conn.execute(
            """SELECT n.*, a.split, a.original_asset_id, l.category_index
               FROM dataset_version_annotations n
               JOIN dataset_version_assets a ON a.id = n.version_asset_id
               JOIN dataset_version_labels l ON l.id = n.version_label_id
               WHERE n.version_id = ? ORDER BY n.id""", (version_id,))]
    return version, labels, assets, annotations


def _write_version_coco(
    archive: zipfile.ZipFile,
    labels: list[dict],
    assets: list[dict],
    annotations: list[dict],
    keypoint_schema: dict | None = None,
) -> None:
    categories = [
        {
            "id": label["category_index"] + 1,
            "name": label["name"],
            "visionforge_attributes_schema": _json(label["attributes_schema_json"], {}),
        }
        for label in labels
    ]
    if keypoint_schema:
        for category in categories:
            category["keypoints"] = keypoint_schema["names"]
            category["skeleton"] = [[a+1,b+1] for a,b in keypoint_schema.get("skeleton", [])]
    annotations_by_asset: defaultdict[int, list[dict]] = defaultdict(list)
    augmented = any(asset.get("variant_index", 0) for asset in assets)
    for annotation in annotations:
        annotations_by_asset[annotation["version_asset_id"]].append(annotation)
    for split in SPLITS:
        split_assets = [asset for asset in assets if asset["split"] == split]
        images: list[dict] = []
        coco_annotations: list[dict] = []
        next_annotation_id = 1
        for asset in split_assets:
            export_name = _safe_export_name(asset)
            image_id = asset["id"] if augmented else asset["original_asset_id"]
            images.append(
                {
                    "id": image_id,
                    "file_name": f"images/{split}/{export_name}",
                    "width": asset["width"],
                    "height": asset["height"],
                }
            )
            for row in annotations_by_asset[asset["id"]]:
                points = _json(row["points_json"], [])
                x1, y1, x2, y2 = _bbox(points)
                item = {
                    "id": next_annotation_id,
                    "image_id": image_id,
                    "category_id": row["category_index"] + 1,
                    "bbox": [x1, y1, x2 - x1, y2 - y1],
                    "area": (
                        _polygon_area(points)
                        if row["shape_type"] == "polygon"
                        else (x2 - x1) * (y2 - y1)
                    ),
                    "iscrowd": 0,
                    "attributes": _json(row["attributes_json"], {}),
                    "visionforge": {
                        "original_annotation_id": row["original_annotation_id"],
                        "source": row["source"],
                        "model_id": row["model_id"],
                        "confidence": row["confidence"],
                        "version": row["source_version"],
                    },
                }
                if row["shape_type"] == "keypoints":
                    keypoints = _json(row.get("keypoints_json"), [])
                    item["keypoints"] = [value for point in keypoints for value in (point["x"],point["y"],point["visibility"])]
                    item["num_keypoints"] = sum(point["visibility"] > 0 for point in keypoints)
                if row["shape_type"] == "polygon":
                    item["segmentation"] = [points]
                coco_annotations.append(item)
                next_annotation_id += 1
        archive.writestr(
            f"annotations/instances_{split}.json",
            json.dumps(
                {"images": images, "annotations": coco_annotations, "categories": categories},
                ensure_ascii=False,
                indent=2,
            ),
        )


def _yolo_line(row: dict, asset: dict, segmentation_task: bool) -> str:
    points = _json(row["points_json"], [])
    class_id = row["category_index"]
    if segmentation_task:
        if row["shape_type"] == "rectangle":
            x1, y1, x2, y2 = points
            points = [x1, y1, x2, y1, x2, y2, x1, y2]
        coords = [
            str(round(value / (asset["width"] if index % 2 == 0 else asset["height"]), 6))
            for index, value in enumerate(points)
        ]
        return f"{class_id} " + " ".join(coords)
    x1, y1, x2, y2 = _bbox(points)
    return " ".join(
        map(
            str,
            [
                class_id,
                round((x1 + x2) / 2 / asset["width"], 6),
                round((y1 + y2) / 2 / asset["height"], 6),
                round((x2 - x1) / asset["width"], 6),
                round((y2 - y1) / asset["height"], 6),
            ],
        )
    )


def _write_version_yolo(
    archive: zipfile.ZipFile,
    version: dict,
    labels: list[dict],
    assets: list[dict],
    annotations: list[dict],
    task_override: str | None = None,
) -> None:
    annotations_by_asset: defaultdict[int, list[dict]] = defaultdict(list)
    for row in annotations:
        annotations_by_asset[row["version_asset_id"]].append(row)
    stats = _json(version.get("stats_json"), {})
    segmentation_task = (
        task_override == "segment"
        if task_override is not None
        else stats.get("task_type") == "segment"
    )
    directory_map = {"train": "train", "valid": "val", "test": "test"}
    for asset in assets:
        split_dir = directory_map[asset["split"]]
        export_name = _safe_export_name(asset)
        label_name = f"{Path(export_name).stem}.txt"
        lines = []
        for row in annotations_by_asset[asset["id"]]:
            line = _yolo_line(row, asset, segmentation_task)
            if row["shape_type"] == "keypoints":
                for point in _json(row.get("keypoints_json"), []):
                    line += f" {point['x']/asset['width']:.6f} {point['y']/asset['height']:.6f} {point['visibility']}"
            lines.append(line)
        archive.writestr(f"labels/{split_dir}/{label_name}", "\n".join(lines))
    names = ", ".join(json.dumps(label["name"], ensure_ascii=False) for label in labels)
    archive.writestr(
        "data.yaml",
        "path: .\n"
        "train: images/train\n"
        "val: images/val\n"
        "test: images/test\n"
        f"nc: {len(labels)}\n"
        f"names: [{names}]\n" + (
            "kpt_shape: " + json.dumps([len(_json(version.get("keypoint_schema_json"), {}).get("names", [])), 3]) + "\n"
            + "flip_idx: " + json.dumps(_json(version.get("keypoint_schema_json"), {}).get("flip_idx") or []) + "\n"
            + "kpt_names: " + json.dumps({str(i):_json(version.get("keypoint_schema_json"), {}).get("names", []) for i in range(len(labels))}, ensure_ascii=False) + "\n"
            if (task_override or stats.get("task_type")) == "pose" else ""
        ),
    )

    if (task_override or stats.get("task_type")) == "pose":
        schema = _json(version.get("keypoint_schema_json"), {})
        # Rewrite once with keypoint metadata so the ZIP has no duplicate data.yaml entries.
        archive.writestr("keypoints.json", json.dumps(schema, ensure_ascii=False))


def _class_directory(label: dict) -> str:
    import re
    safe = re.sub(r"[^\w.-]+", "_", label["name"]).strip(".")[:60] or "class"
    return f"{label['category_index']:04d}_{safe}"


def build_version_export(
    version_id: int,
    export_format: str,
    destination: Path,
    *,
    yolo_task: str | None = None,
    live_snapshot: tuple | None = None,
) -> None:
    if export_format not in {"coco", "yolo", "json"}:
        raise ValueError("unsupported export format")
    if yolo_task not in {None, "detect", "segment", "classify", "pose"}:
        raise ValueError("yolo_task must be detect, segment, classify or pose")
    if export_format != "yolo" and yolo_task is not None:
        raise ValueError("yolo_task is only valid for YOLO exports")
    version, labels, assets, annotations = live_snapshot or _load_snapshot_rows(version_id)
    task = yolo_task or _json(version.get("stats_json"), {}).get("task_type", "detect")
    if yolo_task and (task in {"classify", "pose"} or version.get("task_type") in {"classify", "pose"}) and version.get("task_type") != task:
        raise ValueError("数据版本与导出任务不兼容")
    if task == "classify" and export_format == "coco":
        raise ValueError("分类任务请导出分类目录 ZIP（yolo）或 JSON")
    by_asset = {row["version_asset_id"]:row for row in annotations}
    label_by_index = {row["category_index"]:row for row in labels}
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        if export_format == "json":
            archive.writestr("annotations.json", json.dumps({"task_type":task,
                "keypoint_schema":_json(version.get("keypoint_schema_json"),{}),
                "labels":labels, "assets":[{**asset,"file_name":f"images/{asset['split']}/{_safe_export_name(asset)}"} for asset in assets],
                "annotations":[{**row,"points":_json(row["points_json"],[]),"keypoints":_json(row.get("keypoints_json"),[]),"attributes":_json(row.get("attributes_json"),{})} for row in annotations]}, ensure_ascii=False))
            split_dirs = {split:split for split in SPLITS}
        elif task == "classify":
            if len(by_asset) != len(assets):
                raise ValueError("分类导出要求每张图片恰有一个分类，请先完成 AI 标注或人工校正")
            split_dirs = {"train":"train", "valid":"val", "test":"test"}
            for split in SPLITS:
                for label in labels:
                    archive.writestr(f"{split_dirs[split]}/{_class_directory(label)}/", "")
            archive.writestr("classes.json", json.dumps([{ "index":label["category_index"], "name":label["name"], "directory":_class_directory(label)} for label in labels], ensure_ascii=False))
        elif export_format == "coco":
            _write_version_coco(archive, labels, assets, annotations, _json(version.get("keypoint_schema_json"), {}))
            split_dirs = {split: split for split in SPLITS}
        else:
            _write_version_yolo(
                archive, version, labels, assets, annotations, task_override=yolo_task
            )
            split_dirs = {"train": "train", "valid": "val", "test": "test"}
        version_dir = settings.media_dir if live_snapshot is not None else settings.versions_dir / f"version-{version_id}"
        version_root = version_dir.resolve()
        for asset in assets:
            source = (version_dir / asset["storage_name"]).resolve()
            if (
                version_root not in source.parents
                or not source.is_file()
                or _file_sha256(source) != asset["sha256"]
            ):
                raise RuntimeError(f"版本快照文件缺失或校验失败：{asset['filename']}")
            export_path = f"images/{split_dirs[asset['split']]}/{_safe_export_name(asset)}"
            if task == "classify" and export_format == "yolo":
                label = label_by_index[by_asset[asset["id"]]["category_index"]]
                export_path = f"{split_dirs[asset['split']]}/{_class_directory(label)}/{_safe_export_name(asset)}"
            archive.write(source, export_path)
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "version_id": version_id,
                    "project_id": version["project_id"],
                    "version_number": version["version_number"],
                    "content_hash": version["content_hash"],
                    "augmentation": _json(version.get("augmentation_json"), None),
                    "created_at": version["created_at"],
                    "format": export_format,
                    "task": yolo_task or _json(version["stats_json"], {}).get("task_type"),
                    "stats": _json(version["stats_json"], {}),
                    "assets": [
                        {
                            "original_asset_id": asset["original_asset_id"],
                            "version_asset_id": asset["id"],
                            "variant_index": asset.get("variant_index", 0),
                            "source_version_asset_id": asset.get("source_version_asset_id"),
                            "augmentation": _json(asset.get("augmentation_json"), {}),
                            "split": asset["split"],
                            "sha256": asset["sha256"],
                            "revision": asset["revision"],
                        }
                        for asset in assets
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )


def build_project_export(project_id: int, export_format: str, destination: Path, approved_only: bool = False) -> None:
    with db() as conn:
        conn.execute("BEGIN")
        project = dict(conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())
        labels = [dict(row) for row in conn.execute("SELECT * FROM labels WHERE project_id=? ORDER BY id", (project_id,))]
        assets = [dict(row) for row in conn.execute("SELECT * FROM assets WHERE project_id=? ORDER BY id", (project_id,))
                  if not approved_only or row["review_status"] == "approved"]
        if not assets:
            raise HTTPException(409, "没有符合导出条件的图片")
        asset_ids = {row["id"] for row in assets}
        annotations = [dict(row) for row in conn.execute("SELECT n.* FROM annotations n JOIN assets a ON a.id=n.asset_id WHERE a.project_id=? AND n.deleted_at IS NULL", (project_id,)) if row["asset_id"] in asset_ids]
    assignments = _assign_splits(assets, 42, {"train":0.8,"valid":0.2,"test":0})
    stats = _snapshot_stats(labels, assets, annotations, assignments)
    if project["task_type"] != "mixed":
        stats["task_type"] = project["task_type"]
    categories = {label["id"]:index for index,label in enumerate(labels)}
    for label in labels:
        label["category_index"] = categories[label["id"]]
    for asset in assets:
        asset.update(original_asset_id=asset["id"], split=assignments[asset["id"]])
    for row in annotations:
        row.update(version_asset_id=row["asset_id"], category_index=categories[row["label_id"]],
                   original_annotation_id=row["id"], source_version=row["version"])
    version = {"project_id":project_id,"version_number":None,"content_hash":None,"created_at":now_iso(),
               "stats_json":json.dumps(stats),"task_type":project["task_type"],"keypoint_schema_json":project["keypoint_schema_json"]}
    build_version_export(0, export_format, destination, live_snapshot=(version,labels,assets,annotations))


@router.get("/versions/{version_id}/export/{export_format}")
def export_version(
    version_id: int,
    export_format: str,
    background_tasks: BackgroundTasks,
    _: Actor = Depends(get_actor),
) -> FileResponse:
    version = _require_version(version_id)
    if export_format not in {"coco", "yolo", "json"}:
        raise HTTPException(400, "仅支持 coco、yolo 或 json")
    handle = tempfile.NamedTemporaryFile(
        prefix=f"mdp-v{version_id}-", suffix=f"-{export_format}.zip", delete=False
    )
    path = Path(handle.name)
    handle.close()
    try:
        build_version_export(version_id, export_format, path)
    except Exception as exc:
        path.unlink(missing_ok=True)
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(409, str(exc)) from exc
    background_tasks.add_task(path.unlink, missing_ok=True)
    filename = f"project-{version['project_id']}-v{version['version_number']}-{export_format}.zip"
    return FileResponse(path, media_type="application/zip", filename=filename)
