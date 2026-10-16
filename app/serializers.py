from __future__ import annotations

import json
from typing import Any


def annotation(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["points"] = json.loads(result.pop("points_json"))
    result["attributes"] = json.loads(result.pop("attributes_json", "{}"))
    result["keypoints"] = json.loads(result.pop("keypoints_json", "[]"))
    result["human_modified"] = bool(result.get("human_modified", False))
    return result


def job(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["settings"] = json.loads(result.pop("settings_json", "{}"))
    return result
