from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from .auth import Actor, get_actor
from .config import settings
from .database import all_rows, now_iso, one


router = APIRouter(prefix="/api", tags=["analytics"])


def _require_project(project_id: int) -> None:
    if not one("SELECT id FROM projects WHERE id = ?", (project_id,)):
        raise HTTPException(404, "项目不存在")


def _counter(rows: list[dict[str, Any]], key: str, value: str = "count") -> dict[str, int]:
    return {str(row[key]): int(row[value]) for row in rows}


def project_analytics(project_id: int) -> dict[str, Any]:
    _require_project(project_id)
    summary = one(
        """SELECT COUNT(*) AS asset_count,
                  COALESCE(SUM(CASE WHEN status = 'labeled' THEN 1 ELSE 0 END), 0) AS labeled_count,
                  COALESCE(SUM(CASE WHEN status = 'unlabeled' THEN 1 ELSE 0 END), 0) AS unlabeled_count,
                  COALESCE(SUM(size_bytes), 0) AS total_size_bytes
           FROM assets WHERE project_id = ?""",
        (project_id,),
    ) or {"asset_count": 0, "labeled_count": 0, "unlabeled_count": 0, "total_size_bytes": 0}
    annotation_summary = one(
        """SELECT COUNT(n.id) AS annotation_count,
                  COUNT(DISTINCT n.asset_id) AS annotated_asset_count,
                  COALESCE(AVG(n.confidence), 0) AS average_confidence
           FROM annotations n JOIN assets a ON a.id = n.asset_id
           WHERE a.project_id = ? AND n.deleted_at IS NULL""",
        (project_id,),
    ) or {"annotation_count": 0, "annotated_asset_count": 0, "average_confidence": 0}
    label_count = one("SELECT COUNT(*) AS count FROM labels WHERE project_id = ?", (project_id,))
    summary.update(annotation_summary)
    summary["label_count"] = int(label_count["count"] if label_count else 0)
    summary["annotation_coverage"] = (
        round(summary["annotated_asset_count"] / summary["asset_count"], 4)
        if summary["asset_count"]
        else 0
    )

    workflow = _counter(
        all_rows(
            "SELECT status, COUNT(*) AS count FROM assets WHERE project_id = ? GROUP BY status",
            (project_id,),
        ),
        "status",
    )
    review = _counter(
        all_rows(
            """SELECT review_status, COUNT(*) AS count FROM assets
               WHERE project_id = ? GROUP BY review_status""",
            (project_id,),
        ),
        "review_status",
    )
    workflow = {key: workflow.get(key, 0) for key in ("unlabeled", "labeled")}
    review = {
        key: review.get(key, 0)
        for key in ("draft", "in_progress", "submitted", "approved", "rejected")
    }
    sources = _counter(
        all_rows(
            """SELECT n.source, COUNT(*) AS count FROM annotations n
               JOIN assets a ON a.id = n.asset_id
               WHERE a.project_id = ? AND n.deleted_at IS NULL GROUP BY n.source""",
            (project_id,),
        ),
        "source",
    )
    shape_types = _counter(
        all_rows(
            """SELECT n.shape_type, COUNT(*) AS count FROM annotations n
               JOIN assets a ON a.id = n.asset_id
               WHERE a.project_id = ? AND n.deleted_at IS NULL GROUP BY n.shape_type""",
            (project_id,),
        ),
        "shape_type",
    )
    labels = all_rows(
        """SELECT l.id, l.name, l.color, l.shortcut,
                  COUNT(n.id) AS object_count,
                  COUNT(DISTINCT n.asset_id) AS image_count,
                  COALESCE(AVG(n.confidence), 0) AS average_confidence
           FROM labels l
           LEFT JOIN annotations n ON n.label_id = l.id AND n.deleted_at IS NULL
           WHERE l.project_id = ? GROUP BY l.id ORDER BY object_count DESC, l.id""",
        (project_id,),
    )

    assets = all_rows(
        """SELECT a.*, COUNT(n.id) AS active_annotations
           FROM assets a LEFT JOIN annotations n
             ON n.asset_id = a.id AND n.deleted_at IS NULL
           WHERE a.project_id = ? GROUP BY a.id""",
        (project_id,),
    )
    missing_files = sum(
        not (settings.media_dir / Path(asset["storage_name"]).name).is_file()
        for asset in assets
    )
    empty_labeled = sum(asset["status"] == "labeled" and not asset["active_annotations"] for asset in assets)
    approved_without_annotations = sum(
        asset["review_status"] == "approved" and not asset["active_annotations"] for asset in assets
    )
    duplicate_groups = one(
        """SELECT COUNT(*) AS count FROM (
               SELECT sha256 FROM assets WHERE project_id = ? AND sha256 != ''
               GROUP BY sha256 HAVING COUNT(*) > 1
           )""",
        (project_id,),
    )
    low_confidence = one(
        """SELECT COUNT(*) AS count FROM annotations n JOIN assets a ON a.id = n.asset_id
           WHERE a.project_id = ? AND n.deleted_at IS NULL AND n.source != 'manual'
             AND n.confidence IS NOT NULL AND n.confidence < 0.35""",
        (project_id,),
    )
    tiny_count = 0
    annotation_rows = all_rows(
        """SELECT n.points_json, n.shape_type, a.width, a.height FROM annotations n
           JOIN assets a ON a.id = n.asset_id
           WHERE a.project_id = ? AND n.deleted_at IS NULL""",
        (project_id,),
    )
    for row in annotation_rows:
        if row["shape_type"] == "classification":
            continue
        try:
            points = json.loads(row["points_json"])
            xs, ys = points[0::2], points[1::2]
            relative_area = ((max(xs) - min(xs)) * (max(ys) - min(ys))) / (
                row["width"] * row["height"]
            )
            if relative_area < 0.0001:
                tiny_count += 1
        except (ValueError, TypeError, ZeroDivisionError):
            tiny_count += 1
    duplicate_group_count = int(duplicate_groups["count"] if duplicate_groups else 0)
    low_confidence_count = int(low_confidence["count"] if low_confidence else 0)
    quality = {
        "missing_file_count": missing_files,
        "empty_labeled_count": empty_labeled,
        "approved_without_annotations_count": approved_without_annotations,
        "duplicate_hash_group_count": duplicate_group_count,
        "low_confidence_auto_count": low_confidence_count,
        "tiny_annotation_count": tiny_count,
        "healthy": not any(
            (
                missing_files,
                empty_labeled,
                approved_without_annotations,
                duplicate_group_count,
                low_confidence_count,
                tiny_count,
            )
        ),
    }
    versions = one(
        """SELECT COUNT(*) AS count, MAX(version_number) AS latest_version_number,
                  MAX(created_at) AS latest_created_at
           FROM dataset_versions WHERE project_id = ?""",
        (project_id,),
    ) or {"count": 0, "latest_version_number": None, "latest_created_at": None}
    training_rows = all_rows(
        """SELECT status, COUNT(*) AS count FROM training_runs
           WHERE project_id = ? GROUP BY status""",
        (project_id,),
    )
    return {
        "project_id": project_id,
        "generated_at": now_iso(),
        "summary": summary,
        "workflow": workflow,
        "review": review,
        "annotation_sources": sources,
        "shape_types": shape_types,
        "labels": labels,
        "quality": quality,
        "versions": versions,
        "training": {"by_status": _counter(training_rows, "status")},
    }


@router.get("/projects/{project_id}/analytics")
def analytics(project_id: int, _: Actor = Depends(get_actor)) -> dict[str, Any]:
    return project_analytics(project_id)


@router.get("/projects/{project_id}/quality")
def quality(project_id: int, _: Actor = Depends(get_actor)) -> dict[str, Any]:
    result = project_analytics(project_id)
    return {
        "project_id": project_id,
        "generated_at": result["generated_at"],
        **result["quality"],
    }
