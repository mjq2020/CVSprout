"""Export self-contained, checksum-verified prediction service bundles."""
from __future__ import annotations

import json
import tempfile
import zipfile

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from .auth import Actor, get_actor
from .config import BASE_DIR

router = APIRouter(prefix="/api", tags=["deployment"])


@router.get("/training-runs/{run_id}/deployment")
def bundle(run_id: int, actor: Actor = Depends(get_actor)):
    from .training import _validated_artifact, _parse_json, _file_sha256
    from .database import db
    with db() as conn:
        conn.execute("BEGIN")
        row = conn.execute("SELECT * FROM training_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "训练记录不存在")
        run = dict(row)
        labels = [dict(row) for row in conn.execute(
            "SELECT category_index,name FROM dataset_version_labels WHERE version_id=? ORDER BY category_index",
            (run["version_id"],))]
    if run["status"] != "completed":
        raise HTTPException(409, "训练完成后才能下载部署包")
    path = _validated_artifact(run.get("artifact_path"), "训练", _parse_json(run.get("artifact_metadata_json")))
    config = _parse_json(run["settings_json"])
    try:
        checksum = _file_sha256(path)
    except FileNotFoundError as exc:
        raise HTTPException(409, "训练文件或所属项目已被删除，请刷新后重试") from exc
    manifest = {"schema_version": 1, "task": run["task"], "image_size": config.get("image_size", 640),
                "sha256": checksum, "run_id": run_id, "labels": labels,
                "preprocessing": "Ultralytics pinned runtime handles task-specific preprocessing and postprocessing",
                "outputs": "Ultralytics Results.summary; coordinates are pixels in the original image"}
    temporary = tempfile.NamedTemporaryFile(prefix="cvsprout-deploy-", suffix=".zip", delete=False)
    temporary.close()
    from pathlib import Path
    destination = Path(temporary.name)
    try:
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
            for filename in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
                archive.write(BASE_DIR / filename, filename)
            archive.write(path, "model/model.pt")
            archive.writestr("model/manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            archive.writestr("app/__init__.py", "")
            archive.write(BASE_DIR / "app" / "http_limits.py", "app/http_limits.py")
            for filename in ("__init__.py", "server.py"):
                archive.write(BASE_DIR / "app" / "runtime" / filename, f"app/runtime/{filename}")
            for filename in ("web.txt", "runtime-cpu.txt", "runtime-gpu.txt"):
                archive.write(BASE_DIR / "requirements" / filename, f"requirements/{filename}")
            for source in (BASE_DIR / "deploy" / "runtime").iterdir():
                if source.is_file():
                    archive.write(source, source.name)
        return FileResponse(destination, media_type="application/zip", filename=f"cvsprout-model-{run_id}.zip",
                            background=BackgroundTask(destination.unlink, missing_ok=True))
    except FileNotFoundError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(409, "部署包所需文件已被删除或缺失，请刷新后重试") from exc
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
