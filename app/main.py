from __future__ import annotations

import asyncio
import json
import math
import os
import zipfile
import hashlib
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, Cookie, Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from PIL import Image, ImageOps

from .config import BASE_DIR, settings
from .auth import Actor, create_session_identity, get_actor, require_reviewer, require_admin, password_change_required
from .analytics import router as analytics_router
from .accounts import router as accounts_router
from .review import router as review_router
from .asset_management import router as asset_management_router
from .security import browser_security, request_budget
from .http_limits import RequestBodyLimit
from .collaboration import active_job_for_assets, guard_asset, guard_job_targets
from .task_types import validate_task_annotation
from .database import all_rows, audit, db, init_db, now_iso, one
from .dataset_versions import router as dataset_versions_router, build_project_export
from .jobs import JobWorker
from .annotation_preview import run_preview
from .providers import get_model, get_provider, list_models, seed_builtin_models
from .providers.local import (
    grounding_labels_from_prompt,
    local_runtime_status,
    local_sam3_runtime_status,
)
from .schemas import (
    AnnotationCreate,
    AnnotationPreviewCreate,
    AnnotationUpdate,
    AssetWorkflowUpdate,
    JobCreate,
    LabelCreate,
    ModelServiceUpsert,
    ProjectCreate,
    ProjectArchiveUpdate,
)
from .serializers import annotation as serialize_annotation
from .serializers import job as serialize_job
from .storage import unpack_upload
from .training import PipelineWorker, router as training_router
from .setup import router as setup_router, required as setup_required
from .model_downloads import router as model_downloads_router
from .deployment import router as deployment_router


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings.validate()
    init_db()
    seed_builtin_models()
    worker = JobWorker() if settings.embedded_worker else None
    worker_task = asyncio.create_task(worker.run_forever()) if worker else None
    pipeline_worker = (
        PipelineWorker("embedded-web")
        if settings.training_enabled and settings.embedded_pipeline_worker
        else None
    )
    pipeline_task = (
        asyncio.create_task(pipeline_worker.run_forever()) if pipeline_worker else None
    )
    application.state.background_tasks = [task for task in (worker_task, pipeline_task) if task is not None]
    try:
        yield
    finally:
        if worker and worker_task:
            worker.stop()
            try:
                await asyncio.wait_for(worker_task, timeout=5)
            except TimeoutError:
                worker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await worker_task
        if pipeline_worker and pipeline_task:
            pipeline_worker.stop()
            try:
                await asyncio.wait_for(pipeline_task, timeout=5)
            except TimeoutError:
                pipeline_task.cancel()
                with suppress(asyncio.CancelledError):
                    await pipeline_task


app = FastAPI(title="CVSprout 标注平台", version="0.20.0", lifespan=lifespan)
app.middleware("http")(browser_security)
app.add_middleware(RequestBodyLimit, policy=request_budget)
app.include_router(accounts_router)
app.include_router(review_router)
app.include_router(asset_management_router)
app.include_router(analytics_router)
app.include_router(dataset_versions_router)
app.include_router(training_router)
app.include_router(setup_router)
app.include_router(model_downloads_router)
app.include_router(deployment_router)


@app.exception_handler(RequestValidationError)
async def validation_error_without_inputs(request: Request, exc: RequestValidationError):
    # Pydantic's default error payload includes the rejected input, including
    # passwords. Clients need the field and explanation, never the original value.
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]})


def require_project(project_id: int) -> dict:
    project = one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if not project:
        raise HTTPException(404, "项目不存在")
    project["keypoint_schema"] = json.loads(project.get("keypoint_schema_json") or "{}")
    return project


def require_asset(asset_id: int) -> dict:
    asset = one("SELECT * FROM assets WHERE id = ?", (asset_id,))
    if not asset:
        raise HTTPException(404, "图片不存在")
    return asset


def ensure_asset_write_access(asset: dict, actor: Actor) -> None:
    if (
        asset.get("lock_owner")
        and asset["lock_owner"] != actor.id
        and asset.get("lock_expires_at")
        and asset["lock_expires_at"] > now_iso()
    ):
        raise HTTPException(423, "图片正由其他成员编辑，请稍后重试")


def ensure_asset_annotation_editable(asset: dict, actor: Actor) -> None:
    ensure_asset_write_access(asset, actor)
    if asset.get("review_status") in {"submitted", "approved"}:
        raise HTTPException(409, "图片已提交或审核通过，请先重新打开再修改标注")


@app.get("/api/health")
def health() -> dict:
    from datetime import UTC, datetime, timedelta

    cutoff = (
        datetime.now(UTC)
        - timedelta(seconds=max(10, settings.job_lease_seconds, settings.pipeline_lease_seconds) * 2)
    ).isoformat()
    active_workers = all_rows(
        "SELECT * FROM workers WHERE status != 'offline' AND last_seen_at >= ?",
        (cutoff,),
    )
    pipeline_workers = []
    annotation_workers = []
    for worker in active_workers:
        try:
            metadata = json.loads(worker.get("metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        target = pipeline_workers if metadata.get("worker_type") == "pipeline" else annotation_workers
        target.append(worker)
    return {
        "status": "ok",
        "version": app.version,
        "auth_mode": settings.auth_mode,
        "setup_required": setup_required(),
        "annotation_workers": {
            "status": "online" if annotation_workers else "not_seen",
            "count": len(annotation_workers),
        },
        "pipeline_workers": {
            "status": "online" if pipeline_workers else "not_seen",
            "count": len(pipeline_workers),
        },
    }


@app.get("/api/ready")
async def ready(request: Request):
    """Readiness includes local background tasks or the private compute service."""
    from starlette.concurrency import run_in_threadpool
    try:
        await run_in_threadpool(one, "SELECT 1")
    except Exception:
        return JSONResponse({"status": "unavailable", "component": "database"}, status_code=503)
    tasks = getattr(request.app.state, "background_tasks", None)
    if tasks is None or any(task.done() for task in tasks):
        return JSONResponse({"status": "unavailable", "component": "background_tasks"}, status_code=503)
    if settings.worker_url:
        try:
            async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                response = await client.get(f"{settings.worker_url}/health")
            payload = response.json() if response.status_code == 200 else None
            if not isinstance(payload, dict) or payload.get("status") != "ok":
                raise ValueError("Worker not ready")
        except (httpx.HTTPError, ValueError):
            return JSONResponse({"status": "unavailable", "component": "worker"}, status_code=503)
    return {"status": "ok"}


@app.get("/api/session")
def session(actor: Actor = Depends(get_actor)) -> dict:
    return {"actor": actor.id, "role": actor.role, "display_name": actor.display_name, "user_id": actor.user_id,
            "username": getattr(actor, "username", ""), "must_change_password": getattr(actor, "must_change_password", False)}


@app.post("/api/session")
def create_session(
    response: Response,
    actor: Actor = Depends(get_actor),
) -> dict:
    if settings.auth_mode in {"token", "accounts"} and actor.user_id is None:
        cookie_options = {
            "httponly": True,
            "secure": settings.cookie_secure,
            "samesite": "strict",
            "max_age": 8 * 60 * 60,
            "path": "/",
        }
        response.set_cookie(
            "mdp_session",
            create_session_identity(),
            **cookie_options,
        )
    return {"actor": actor.id, "role": actor.role, "display_name": actor.display_name, "user_id": actor.user_id}


@app.delete("/api/session")
def delete_session(response: Response, request: Request, mdp_session: str | None = Cookie(default=None)) -> dict:
    if mdp_session and mdp_session.startswith("account."):
        from .accounts import revoke_browser_session
        revoke_browser_session(mdp_session, ip_address=request.client.host if request.client else "")
    response.delete_cookie("mdp_session", path="/")
    return {"logged_out": True}


@app.get("/api/models")
def models(_: Actor = Depends(get_actor)) -> list[dict]:
    return list_models()


@app.post("/api/models", status_code=201)
def create_model_service(payload: ModelServiceUpsert, actor: Actor = Depends(get_actor)) -> dict:
    require_admin(actor)
    if get_model(payload.id):
        raise HTTPException(409, "模型服务 ID 已存在")
    now = now_iso()
    with db() as conn:
        conn.execute(
            """INSERT INTO model_services
               (id, name, family, provider_type, description, endpoint, model_name,
                api_key_env, capabilities_json, modes_json, config_json, timeout_seconds,
                enabled, builtin, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
            (
                payload.id, payload.name, payload.family, payload.provider_type,
                payload.description, payload.endpoint.rstrip("/"), payload.model_name,
                payload.api_key_env, json.dumps(payload.capabilities, ensure_ascii=False),
                json.dumps(payload.modes), json.dumps(payload.config, ensure_ascii=False),
                payload.timeout_seconds, int(payload.enabled), now, now,
            ),
        )
    audit(actor.id, "create", "model_service", payload.id, payload=payload.model_dump())
    return get_model(payload.id) or {}


@app.put("/api/models/{model_id}")
def update_model_service(
    model_id: str, payload: ModelServiceUpsert, actor: Actor = Depends(get_actor)
) -> dict:
    require_admin(actor)
    existing = get_model(model_id)
    if not existing:
        raise HTTPException(404, "模型服务不存在")
    if payload.id != model_id:
        raise HTTPException(400, "模型服务 ID 不可修改")
    with db() as conn:
        conn.execute(
            """UPDATE model_services SET name = ?, family = ?, provider_type = ?,
               description = ?, endpoint = ?, model_name = ?, api_key_env = ?,
               capabilities_json = ?, modes_json = ?, config_json = ?, timeout_seconds = ?,
               enabled = ?, updated_at = ? WHERE id = ?""",
            (
                payload.name, payload.family, payload.provider_type, payload.description,
                payload.endpoint.rstrip("/"), payload.model_name, payload.api_key_env,
                json.dumps(payload.capabilities, ensure_ascii=False), json.dumps(payload.modes),
                json.dumps(payload.config, ensure_ascii=False), payload.timeout_seconds,
                int(payload.enabled), now_iso(), model_id,
            ),
        )
    audit(actor.id, "update", "model_service", model_id, payload=payload.model_dump())
    return get_model(model_id) or {}


@app.delete("/api/models/{model_id}")
def delete_model_service(model_id: str, actor: Actor = Depends(get_actor)) -> dict:
    require_admin(actor)
    existing = get_model(model_id)
    if not existing:
        raise HTTPException(404, "模型服务不存在")
    if existing["builtin"]:
        raise HTTPException(409, "内置模型不能删除，可以将其停用")
    with db() as conn:
        conn.execute("DELETE FROM model_services WHERE id = ?", (model_id,))
    audit(actor.id, "delete", "model_service", model_id)
    return {"deleted": True}


class ModelEnabled(BaseModel):
    enabled: bool


@app.patch("/api/models/{model_id}/enabled")
def set_model_enabled(model_id: str, payload: ModelEnabled, actor: Actor = Depends(get_actor)) -> dict:
    require_admin(actor)
    if not get_model(model_id):
        raise HTTPException(404, "模型服务不存在")
    with db() as conn:
        conn.execute("UPDATE model_services SET enabled=?,updated_at=? WHERE id=?", (int(payload.enabled), now_iso(), model_id))
    audit(actor.id, "enable" if payload.enabled else "disable", "model_service", model_id)
    return get_model(model_id)


@app.post("/api/models/{model_id}/health")
async def check_model_health(model_id: str, actor: Actor = Depends(get_actor)) -> dict:
    model = get_model(model_id)
    if not model:
        raise HTTPException(404, "模型服务不存在")
    status, error = "unhealthy", ""
    try:
        if model["provider_type"] == "demo":
            status = "healthy"
        elif model["provider_type"] == "local_yolo":
            status = "healthy" if model["configured"] else "unhealthy"
            error = model["availability_error"]
        elif model["provider_type"] in {"local_grounding_dino", "local_sam2"}:
            available, error = local_runtime_status(model["model_name"])
            status = "healthy" if available else "unhealthy"
        elif model["provider_type"] == "local_sam3":
            available, error = local_sam3_runtime_status(
                model["model_name"], str(settings.sam3_source_dir)
            )
            status = "healthy" if available else "unhealthy"
        else:
            headers = {}
            if model["api_key_env"] and os.getenv(model["api_key_env"]):
                headers["Authorization"] = f"Bearer {os.getenv(model['api_key_env'])}"
            health_url = model["endpoint"]
            if model["provider_type"] == "openai_vision":
                if health_url.endswith("/chat/completions"):
                    health_url = health_url.removesuffix("/chat/completions") + "/models"
                elif health_url.endswith("/v1"):
                    health_url += "/models"
                else:
                    health_url += "/v1/models"
            async with httpx.AsyncClient(
                timeout=min(model["timeout_seconds"], 10), trust_env=False
            ) as client:
                response = await client.get(health_url, headers=headers)
            if response.status_code < 400 or response.status_code in {400, 405, 422}:
                status = "healthy"
            else:
                error = f"HTTP {response.status_code}"
    except Exception as exc:
        error = str(exc)[:500]
    with db() as conn:
        conn.execute(
            """UPDATE model_services SET last_health_status = ?, last_health_error = ?,
               last_health_at = ? WHERE id = ?""",
            (status, error, now_iso(), model_id),
        )
    audit(actor.id, "health_check", "model_service", model_id, payload={"status": status})
    return {"status": status, "error": error, "checked_at": now_iso()}


@app.get("/api/projects")
def projects(archived: bool = False, actor: Actor = Depends(get_actor)) -> list[dict]:
    if archived:
        require_admin(actor)
    rows = all_rows(
        """SELECT p.*,
                  COUNT(DISTINCT a.id) AS asset_count,
                  COUNT(DISTINCT CASE WHEN a.status = 'labeled' THEN a.id END) AS labeled_count,
                  COUNT(DISTINCT CASE WHEN a.review_status = 'submitted' THEN a.id END) AS submitted_count,
                  COUNT(DISTINCT CASE WHEN a.review_status = 'approved' THEN a.id END) AS approved_count,
                  COUNT(DISTINCT l.id) AS label_count
           FROM projects p
           LEFT JOIN assets a ON a.project_id = p.id
           LEFT JOIN labels l ON l.project_id = p.id
           WHERE (p.archived_at IS NOT NULL) = ?
           GROUP BY p.id ORDER BY p.updated_at DESC, p.id DESC""",
        (archived,),
    )
    # A bounded filmstrip per project; fetch all previews in one query.
    previews = all_rows(
        """SELECT id, project_id FROM (
             SELECT a.id, a.project_id,
                    ROW_NUMBER() OVER (PARTITION BY a.project_id ORDER BY a.id DESC) AS position
             FROM assets a JOIN projects p ON p.id = a.project_id
             WHERE (p.archived_at IS NOT NULL) = ?
           ) WHERE position <= 3 ORDER BY project_id, position""", (archived,)
    )
    by_project: dict[int, list[int]] = {}
    for preview in previews:
        by_project.setdefault(preview["project_id"], []).append(preview["id"])
    for row in rows:
        row["preview_asset_ids"] = by_project.get(row["id"], [])
    return rows


@app.patch("/api/projects/{project_id}/archive")
def archive_project(project_id: int, payload: ProjectArchiveUpdate, actor: Actor = Depends(get_actor)) -> dict:
    require_admin(actor)
    # Archiving organizes the project list; it never deletes data or stops work.
    with db() as conn:
        changed = conn.execute(
            "UPDATE projects SET archived_at = ? WHERE id = ?",
            (now_iso() if payload.archived else None, project_id),
        )
        if not changed.rowcount:
            raise HTTPException(404, "项目不存在")
    audit(actor.id, "archive" if payload.archived else "restore", "project", project_id, project_id)
    return require_project(project_id)


@app.post("/api/projects", status_code=201)
def create_project(payload: ProjectCreate, actor: Actor = Depends(get_actor)) -> dict:
    require_admin(actor)
    now = now_iso()
    label_names = payload.labels or ["object"]
    palette = ["#22c55e", "#38bdf8", "#f97316", "#a78bfa", "#f43f5e", "#facc15"]
    with db() as conn:
        cursor = conn.execute(
            "INSERT INTO projects (name, description, task_type, keypoint_schema_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (payload.name, payload.description, payload.task_type,
             json.dumps(payload.keypoint_schema.model_dump() if payload.keypoint_schema else {}), now, now),
        )
        project_id = cursor.lastrowid
        for index, label_name in enumerate(label_names):
            conn.execute(
                """INSERT INTO labels
                   (project_id, name, color, shortcut, attributes_schema_json, created_at)
                   VALUES (?, ?, ?, ?, '{}', ?)""",
                (
                    project_id, label_name, palette[index % len(palette)],
                    str(index + 1) if index < 9 else "", now,
                ),
            )
    audit(actor.id, "create", "project", int(project_id), int(project_id), payload.model_dump())
    return require_project(int(project_id))


@app.get("/api/projects/{project_id}")
def project_detail(project_id: int, _: Actor = Depends(get_actor)) -> dict:
    project = require_project(project_id)
    project["labels"] = all_rows(
        "SELECT * FROM labels WHERE project_id = ? ORDER BY id", (project_id,)
    )
    for label in project["labels"]:
        label["attributes_schema"] = json.loads(label.pop("attributes_schema_json", "{}"))
    stats = one(
        """SELECT COUNT(*) AS asset_count,
                  COALESCE(SUM(CASE WHEN status = 'labeled' THEN 1 ELSE 0 END), 0) AS labeled_count,
                  COALESCE(SUM(CASE WHEN review_status = 'submitted' THEN 1 ELSE 0 END), 0) AS submitted_count,
                  COALESCE(SUM(CASE WHEN review_status = 'approved' THEN 1 ELSE 0 END), 0) AS approved_count
           FROM assets WHERE project_id = ?""",
        (project_id,),
    )
    project.update(stats or {"asset_count": 0, "labeled_count": 0})
    return project


@app.post("/api/projects/{project_id}/labels", status_code=201)
def create_label(project_id: int, payload: LabelCreate, actor: Actor = Depends(get_actor)) -> dict:
    require_admin(actor)
    require_project(project_id)
    try:
        with db() as conn:
            cursor = conn.execute(
                """INSERT INTO labels
                   (project_id, name, color, shortcut, attributes_schema_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    project_id, payload.name.strip(), payload.color, payload.shortcut,
                    json.dumps(payload.attributes_schema, ensure_ascii=False), now_iso(),
                ),
            )
            label_id = cursor.lastrowid
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(409, "标签名称已存在") from exc
        raise
    audit(actor.id, "create", "label", int(label_id), project_id, payload.model_dump())
    result = one("SELECT * FROM labels WHERE id = ?", (label_id,)) or {}
    result["attributes_schema"] = json.loads(result.pop("attributes_schema_json", "{}"))
    return result


@app.delete("/api/labels/{label_id}")
def delete_label(label_id: int, actor: Actor = Depends(get_actor)) -> dict:
    require_admin(actor)
    label = one("SELECT * FROM labels WHERE id = ?", (label_id,))
    if not label:
        raise HTTPException(404, "标签不存在")
    count = one("SELECT COUNT(*) AS count FROM labels WHERE project_id = ?", (label["project_id"],))
    if count and count["count"] <= 1:
        raise HTTPException(409, "项目至少需要保留一个标签")
    usage = one("SELECT COUNT(*) AS count FROM annotations WHERE label_id = ?", (label_id,))
    if usage and usage["count"]:
        raise HTTPException(409, "标签已有标注历史，不能删除；请保留它以保证审计和导出可追溯")
    with db() as conn:
        conn.execute("DELETE FROM labels WHERE id = ?", (label_id,))
    audit(actor.id, "delete", "label", label_id, label["project_id"])
    return {"deleted": True}


@app.post("/api/projects/{project_id}/assets", status_code=201)
async def upload_assets(
    project_id: int,
    files: list[UploadFile] = File(...),
    actor: Actor = Depends(get_actor),
) -> dict:
    require_project(project_id)
    stored = []
    created, duplicate_count = [], 0
    try:
        for upload in files:
            stored.extend(await unpack_upload(upload))
            if len(stored) > settings.max_assets_per_upload:
                raise HTTPException(
                    413, f"单次上传图片数量不能超过 {settings.max_assets_per_upload}"
                )
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for image in stored:
                duplicate = conn.execute(
                    "SELECT id FROM assets WHERE project_id = ? AND sha256 = ?",
                    (project_id, image.sha256),
                ).fetchone()
                if duplicate:
                    (settings.media_dir / image.storage_name).unlink(missing_ok=True)
                    duplicate_count += 1
                    continue
                cursor = conn.execute(
                    """INSERT INTO assets
                       (project_id, filename, storage_name, mime_type, width, height, size_bytes,
                        sha256, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        project_id,
                        image.filename,
                        image.storage_name,
                        image.mime_type,
                        image.width,
                        image.height,
                        image.size_bytes,
                        image.sha256,
                        now_iso(), now_iso(),
                    ),
                )
                created.append(cursor.lastrowid)
            conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now_iso(), project_id))
    except Exception:
        for image in stored:
            if not one("SELECT id FROM assets WHERE storage_name = ?", (image.storage_name,)):
                (settings.media_dir / image.storage_name).unlink(missing_ok=True)
        raise
    audit(
        actor.id, "upload", "project", project_id, project_id,
        {"created": len(created), "duplicates": duplicate_count},
    )
    return {"count": len(created), "duplicate_count": duplicate_count, "asset_ids": created}


@app.get("/api/projects/{project_id}/assets")
def assets(
    project_id: int, status: str | None = None, review_status: str | None = None,
    _: Actor = Depends(get_actor),
) -> list[dict]:
    require_project(project_id)
    query = """SELECT a.*, COUNT(n.id) AS annotation_count
               FROM assets a LEFT JOIN annotations n
                 ON n.asset_id = a.id AND n.deleted_at IS NULL
               WHERE a.project_id = ?"""
    params: tuple = (project_id,)
    if status:
        query += " AND a.status = ?"
        params += (status,)
    if review_status:
        query += " AND a.review_status = ?"
        params += (review_status,)
    query += " GROUP BY a.id ORDER BY a.id"
    return all_rows(query, params)


@app.get("/api/assets/{asset_id}")
def asset_detail(asset_id: int, _: Actor = Depends(get_actor)) -> dict:
    asset = require_asset(asset_id)
    count = one(
        "SELECT COUNT(*) AS count FROM annotations WHERE asset_id = ? AND deleted_at IS NULL",
        (asset_id,),
    )
    asset["annotation_count"] = count["count"] if count else 0
    return asset


@app.get("/api/assets/{asset_id}/image")
def asset_image(asset_id: int, _: Actor = Depends(get_actor)) -> FileResponse:
    asset = require_asset(asset_id)
    path = settings.media_dir / asset["storage_name"]
    if not path.exists():
        raise HTTPException(404, "图片文件不存在")
    return FileResponse(path, media_type=asset["mime_type"], filename=asset["filename"])


@app.get("/api/assets/{asset_id}/thumbnail")
def asset_thumbnail(
    asset_id: int,
    size: int = Query(default=320, ge=64, le=1024),
    _: Actor = Depends(get_actor),
) -> FileResponse:
    asset = require_asset(asset_id)
    media_root = settings.media_dir.resolve()
    source = (settings.media_dir / asset["storage_name"]).resolve()
    if media_root not in source.parents or not source.is_file():
        raise HTTPException(404, "图片文件不存在")
    cache_path = settings.thumbnail_dir / f"{asset['sha256']}-{size}.webp"
    if not cache_path.is_file():
        temporary = settings.thumbnail_dir / f".{cache_path.name}.{uuid.uuid4().hex}.tmp"
        try:
            with Image.open(source) as opened:
                image = ImageOps.exif_transpose(opened)
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGB")
                image.thumbnail((size, size), Image.Resampling.LANCZOS, reducing_gap=3.0)
                image.save(temporary, "WEBP", quality=82, method=4)
            os.replace(temporary, cache_path)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            raise HTTPException(415, "无法生成图片缩略图") from exc
    return FileResponse(
        cache_path,
        media_type="image/webp",
        filename=f"{Path(asset['filename']).stem}-{size}.webp",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@app.post("/api/assets/{asset_id}/lock")
def acquire_asset_lock(asset_id: int, actor: Actor = Depends(get_actor)) -> dict:
    asset = require_asset(asset_id)
    from datetime import UTC, datetime, timedelta

    expires_at = (datetime.now(UTC) + timedelta(seconds=settings.asset_lock_seconds)).isoformat()
    now = now_iso()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        active_job = active_job_for_assets(conn, asset["project_id"], [asset_id])
        if active_job:
            raise HTTPException(409, f"图片正在由 AI 任务 #{active_job} 处理，暂时只读")
        row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "图片已被删除，请刷新图片列表")
        asset = dict(row)
        if asset["review_status"] in {"submitted", "approved"} and actor.role == "annotator":
            raise HTTPException(409, "图片已提交审核，标注员当前只读；需撤回或由审核员重新打开")
        cursor = conn.execute(
            """UPDATE assets SET lock_owner = ?, lock_expires_at = ?
               WHERE id = ? AND (lock_owner IS NULL OR lock_owner = ? OR lock_expires_at <= ?)""",
            (actor.id, expires_at, asset_id, actor.id, now),
        )
        if cursor.rowcount != 1:
            current = conn.execute(
                "SELECT lock_owner, lock_expires_at FROM assets WHERE id = ?", (asset_id,)
            ).fetchone()
            raise HTTPException(
                423,
                {"message": "图片已被其他用户锁定", "owner": current[0], "expires_at": current[1]},
            )
    return {"owner": actor.id, "expires_at": expires_at, "revision": asset["revision"]}


@app.delete("/api/assets/{asset_id}/lock")
def release_asset_lock(asset_id: int, actor: Actor = Depends(get_actor)) -> dict:
    require_asset(asset_id)
    with db() as conn:
        conn.execute(
            "UPDATE assets SET lock_owner = NULL, lock_expires_at = NULL WHERE id = ? AND lock_owner = ?",
            (asset_id, actor.id),
        )
    return {"released": True}


@app.post("/api/assets/{asset_id}/workflow")
def update_asset_workflow(
    asset_id: int, payload: AssetWorkflowUpdate, actor: Actor = Depends(get_actor)
) -> dict:
    asset = require_asset(asset_id)
    if payload.expected_revision != asset["revision"]:
        raise HTTPException(409, "图片已被其他用户修改，请刷新后重试")
    transitions = {
        "start": ({"draft", "rejected"}, "in_progress"),
        "submit": ({"draft", "in_progress", "rejected"}, "submitted"),
        "approve": ({"submitted"}, "approved"),
        "reject": ({"submitted"}, "rejected"),
        "reopen": ({"submitted", "approved", "rejected"}, "draft"),
    }
    allowed_from, target = transitions[payload.action]
    if asset["review_status"] not in allowed_from:
        raise HTTPException(409, f"不能从 {asset['review_status']} 执行 {payload.action}")
    if payload.action in {"approve", "reject"} or (payload.action == "reopen" and asset["review_status"] == "approved"):
        require_reviewer(actor)
    else:
        ensure_asset_write_access(asset, actor)
    if payload.action == "reject" and not payload.reason.strip():
        raise HTTPException(422, "驳回时必须填写原因")
    if payload.action == "submit":
        count = one(
            "SELECT COUNT(*) AS count FROM annotations WHERE asset_id = ? AND deleted_at IS NULL",
            (asset_id,),
        )
        if not count or not count["count"]:
            raise HTTPException(409, "没有有效标注，不能提交审核")
    now = now_iso()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        guard_asset(conn, asset_id, actor, editable=False)
        cursor = conn.execute(
            """UPDATE assets SET review_status = ?, review_reason = ?, revision = revision + 1,
               updated_at = ? WHERE id = ? AND revision = ?""",
            (target, payload.reason, now, asset_id, payload.expected_revision),
        )
        if cursor.rowcount != 1:
            raise HTTPException(409, "图片已被其他用户修改，请刷新后重试")
        if payload.action == "submit":
            conn.execute("UPDATE assets SET lock_owner=NULL, lock_expires_at=NULL WHERE id=?", (asset_id,))
        conn.execute(
            """INSERT INTO review_events
               (asset_id, from_status, to_status, actor, reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (asset_id, asset["review_status"], target, actor.id, payload.reason, now),
        )
    audit(
        actor.id, payload.action, "asset_review", asset_id, asset["project_id"],
        {"from": asset["review_status"], "to": target, "reason": payload.reason},
    )
    return {**require_asset(asset_id), "previous_status": asset["review_status"]}


@app.get("/api/assets/{asset_id}/review-events")
def asset_review_events(asset_id: int, _: Actor = Depends(get_actor)) -> list[dict]:
    require_asset(asset_id)
    return all_rows("SELECT * FROM review_events WHERE asset_id = ? ORDER BY id DESC", (asset_id,))


@app.get("/api/assets/{asset_id}/annotations")
def annotations(asset_id: int, _: Actor = Depends(get_actor)) -> list[dict]:
    require_asset(asset_id)
    rows = all_rows(
        """SELECT n.*, l.name AS label_name, l.color AS label_color
           FROM annotations n JOIN labels l ON l.id = n.label_id
           WHERE n.asset_id = ? AND n.deleted_at IS NULL ORDER BY n.id""",
        (asset_id,),
    )
    return [serialize_annotation(row) for row in rows]


@app.post("/api/assets/{asset_id}/annotations", status_code=201)
def create_annotation(
    asset_id: int, payload: AnnotationCreate, actor: Actor = Depends(get_actor)
) -> dict:
    asset = require_asset(asset_id)
    ensure_asset_annotation_editable(asset, actor)
    if payload.expected_asset_revision is not None and payload.expected_asset_revision != asset["revision"]:
        raise HTTPException(409, "图片标注已被其他用户修改，请刷新后重试")
    label = one(
        "SELECT * FROM labels WHERE id = ? AND project_id = ?",
        (payload.label_id, asset["project_id"]),
    )
    if not label:
        raise HTTPException(400, "标签不属于当前项目")
    if payload.source != "manual" or payload.model_id is not None:
        raise HTTPException(400, "人工标注接口不能伪造 AI 来源；AI 标注必须通过任务系统创建")
    keypoints = [point.model_dump() for point in payload.keypoints]
    validate_task_annotation(require_project(asset["project_id"]), payload.shape_type, payload.points, keypoints, asset["width"], asset["height"])
    _validate_shape(payload.shape_type, payload.points, asset["width"], asset["height"])
    _validate_attributes(label, payload.attributes)
    now = now_iso()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        guard_asset(conn, asset_id, actor)
        if payload.shape_type == "classification" and conn.execute(
            "SELECT 1 FROM annotations WHERE asset_id=? AND deleted_at IS NULL", (asset_id,)
        ).fetchone():
            raise HTTPException(409, "图片已有分类，请修改已有分类标签")
        cursor = conn.execute(
            """INSERT INTO annotations
               (asset_id, label_id, shape_type, points_json, attributes_json, keypoints_json,
                confidence, source, model_id, version, created_by, updated_by,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
            (
                asset_id,
                payload.label_id,
                payload.shape_type,
                json.dumps(payload.points),
                json.dumps(payload.attributes, ensure_ascii=False),
                json.dumps(keypoints),
                None,
                "manual",
                None,
                actor.id, actor.id, now, now,
            ),
        )
        revision_cursor = conn.execute(
            """UPDATE assets SET status = 'labeled', review_status = 'draft',
               revision = revision + 1, updated_at = ? WHERE id = ? AND revision = ?""",
            (now, asset_id, asset["revision"]),
        )
        if revision_cursor.rowcount != 1:
            raise HTTPException(409, "图片标注已被其他用户修改，请刷新后重试")
        annotation_id = cursor.lastrowid
    row = one(
        """SELECT n.*, l.name AS label_name, l.color AS label_color
           FROM annotations n JOIN labels l ON l.id = n.label_id
           WHERE n.id = ? AND n.deleted_at IS NULL""",
        (annotation_id,),
    )
    audit(actor.id, "create", "annotation", int(annotation_id), asset["project_id"])
    result = serialize_annotation(row or {})
    result["asset_revision"] = asset["revision"] + 1
    return result


@app.patch("/api/annotations/{annotation_id}")
def update_annotation(
    annotation_id: int, payload: AnnotationUpdate, actor: Actor = Depends(get_actor)
) -> dict:
    item = one(
        """SELECT n.*, a.project_id, a.width, a.height, a.review_status,
                  a.lock_owner, a.lock_expires_at, a.revision AS asset_revision
           FROM annotations n JOIN assets a ON a.id = n.asset_id
           WHERE n.id = ? AND n.deleted_at IS NULL""",
        (annotation_id,),
    )
    if not item:
        raise HTTPException(404, "标注不存在")
    ensure_asset_annotation_editable(item, actor)
    if item["version"] != payload.expected_version:
        raise HTTPException(409, "对象已被其他用户修改，请刷新后重试")
    label_id = payload.label_id if payload.label_id is not None else item["label_id"]
    label = one(
        "SELECT * FROM labels WHERE id = ? AND project_id = ?", (label_id, item["project_id"])
    )
    if not label:
        raise HTTPException(400, "标签不属于当前项目")
    shape_type = payload.shape_type or item["shape_type"]
    points = payload.points if payload.points is not None else json.loads(item["points_json"])
    keypoints = ([point.model_dump() for point in payload.keypoints] if payload.keypoints is not None else json.loads(item.get("keypoints_json") or "[]"))
    validate_task_annotation(require_project(item["project_id"]), shape_type, points, keypoints, item["width"], item["height"])
    _validate_shape(shape_type, points, item["width"], item["height"])
    attributes = payload.attributes if payload.attributes is not None else json.loads(item["attributes_json"])
    _validate_attributes(label, attributes)
    now = now_iso()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current_asset = guard_asset(conn, item["asset_id"], actor)
        if current_asset["revision"] != item["asset_revision"]:
            raise HTTPException(409, "图片已被其他协作者修改或审核，请刷新")
        cursor = conn.execute(
            """UPDATE annotations SET label_id = ?, shape_type = ?, points_json = ?,
               attributes_json = ?, keypoints_json = ?, human_modified = 1, version = version + 1, updated_by = ?, updated_at = ?
               WHERE id = ? AND version = ? AND deleted_at IS NULL""",
            (
                label_id, shape_type, json.dumps(points),
                json.dumps(attributes, ensure_ascii=False), json.dumps(keypoints), actor.id, now,
                annotation_id, payload.expected_version,
            ),
        )
        if cursor.rowcount != 1:
            raise HTTPException(409, "对象已被其他用户修改，请刷新后重试")
        conn.execute(
            """UPDATE assets SET review_status = 'draft', revision = revision + 1,
               updated_at = ? WHERE id = ?""",
            (now, item["asset_id"]),
        )
    audit(actor.id, "update", "annotation", annotation_id, item["project_id"])
    row = one(
        """SELECT n.*, l.name AS label_name, l.color AS label_color
           FROM annotations n JOIN labels l ON l.id = n.label_id WHERE n.id = ?""",
        (annotation_id,),
    )
    result = serialize_annotation(row or {})
    result["asset_revision"] = require_asset(item["asset_id"])["revision"]
    return result


@app.delete("/api/annotations/{annotation_id}")
def delete_annotation(
    annotation_id: int,
    expected_version: int = Query(ge=1),
    actor: Actor = Depends(get_actor),
) -> dict:
    item = one(
        """SELECT n.*, a.project_id, a.review_status, a.lock_owner, a.lock_expires_at, a.revision AS asset_revision
           FROM annotations n JOIN assets a ON a.id = n.asset_id
           WHERE n.id = ? AND n.deleted_at IS NULL""",
        (annotation_id,),
    )
    if not item:
        raise HTTPException(404, "标注不存在")
    ensure_asset_annotation_editable(item, actor)
    if item["version"] != expected_version:
        raise HTTPException(409, "对象已被其他用户修改，请刷新后重试")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current_asset = guard_asset(conn, item["asset_id"], actor)
        if current_asset["revision"] != item["asset_revision"]:
            raise HTTPException(409, "图片已被其他协作者修改或审核，请刷新")
        now = now_iso()
        cursor = conn.execute(
            """UPDATE annotations SET deleted_at = ?, updated_at = ?, updated_by = ?,
               version = version + 1 WHERE id = ? AND version = ? AND deleted_at IS NULL""",
            (now, now, actor.id, annotation_id, expected_version),
        )
        if cursor.rowcount != 1:
            raise HTTPException(409, "对象已被其他用户修改，请刷新后重试")
        remaining = conn.execute(
            "SELECT COUNT(*) FROM annotations WHERE asset_id = ? AND deleted_at IS NULL",
            (item["asset_id"],),
        ).fetchone()[0]
        conn.execute(
            """UPDATE assets SET status = ?, review_status = 'draft', revision = revision + 1,
               updated_at = ? WHERE id = ?""",
            ("labeled" if remaining else "unlabeled", now, item["asset_id"]),
        )
    audit(actor.id, "delete", "annotation", annotation_id, item["project_id"])
    return {"deleted": True, "asset_revision": require_asset(item["asset_id"])["revision"]}


def _prepare_annotation_request(project_id: int, payload: JobCreate, actor: Actor) -> tuple:
    """Validate preview and queue requests using the same model and class contract."""
    if actor.must_change_password:
        raise password_change_required()
    if actor.role not in {"admin", "annotator", "reviewer"}:
        raise HTTPException(403, "需要标注员或审核员权限")
    project = require_project(project_id)
    if payload.include_reviewed:
        require_reviewer(actor)
    model = get_model(payload.model_id)
    provider = get_provider(payload.model_id)
    if not model or not provider:
        raise HTTPException(404, "标注模型不存在")
    if not provider.info.configured:
        raise HTTPException(409, f"{provider.info.name} 尚未配置推理端点")
    supported_tasks = model.get("tasks", ["detect", "segment"])
    if project["task_type"] != "mixed" and project["task_type"] not in supported_tasks:
        raise HTTPException(409, "该 AI 服务不支持项目任务类型，请选择匹配的模型")
    if payload.mode not in provider.info.modes:
        raise HTTPException(400, "该模型不支持所选标注模式")
    if model["provider_type"] in {"interactive_segmentation", "local_sam2"} and not (
        payload.hint_annotation_id or payload.hints
    ):
        raise HTTPException(400, "交互式分割需要正负点、粗框或粗多边形提示")
    if payload.mode == "assist" and (not payload.asset_ids or len(payload.asset_ids) != 1):
        raise HTTPException(400, "半自动标注一次只能处理一张图片")
    if payload.asset_ids:
        placeholders = ",".join("?" for _ in payload.asset_ids)
        count = one(
            f"SELECT COUNT(*) AS count FROM assets WHERE project_id = ? AND id IN ({placeholders})",
            (project_id, *payload.asset_ids),
        )
        if not count or count["count"] != len(set(payload.asset_ids)):
            raise HTTPException(400, "任务包含不属于当前项目的图片")
        if not payload.include_reviewed:
            placeholders = ",".join("?" for _ in payload.asset_ids)
            reviewed = one(
                f"""SELECT COUNT(*) AS count FROM assets WHERE project_id = ?
                    AND id IN ({placeholders}) AND review_status IN ('submitted', 'approved')""",
                (project_id, *payload.asset_ids),
            )
            if reviewed and reviewed["count"]:
                raise HTTPException(409, "任务包含已提交或审核通过的图片；请先重新打开")
    if payload.hint_annotation_id:
        hint = one(
            """SELECT n.id, n.asset_id FROM annotations n JOIN assets a ON a.id = n.asset_id
               WHERE n.id = ? AND a.project_id = ? AND n.deleted_at IS NULL""",
            (payload.hint_annotation_id, project_id),
        )
        if not hint:
            raise HTTPException(400, "提示标注不属于当前项目")
        if payload.asset_ids and hint["asset_id"] not in payload.asset_ids:
            raise HTTPException(400, "提示标注不属于任务所选图片")
    if payload.hints:
        if not payload.asset_ids or len(payload.asset_ids) != 1:
            raise HTTPException(400, "交互提示只能用于单张图片")
        hint_asset = require_asset(payload.asset_ids[0])
        _validate_job_hints(payload.hints, hint_asset["width"], hint_asset["height"])
    project_labels = all_rows(
        "SELECT * FROM labels WHERE project_id = ? ORDER BY id", (project_id,)
    )
    if not project_labels:
        raise HTTPException(409, "请先创建标签")
    project_label_names = [label["name"] for label in project_labels]
    concept_provider = model["provider_type"] in {"local_grounding_dino", "local_sam3"}
    if concept_provider:
        target_label_names = payload.target_labels or grounding_labels_from_prompt(
            payload.prompt, project_label_names
        )
    else:
        target_label_names = payload.target_labels or project_label_names
        known_labels = {name.casefold() for name in project_label_names}
        unknown = [name for name in target_label_names if name.casefold() not in known_labels]
        if unknown:
            raise HTTPException(400, f"目标类别不属于当前项目：{', '.join(unknown)}")
    known = {label["name"].casefold(): label for label in project_labels}
    pending_labels = [name for name in target_label_names if name.casefold() not in known]
    if pending_labels:
        require_admin(actor)
    target_label_names = [known[name.casefold()]["name"] if name.casefold() in known else name
                          for name in target_label_names]
    config = {
        "asset_ids": payload.asset_ids,
        "hint_annotation_id": payload.hint_annotation_id,
        "hints": payload.hints,
        "replace_existing_auto": payload.replace_existing_auto,
        "confidence_threshold": payload.confidence_threshold,
        "include_reviewed": payload.include_reviewed,
        "target_labels": target_label_names,
        "prompt_kind": "grounding_labels" if concept_provider else "instruction",
    }
    return project, model, provider, project_labels, config, pending_labels


@app.post("/api/projects/{project_id}/annotation-preview")
async def annotation_preview(project_id: int, payload: AnnotationPreviewCreate,
                             actor: Actor = Depends(get_actor)) -> dict:
    project, model, provider, labels, config, pending = await asyncio.to_thread(
        _prepare_annotation_request, project_id, payload, actor
    )
    return await run_preview(project, model, provider, labels, config, payload.prompt, pending)


@app.post("/api/projects/{project_id}/jobs", status_code=202)
def create_job(project_id: int, payload: JobCreate, actor: Actor = Depends(get_actor)) -> dict:
    project, model, provider, project_labels, config, _pending = _prepare_annotation_request(project_id, payload, actor)
    target_label_names = config["target_labels"]
    concept_provider = config["prompt_kind"] == "grounding_labels"
    if payload.asset_ids:
        job_total = len(set(payload.asset_ids))
    else:
        eligibility_query = "SELECT COUNT(*) AS count FROM assets WHERE project_id = ?"
        if not payload.include_reviewed:
            eligibility_query += " AND review_status NOT IN ('submitted', 'approved')"
        eligible = one(eligibility_query, (project_id,))
        job_total = int(eligible["count"]) if eligible else 0
    if not job_total:
        raise HTTPException(409, "没有可处理的草稿图片")
    settings_json = json.dumps(config, ensure_ascii=False)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        selected = set(payload.asset_ids) if payload.asset_ids is not None else None
        eligible_assets = [dict(row) for row in conn.execute("SELECT * FROM assets WHERE project_id=? ORDER BY id", (project_id,))
                           if (selected is None or row["id"] in selected)]
        if selected is not None and len(eligible_assets) != len(selected):
            raise HTTPException(400, "任务包含不属于当前项目的图片")
        if not payload.include_reviewed:
            if selected is not None and any(row["review_status"] in {"submitted", "approved"} for row in eligible_assets):
                raise HTTPException(409, "请先重新打开已提交或审核通过的图片")
            eligible_assets = [row for row in eligible_assets if row["review_status"] not in {"submitted", "approved"}]
        frozen_ids = [row["id"] for row in eligible_assets]
        guard_job_targets(conn, project_id, frozen_ids)
        job_total = len(frozen_ids)
        settings_json = json.dumps({**json.loads(settings_json), "asset_ids": frozen_ids,
                                    "task_type": project["task_type"], "keypoint_schema": project["keypoint_schema"]}, ensure_ascii=False)
        auto_added_labels: list[str] = []
        if concept_provider:
            existing = {
                row["name"].casefold(): row["name"]
                for row in conn.execute(
                    "SELECT name FROM labels WHERE project_id = ?", (project_id,)
                ).fetchall()
            }
            palette = ["#22c55e", "#38bdf8", "#f97316", "#a78bfa", "#f43f5e", "#facc15"]
            label_count = len(existing)
            canonical_targets: list[str] = []
            for target in target_label_names:
                canonical = existing.get(target.casefold())
                if canonical is None:
                    require_admin(actor)
                    conn.execute(
                        """INSERT INTO labels
                           (project_id, name, color, shortcut, attributes_schema_json, created_at)
                           VALUES (?, ?, ?, '', '{}', ?)""",
                        (project_id, target, palette[label_count % len(palette)], now_iso()),
                    )
                    existing[target.casefold()] = target
                    canonical = target
                    auto_added_labels.append(target)
                    label_count += 1
                canonical_targets.append(canonical)
            target_label_names = canonical_targets
            settings_json = json.dumps(
                {**json.loads(settings_json), "target_labels": target_label_names},
                ensure_ascii=False,
            )
            if auto_added_labels:
                conn.execute(
                    "UPDATE projects SET updated_at = ? WHERE id = ?", (now_iso(), project_id)
                )
        cursor = conn.execute(
            """INSERT INTO jobs
               (project_id, model_id, mode, prompt, status, total, settings_json, max_retries,
                progress_message, created_at)
               VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, '已排队，等待标注 Worker', ?)""",
            (
                project_id, payload.model_id, payload.mode, payload.prompt,
                job_total, settings_json, payload.max_retries, now_iso(),
            ),
        )
        job_id = int(cursor.lastrowid)
    audit(
        actor.id, "enqueue", "annotation_job", job_id, project_id,
        {
            "model_id": payload.model_id,
            "mode": payload.mode,
            "asset_ids": payload.asset_ids,
            "target_labels": target_label_names,
            "auto_added_labels": auto_added_labels,
        },
    )
    return serialize_job(one("SELECT * FROM jobs WHERE id = ?", (job_id,)) or {})


@app.get("/api/projects/{project_id}/jobs")
def jobs(project_id: int, _: Actor = Depends(get_actor)) -> list[dict]:
    require_project(project_id)
    return [
        serialize_job(row)
        for row in all_rows("SELECT * FROM jobs WHERE project_id = ? ORDER BY id DESC", (project_id,))
    ]


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: int, _: Actor = Depends(get_actor)) -> dict:
    row = one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not row:
        raise HTTPException(404, "任务不存在")
    return serialize_job(row)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int, actor: Actor = Depends(get_actor)) -> dict:
    row = one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not row:
        raise HTTPException(404, "任务不存在")
    if row["status"] in {"completed", "failed", "cancelled"}:
        raise HTTPException(409, "任务已经结束")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        now = now_iso()
        cursor = conn.execute(
            """UPDATE jobs SET cancel_requested = 1,
               status = CASE WHEN status = 'queued' OR lease_expires_at IS NULL OR lease_expires_at <= ? THEN 'cancelled' ELSE status END,
               finished_at = CASE WHEN status = 'queued' OR lease_expires_at IS NULL OR lease_expires_at <= ? THEN ? ELSE finished_at END,
               progress_message = '正在取消' WHERE id = ? AND status IN ('queued','running')""",
            (now, now, now, job_id),
        )
        if cursor.rowcount != 1:
            raise HTTPException(409, "任务已经结束")
    audit(actor.id, "cancel", "annotation_job", job_id, row["project_id"])
    return serialize_job(one("SELECT * FROM jobs WHERE id = ?", (job_id,)) or {})


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: int, actor: Actor = Depends(get_actor)) -> dict:
    row = one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not row:
        raise HTTPException(404, "任务不存在")
    if row["status"] not in {"failed", "cancelled"}:
        raise HTTPException(409, "只有失败或取消的任务可以重试")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        if row["status"] not in {"failed", "cancelled"}:
            raise HTTPException(409, "任务已被重试或仍在运行")
        config = json.loads(row["settings_json"])
        if config.get("include_reviewed"):
            require_reviewer(actor)
        requested = config.get("asset_ids")
        targets = [dict(asset) for asset in conn.execute("SELECT * FROM assets WHERE project_id=?", (row["project_id"],))
                   if requested is None or asset["id"] in requested]
        if not config.get("include_reviewed"):
            targets = [asset for asset in targets if asset["review_status"] not in {"submitted", "approved"}]
        target_ids = [asset["id"] for asset in targets]
        guard_job_targets(conn, row["project_id"], target_ids, exclude=job_id)
        config["asset_ids"] = target_ids
        conn.execute("DELETE FROM job_asset_results WHERE job_id=?", (job_id,))
        conn.execute(
            """UPDATE jobs SET status = 'queued', cancel_requested = 0, attempt = 0,
               error = NULL, claimed_by = NULL, lease_expires_at = NULL, heartbeat_at = NULL,
               processed = 0, created_count = 0, started_at = NULL, finished_at = NULL,
               review_generation = review_generation + 1,
               settings_json = ?, total = ?, progress_message = '等待工作进程' WHERE id = ?""",
            (json.dumps(config, ensure_ascii=False), len(target_ids), job_id),
        )
    audit(actor.id, "retry", "annotation_job", job_id, row["project_id"])
    return serialize_job(one("SELECT * FROM jobs WHERE id = ?", (job_id,)) or {})


@app.get("/api/projects/{project_id}/audit-logs")
def project_audit_logs(
    project_id: int,
    before_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
    actor: Actor = Depends(get_actor),
) -> list[dict]:
    require_project(project_id)
    query = "SELECT * FROM audit_logs WHERE project_id = ?"
    params: tuple = (project_id,)
    if before_id is not None:
        query += " AND id < ?"
        params += (before_id,)
    query += " ORDER BY id DESC LIMIT ?"
    params += (limit,)
    rows = all_rows(query, params)
    for row in rows:
        row["payload"] = json.loads(row.pop("payload_json", "{}"))
    return rows


@app.get("/api/projects/{project_id}/export/{export_format}")
def export_dataset(
    project_id: int, export_format: str, background_tasks: BackgroundTasks, approved_only: bool = False,
    _: Actor = Depends(get_actor),
) -> FileResponse:
    project = require_project(project_id)
    if export_format not in {"coco", "yolo", "json"}:
        raise HTTPException(400, "仅支持 coco、yolo 或 json")
    if project["task_type"] in {"classify", "pose"} or export_format == "json":
        import tempfile
        with tempfile.NamedTemporaryFile(prefix="mdp-export-", suffix=".zip", delete=False) as handle:
            path = Path(handle.name)
        try:
            build_project_export(project_id, export_format, path, approved_only)
        except Exception as exc:
            path.unlink(missing_ok=True)
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(409, str(exc)) from exc
        background_tasks.add_task(path.unlink, missing_ok=True)
        return FileResponse(path, media_type="application/zip", filename=f"project-{project_id}-{export_format}.zip")
    # Keep labels, images and annotations from one SQLite snapshot even when
    # another collaborator deletes an image while the archive is being built.
    with db() as conn:
        conn.execute("BEGIN")
        labels = [dict(row) for row in conn.execute(
            "SELECT * FROM labels WHERE project_id = ? ORDER BY id", (project_id,)
        ).fetchall()]
        asset_query = "SELECT * FROM assets WHERE project_id = ?"
        if approved_only:
            asset_query += " AND review_status = 'approved'"
        assets_rows = [dict(row) for row in conn.execute(
            asset_query + " ORDER BY id", (project_id,)
        ).fetchall()]
        asset_ids = [asset["id"] for asset in assets_rows]
        if not asset_ids:
            raise HTTPException(409, "没有符合导出条件的图片")
        placeholders = ",".join("?" for _ in asset_ids)
        annotations_rows = [dict(row) for row in conn.execute(
            f"""SELECT n.*, l.name AS label_name FROM annotations n
               JOIN labels l ON l.id = n.label_id JOIN assets a ON a.id = n.asset_id
               WHERE n.deleted_at IS NULL AND a.id IN ({placeholders}) ORDER BY n.id""",
            tuple(asset_ids),
        ).fetchall()]
    import tempfile
    handle = tempfile.NamedTemporaryFile(prefix="mdp-export-", suffix=".zip", delete=False)
    handle.close()
    destination = Path(handle.name)
    try:
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            yolo_splits = _legacy_yolo_splits(assets_rows) if export_format == "yolo" else {}
            if export_format == "coco":
                _write_coco(archive, labels, assets_rows, annotations_rows)
            else:
                _write_yolo(archive, labels, assets_rows, annotations_rows, yolo_splits)
            media_root = settings.media_dir.resolve()
            for asset in assets_rows:
                path = (settings.media_dir / asset["storage_name"]).resolve()
                if media_root not in path.parents or not path.is_file():
                    raise HTTPException(
                        409, f"导出所需图片已被删除或文件缺失：{asset['filename']}。请刷新图片列表后重试。"
                    )
                image_prefix = (
                    f"images/{yolo_splits[asset['id']]}"
                    if export_format == "yolo"
                    else "images"
                )
                try:
                    archive.write(path, f"{image_prefix}/{asset['id']}_{asset['filename']}")
                except FileNotFoundError as exc:
                    raise HTTPException(
                        409, f"导出时图片已被删除：{asset['filename']}。请刷新图片列表后重试。"
                    ) from exc
            archive.writestr(
                "README.txt",
                f"Project: {project['name']}\nFormat: {export_format.upper()}\nImages: {len(assets_rows)}\nApproved only: {approved_only}\n",
            )
            archive.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "project_id": project_id,
                        "exported_at": now_iso(),
                        "approved_only": approved_only,
                        "labels": [
                            {
                                "id": label["id"],
                                "name": label["name"],
                                "attributes_schema": json.loads(
                                    label.get("attributes_schema_json") or "{}"
                                ),
                            }
                            for label in labels
                        ],
                        "assets": [
                            {
                                "id": a["id"],
                                "sha256": a["sha256"],
                                "revision": a["revision"],
                                **(
                                    {"split": yolo_splits[a["id"]]}
                                    if export_format == "yolo"
                                    else {}
                                ),
                            }
                            for a in assets_rows
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    background_tasks.add_task(destination.unlink, missing_ok=True)
    return FileResponse(destination, media_type="application/zip",
                        filename=f"project-{project_id}-{export_format}.zip")


def _validate_shape(shape_type: str, points: list[float], width: int, height: int) -> None:
    if shape_type == "classification":
        if points:
            raise HTTPException(422, "分类标注不包含坐标")
        return
    if shape_type == "keypoints":
        shape_type = "rectangle"
    if shape_type == "rectangle" and len(points) != 4:
        raise HTTPException(422, "矩形需要 [x1, y1, x2, y2] 四个坐标")
    if shape_type == "polygon" and (len(points) < 6 or len(points) % 2):
        raise HTTPException(422, "多边形至少需要三个点")
    for index, value in enumerate(points):
        if not math.isfinite(value):
            raise HTTPException(422, "标注坐标必须是有限数值")
        limit = width if index % 2 == 0 else height
        if value < 0 or value > limit:
            raise HTTPException(422, "标注坐标超出图片范围")
    if shape_type == "rectangle":
        x1, y1, x2, y2 = points
        if x2 <= x1 or y2 <= y1:
            raise HTTPException(422, "矩形必须满足 x2 > x1 且 y2 > y1")
        return
    area = 0.0
    vertices = list(zip(points[0::2], points[1::2]))
    for index, (x1, y1) in enumerate(vertices):
        x2, y2 = vertices[(index + 1) % len(vertices)]
        area += x1 * y2 - x2 * y1
    if abs(area) < 1.0:
        raise HTTPException(422, "多边形面积过小或所有顶点共线")


def _validate_attributes(label: dict, attributes: dict) -> None:
    """Validate the commonly used subset of JSON Schema stored on a label.

    Full JSON Schema can still be preserved in the database. Enforcing its core object,
    required, enum and primitive type rules here prevents invalid training metadata.
    """
    try:
        schema = json.loads(label.get("attributes_schema_json") or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(500, "标签属性结构配置损坏") from exc
    if not schema:
        return
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    for name in required:
        if name not in attributes:
            raise HTTPException(422, f"缺少必填属性：{name}")
    if schema.get("additionalProperties") is False:
        unknown = set(attributes) - set(properties)
        if unknown:
            raise HTTPException(422, f"存在未定义属性：{', '.join(sorted(unknown))}")
    type_map = {
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    for name, value in attributes.items():
        rule = properties.get(name)
        if not isinstance(rule, dict):
            continue
        expected = rule.get("type")
        expected_type = type_map.get(expected)
        if expected_type and (
            not isinstance(value, expected_type)
            or expected in {"number", "integer"} and isinstance(value, bool)
        ):
            raise HTTPException(422, f"属性 {name} 的类型应为 {expected}")
        if expected in {"number", "integer"} and not math.isfinite(value):
            raise HTTPException(422, f"属性 {name} 必须是有限数值")
        if "enum" in rule and value not in rule["enum"]:
            raise HTTPException(422, f"属性 {name} 不在允许值范围内")


def _validate_job_hints(hints: dict, width: int, height: int) -> None:
    points = hints.get("points")
    if points is None:
        return
    if not isinstance(points, list) or not points:
        raise HTTPException(422, "交互提示点不能为空")
    if len(points) > 200:
        raise HTTPException(422, "交互提示点数量不能超过 200")
    for point in points:
        if not isinstance(point, dict) or point.get("label") not in {0, 1}:
            raise HTTPException(422, "交互提示点必须包含 x、y 和 0/1 label")
        x, y = point.get("x"), point.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            raise HTTPException(422, "交互提示点坐标必须为数值")
        if not math.isfinite(x) or not math.isfinite(y) or not (0 <= x <= width and 0 <= y <= height):
            raise HTTPException(422, "交互提示点超出图片范围")


def _write_coco(archive: zipfile.ZipFile, labels: list[dict], assets: list[dict], rows: list[dict]) -> None:
    categories = [
        {
            "id": label["id"],
            "name": label["name"],
            "visionforge_attributes_schema": json.loads(
                label.get("attributes_schema_json") or "{}"
            ),
        }
        for label in labels
    ]
    images = [
        {
            "id": asset["id"],
            "file_name": f"images/{asset['id']}_{asset['filename']}",
            "width": asset["width"],
            "height": asset["height"],
        }
        for asset in assets
    ]
    annotations = []
    for row in rows:
        points = json.loads(row["points_json"])
        xs, ys = points[0::2], points[1::2]
        x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
        area = (x2 - x1) * (y2 - y1)
        if row["shape_type"] == "polygon":
            vertices = list(zip(xs, ys))
            area = abs(sum(
                vx * vertices[(index + 1) % len(vertices)][1]
                - vertices[(index + 1) % len(vertices)][0] * vy
                for index, (vx, vy) in enumerate(vertices)
            )) / 2
        item = {
            "id": row["id"],
            "image_id": row["asset_id"],
            "category_id": row["label_id"],
            "bbox": [x1, y1, x2 - x1, y2 - y1],
            "area": area,
            "iscrowd": 0,
            "attributes": json.loads(row["attributes_json"]),
            "visionforge": {
                "source": row["source"],
                "model_id": row["model_id"],
                "confidence": row["confidence"],
                "version": row["version"],
            },
        }
        if row["shape_type"] == "polygon":
            item["segmentation"] = [points]
        annotations.append(item)
    archive.writestr(
        "annotations/instances.json",
        json.dumps({"images": images, "annotations": annotations, "categories": categories}, ensure_ascii=False, indent=2),
    )


def _legacy_yolo_splits(assets: list[dict]) -> dict[int, str]:
    ordered = sorted(
        assets,
        key=lambda asset: hashlib.sha256(
            f"legacy-yolo-v1:{asset['sha256']}:{asset['id']}".encode()
        ).hexdigest(),
    )
    if len(ordered) <= 1:
        train_count = len(ordered)
    else:
        train_count = min(len(ordered) - 1, max(1, round(len(ordered) * 0.8)))
    return {
        asset["id"]: ("train" if index < train_count else "val")
        for index, asset in enumerate(ordered)
    }


def _write_yolo(
    archive: zipfile.ZipFile,
    labels: list[dict],
    assets: list[dict],
    rows: list[dict],
    splits: dict[int, str] | None = None,
) -> None:
    category_index = {label["id"]: index for index, label in enumerate(labels)}
    asset_map = {asset["id"]: asset for asset in assets}
    grouped: dict[int, list[str]] = {asset["id"]: [] for asset in assets}
    splits = splits or _legacy_yolo_splits(assets)
    segmentation_task = any(row["shape_type"] == "polygon" for row in rows)
    for row in rows:
        asset = asset_map[row["asset_id"]]
        points = json.loads(row["points_json"])
        class_id = category_index[row["label_id"]]
        if row["shape_type"] == "polygon" or segmentation_task:
            if row["shape_type"] == "rectangle":
                x1, y1, x2, y2 = points
                points = [x1, y1, x2, y1, x2, y2, x1, y2]
            coords = [
                str(round(value / (asset["width"] if i % 2 == 0 else asset["height"]), 6))
                for i, value in enumerate(points)
            ]
            line = f"{class_id} " + " ".join(coords)
        else:
            x1, y1, x2, y2 = points
            line = " ".join(
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
        grouped[row["asset_id"]].append(line)
    for asset in assets:
        archive.writestr(
            f"labels/{splits[asset['id']]}/{asset['id']}_{Path(asset['filename']).stem}.txt",
            "\n".join(grouped[asset["id"]]),
        )
    names = ", ".join(json.dumps(label["name"], ensure_ascii=False) for label in labels)
    archive.writestr(
        "data.yaml",
        f"path: .\ntrain: images/train\nval: images/val\nnc: {len(labels)}\nnames: [{names}]\n",
    )


STATIC_DIR = BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/{path:path}", response_class=HTMLResponse, include_in_schema=False)
def spa_fallback(path: str) -> FileResponse:
    if path.startswith("api/"):
        raise HTTPException(404)
    return FileResponse(STATIC_DIR / "index.html")
