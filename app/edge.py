"""Project-scoped reCamera ingestion; capture receipts are durable and idempotent.

Devices use their own credentials and can only append original images to their
bound project. They cannot edit labels, human annotations, blocks or datasets.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import Actor, get_actor, require_admin
from .config import settings
from .database import db, now_iso, one
from .storage import _store_image

router = APIRouter(prefix="/api/edge", tags=["edge-devices"])
MAX_CAPTURE_BYTES = 10 * 1024 * 1024
TOKEN_PREFIX = "cvsprout_device_"
CONNECTION_TIMEOUT_SECONDS = 90


def _server_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or any(c.isspace() or ord(c) < 32 for c in value)):
            raise ValueError()
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("请填写不含账号、密码和查询参数的 HTTP(S) 服务地址") from exc
    return value


class CreateDevice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    project_id: int = Field(gt=0, le=2**63 - 1, strict=True)
    server_url: str = Field(default="", max_length=2048)
    capture_enabled: bool = Field(default=True, strict=True)
    capture_interval_seconds: int = Field(default=10, ge=1, le=86400, strict=True)
    max_pending_images: int = Field(default=500, ge=1, le=10000, strict=True)
    max_pending_bytes: int = Field(default=268435456, ge=1024 * 1024, le=8 * 1024**3, strict=True)

    @field_validator("name")
    @classmethod
    def name_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("请输入设备名称")
        return value.strip()

    @field_validator("server_url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return _server_url(value)


class UpdateDevice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=100)
    server_url: str | None = Field(default=None, max_length=2048)
    capture_enabled: bool | None = Field(default=None, strict=True)
    capture_interval_seconds: int | None = Field(default=None, ge=1, le=86400, strict=True)
    max_pending_images: int | None = Field(default=None, ge=1, le=10000, strict=True)
    max_pending_bytes: int | None = Field(default=None, ge=1024 * 1024, le=8 * 1024**3, strict=True)

    @field_validator("name")
    @classmethod
    def name_nonblank(cls, value: str | None) -> str:
        if value is None or not value.strip():
            raise ValueError("请输入设备名称")
        return value.strip()

    @field_validator("server_url")
    @classmethod
    def valid_url(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("服务地址不能是 null")
        return _server_url(value)


class DeviceHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app_version: str = Field(default="", max_length=100)
    firmware_version: str = Field(default="", max_length=100)
    sdk_version: str = Field(default="", max_length=100)
    state: str = Field(default="idle", max_length=64)
    pending_images: int = Field(default=0, ge=0, le=1000000, strict=True)
    pending_bytes: int = Field(default=0, ge=0, le=2**63 - 1, strict=True)
    uploaded_images: int = Field(default=0, ge=0, le=2**63 - 1, strict=True)
    last_error: str = Field(default="", max_length=500)
    model_release: str = Field(default="", max_length=128)
    frames_received: int | None = Field(default=None, ge=0, le=2**63 - 1, strict=True)
    captured_images: int | None = Field(default=None, ge=0, le=2**63 - 1, strict=True)
    last_frame_age_seconds: float | None = Field(default=None, ge=0, le=2**53, allow_inf_nan=False, strict=True)
    capture_error: str = Field(default="", max_length=500)
    upload_error: str = Field(default="", max_length=500)
    session_complete: bool | None = Field(default=None, strict=True)


def _heartbeat_data(device: dict) -> dict:
    try:
        value = json.loads(device["heartbeat_json"])
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _safe_text(value: str) -> str:
    # All device strings are untrusted, including version and state fields.
    value = re.sub(r"cvsprout_device_[A-Za-z0-9_-]+", "[redacted]", value, flags=re.IGNORECASE)
    value = re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer [redacted]", value)
    return "".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in value)


def _public_heartbeat(values: dict) -> dict:
    # Ignore internal/future keys and tolerate damaged legacy rows instead of
    # making the entire device page unavailable. Never reflect arbitrary keys.
    clean = {key: value for key, value in values.items() if key in DeviceHeartbeat.model_fields}
    try:
        clean = DeviceHeartbeat.model_validate(clean).model_dump()
    except ValueError:
        return {}
    return {key: _safe_text(value) if isinstance(value, str) else value for key, value in clean.items()}


def _timestamp(value) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return timestamp.astimezone(timezone.utc) if timestamp.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _age_seconds(value, now: datetime) -> float | None:
    timestamp = _timestamp(value)
    if timestamp is None:
        return None
    age = (now - timestamp).total_seconds()
    # These are server timestamps. A substantially future value is corrupt or
    # follows a server clock correction; it cannot prove a current connection.
    return round(max(0.0, age), 3) if age >= -5 else None


def _connection(device: dict, heartbeat: dict, now: datetime) -> dict:
    age = _age_seconds(device["last_seen_at"], now)
    heartbeat_at = heartbeat.get("_received_at")
    heartbeat_age = _age_seconds(heartbeat_at, now)
    rotated, seen, created = (_timestamp(device[key]) for key in ("token_rotated_at", "last_seen_at", "created_at"))
    reconnect = (heartbeat.get("_awaiting_reconnect") is True
                 or (heartbeat.get("_credential_confirmed") is not True
                     and ((rotated is not None and seen is not None and rotated > seen)
                          or (seen is None and rotated is not None and created is not None and rotated > created))))
    if not device["enabled"] or device["revoked_at"]:
        state = "revoked"
    elif reconnect:
        state = "reconnect_required"
    elif not device["last_seen_at"]:
        state = "never_connected"
    elif age is not None and age <= CONNECTION_TIMEOUT_SECONDS:
        state = "online"
    else:
        state = "offline"
    return {"state": state, "online": state == "online", "server_time": now.isoformat(),
            "last_contact_at": device["last_seen_at"] if seen is not None else None,
            "last_heartbeat_at": heartbeat_at if _timestamp(heartbeat_at) is not None else None,
            "age_seconds": age, "heartbeat_age_seconds": heartbeat_age,
            "timeout_seconds": CONNECTION_TIMEOUT_SECONDS}


def _ingestion(device: dict, heartbeat: dict, internal: dict, now: datetime) -> dict:
    connection = device["connection"]
    heartbeat_age = connection["heartbeat_age_seconds"]
    fresh = (connection["online"] and bool(heartbeat) and internal.get("_heartbeat_stale") is not True
             and heartbeat_age is not None and heartbeat_age <= CONNECTION_TIMEOUT_SECONDS)
    report = heartbeat if fresh else {}
    error = report.get("capture_error") or report.get("upload_error") or report.get("last_error", "")
    pending = report.get("pending_images")
    capture_age = _age_seconds(device["last_capture_at"], now)
    image_timeout = max(CONNECTION_TIMEOUT_SECONDS, device["capture_interval_seconds"] * 3)
    state = "waiting_first_image"
    if device["project_archived"]:
        state = "archived"
    elif not connection["online"]:
        state = "waiting_connection"
    elif not device["capture_enabled"]:
        state = "paused"
    elif not fresh:
        state = "receiving" if capture_age is not None and capture_age <= CONNECTION_TIMEOUT_SECONDS else "waiting_heartbeat"
    elif report.get("state") == "queue_full":
        state = "queue_full"
    elif error or report.get("state") in {"capture_error", "capture_stopped"}:
        state = "error"
    elif pending:
        state = "upload_pending"
    elif report.get("session_complete") is True or report.get("state") == "session_limit":
        state = "session_complete"
    elif report.get("state") == "waiting_frames" or report.get("frames_received") == 0:
        state = "waiting_camera"
    elif device["capture_count"]:
        state = "receiving" if capture_age is not None and capture_age <= image_timeout else "no_recent_images"
    return {"state": state, "last_error": error, "heartbeat_fresh": fresh,
            "pending_images": pending, "pending_bytes": report.get("pending_bytes"),
            "captured_images": report.get("captured_images"), "frames_received": report.get("frames_received"),
            "last_frame_age_seconds": report.get("last_frame_age_seconds"), "last_capture_age_seconds": capture_age,
            "image_timeout_seconds": image_timeout}


def _contact(conn, device: dict, now: str) -> None:
    heartbeat = _heartbeat_data(device)
    heartbeat.pop("_awaiting_reconnect", None)
    heartbeat["_credential_confirmed"] = True
    conn.execute("UPDATE edge_devices SET last_seen_at=?,heartbeat_json=? WHERE id=?",
                 (now, json.dumps(heartbeat, ensure_ascii=False), device["id"]))


def _token() -> tuple[str, str]:
    value = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return value, hashlib.sha256(value.encode()).hexdigest()


def _unauthorized() -> HTTPException:
    return HTTPException(401, "设备凭据无效或已撤销，请在设备中心重新配置", headers={"WWW-Authenticate": "Bearer"})


def get_device(request: Request, authorization: str | None = Header(default=None)) -> dict:
    """Separate dependency, intentionally independent of human authentication."""
    if not authorization or not authorization.startswith("Bearer " + TOKEN_PREFIX):
        raise _unauthorized()
    token = authorization.removeprefix("Bearer ").strip()
    if len(token) > 256:
        raise _unauthorized()
    row = one("SELECT * FROM edge_devices WHERE token_hash=? AND enabled=1 AND revoked_at IS NULL",
              (hashlib.sha256(token.encode()).hexdigest(),))
    if row is None:
        raise _unauthorized()
    return row


def _device(conn, device_id: str) -> dict:
    row = conn.execute("SELECT * FROM edge_devices WHERE id=?", (device_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "设备不存在")
    return dict(row)


def _current_device(conn, authenticated: dict) -> dict:
    # Re-check while holding the write transaction: rotation or revocation cannot
    # race an upload between its dependency authentication and its commit.
    row = conn.execute("SELECT * FROM edge_devices WHERE id=? AND token_hash=? AND enabled=1 AND revoked_at IS NULL",
                       (authenticated["id"], authenticated["token_hash"])).fetchone()
    if row is None:
        raise _unauthorized()
    return dict(row)


def _project(conn, project_id: int, *, write: bool = False):
    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "项目不存在或已删除")
    if write and row["archived_at"]:
        raise HTTPException(409, "项目已归档，设备暂不能导入图片")
    return row


def _present(conn, device: dict) -> dict:
    result = {key: value for key, value in device.items() if key not in {"token_hash", "heartbeat_json"}}
    result["enabled"] = bool(result["enabled"])
    result["capture_enabled"] = bool(result["capture_enabled"])
    internal_heartbeat = _heartbeat_data(device)
    result["heartbeat"] = _public_heartbeat(internal_heartbeat)
    project = conn.execute("SELECT name,archived_at FROM projects WHERE id=?", (device["project_id"],)).fetchone()
    result["project_name"] = project["name"] if project else ""
    result["project_archived"] = bool(project and project["archived_at"])
    counts = conn.execute("SELECT COUNT(*) AS captures,COUNT(DISTINCT asset_id) AS assets FROM edge_capture_events WHERE device_id=?", (device["id"],)).fetchone()
    result["capture_count"], result["asset_count"] = counts["captures"], counts["assets"]
    now = _timestamp(now_iso()) or datetime.now(timezone.utc)
    result["connection"] = _connection(device, internal_heartbeat, now)
    result["ingestion"] = _ingestion(result, result["heartbeat"], internal_heartbeat, now)
    return result


def _audit(conn, actor: Actor, action: str, device: dict, payload: dict | None = None) -> None:
    conn.execute("""INSERT INTO audit_logs(actor,action,resource_type,resource_id,project_id,payload_json,created_at)
        VALUES (?,?, 'edge_device',?,?,?,?)""",
                 (actor.id, action, device["id"], device["project_id"], json.dumps(payload or {}, ensure_ascii=False), now_iso()))


@router.get("/devices")
def list_devices(actor: Actor = Depends(get_actor)) -> dict:
    require_admin(actor)
    with db() as conn:
        return {"devices": [_present(conn, dict(row)) for row in conn.execute("SELECT * FROM edge_devices ORDER BY created_at DESC,id")]}


@router.post("/devices", status_code=201)
def create_device(payload: CreateDevice, request: Request, actor: Actor = Depends(get_actor)) -> dict:
    require_admin(actor)
    values = payload.model_dump()
    values["server_url"] = values["server_url"] or _server_url(settings.public_origin or str(request.base_url))
    token, token_hash = _token()
    device_id, now = str(uuid.uuid4()), now_iso()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _project(conn, payload.project_id, write=True)
        conn.execute("""INSERT INTO edge_devices(id,project_id,name,server_url,token_hash,capture_enabled,
            capture_interval_seconds,max_pending_images,max_pending_bytes,token_rotated_at,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (device_id, values["project_id"], values["name"], values["server_url"], token_hash,
                values["capture_enabled"], values["capture_interval_seconds"], values["max_pending_images"],
                values["max_pending_bytes"], now, now, now))
        device = _device(conn, device_id)
        _audit(conn, actor, "create", device, {"name": device["name"]})
        result = _present(conn, device)
    return {**result, "device_token": token}


@router.patch("/devices/{device_id}")
def update_device(device_id: str, payload: UpdateDevice, actor: Actor = Depends(get_actor)) -> dict:
    require_admin(actor)
    changes = payload.model_dump(exclude_unset=True)
    if any(value is None for value in changes.values()):
        raise HTTPException(422, "配置项不能是 null")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        device = _device(conn, device_id)
        if device["revoked_at"]:
            raise HTTPException(409, "设备已撤销，请新建设备接入")
        if changes:
            changes["updated_at"] = now_iso()
            columns = ",".join(f"{column}=?" for column in changes)
            conn.execute(f"UPDATE edge_devices SET {columns} WHERE id=?", (*changes.values(), device_id))
            _audit(conn, actor, "configure", device, {"fields": sorted(changes)})
        return _present(conn, _device(conn, device_id))


@router.post("/devices/{device_id}/rotate-token")
def rotate_token(device_id: str, actor: Actor = Depends(get_actor)) -> dict:
    require_admin(actor)
    token, token_hash = _token()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        device = _device(conn, device_id)
        if device["revoked_at"]:
            raise HTTPException(409, "设备已撤销，请新建设备接入")
        now = now_iso()
        previous_heartbeat = _heartbeat_data(device)
        previous_heartbeat.update({"_awaiting_reconnect": True, "_heartbeat_stale": True, "_credential_confirmed": False})
        conn.execute("UPDATE edge_devices SET token_hash=?,token_rotated_at=?,updated_at=?,heartbeat_json=? WHERE id=?",
                     (token_hash, now, now, json.dumps(previous_heartbeat, ensure_ascii=False), device_id))
        _audit(conn, actor, "rotate_token", device)
        result = _present(conn, _device(conn, device_id))
    return {"device": result, "device_token": token}


@router.delete("/devices/{device_id}")
def revoke_device(device_id: str, actor: Actor = Depends(get_actor)) -> dict:
    require_admin(actor)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        device = _device(conn, device_id)
        if not device["revoked_at"]:
            now = now_iso()
            conn.execute("UPDATE edge_devices SET enabled=0,capture_enabled=0,token_hash=NULL,revoked_at=?,updated_at=? WHERE id=?", (now, now, device_id))
            _audit(conn, actor, "revoke", device)
    return {"revoked": True, "id": device_id}


@router.get("/devices/{device_id}/captures")
def list_captures(device_id: str, limit: int = Query(default=25, ge=1, le=200),
                  offset: int = Query(default=0, ge=0, le=2**63 - 1), actor: Actor = Depends(get_actor)) -> dict:
    require_admin(actor)
    with db() as conn:
        _device(conn, device_id)
        total = conn.execute("SELECT COUNT(*) FROM edge_capture_events WHERE device_id=?", (device_id,)).fetchone()[0]
        items = [dict(row) for row in conn.execute("""SELECT e.*,a.filename FROM edge_capture_events e
            LEFT JOIN assets a ON a.id=e.asset_id WHERE e.device_id=? ORDER BY e.id DESC LIMIT ? OFFSET ?""", (device_id, limit, offset))]
        for item in items:
            item["duplicate"] = bool(item["duplicate"])
        return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/v1/config")
def device_config(device: dict = Depends(get_device)) -> dict:
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        device = _current_device(conn, device)
        project = _project(conn, device["project_id"])
        _contact(conn, device, now_iso())
        return {"device_id": device["id"], "project_id": device["project_id"],
                "capture_enabled": bool(device["capture_enabled"] and not project["archived_at"]),
                "capture_interval_seconds": device["capture_interval_seconds"],
                "max_pending_images": device["max_pending_images"], "max_pending_bytes": device["max_pending_bytes"],
                "max_image_bytes": MAX_CAPTURE_BYTES, "project_archived": bool(project["archived_at"]),
                "heartbeat_schema_version": 2, "system_model_schema_version": 1}


@router.post("/v1/heartbeat")
def heartbeat(payload: DeviceHeartbeat, device: dict = Depends(get_device)) -> dict:
    values = _public_heartbeat(payload.model_dump())
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        device = _current_device(conn, device)
        now = now_iso()
        values["_received_at"] = now
        values["_credential_confirmed"] = True
        conn.execute("UPDATE edge_devices SET last_seen_at=?,heartbeat_json=? WHERE id=?", (now, json.dumps(values, ensure_ascii=False), device["id"]))
    return {"received": True, "server_time": now}


def _capture_time(value: str) -> str:
    if len(value) > 64:
        raise HTTPException(422, "captured_at 必须为含时区的 ISO 时间")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError()
        return timestamp.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError) as exc:
        raise HTTPException(422, "captured_at 必须为含时区的 ISO 时间") from exc


def _commit_capture(device: dict, content: bytes, filename: str, capture_id: str,
                    captured_at: str, batch_id: str, model_release: str) -> dict:
    image = None
    retained = False
    try:
        try:
            image = _store_image(filename, content)
        except ValueError as exc:
            raise HTTPException(415, "请上传有效的 JPEG、PNG、WebP 或 BMP 原图") from exc
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            device = _current_device(conn, device)
            _project(conn, device["project_id"], write=True)
            now = now_iso()
            previous = conn.execute("SELECT * FROM edge_capture_events WHERE device_id=? AND capture_id=?", (device["id"], capture_id)).fetchone()
            if previous:
                if previous["sha256"] != image.sha256:
                    raise HTTPException(409, "capture_id 已用于另一张图片，请保留原始队列记录或使用新的采集编号")
                if previous["asset_id"] is None:
                    raise HTTPException(410, "本次采集对应的图片已被删除，不会重复导入")
                result = {"received": True, "capture_id": capture_id, "asset_id": previous["asset_id"], "duplicate": bool(previous["duplicate"])}
            else:
                duplicate = conn.execute("SELECT id FROM assets WHERE project_id=? AND sha256=? ORDER BY id LIMIT 1", (device["project_id"], image.sha256)).fetchone()
                if duplicate:
                    asset_id = duplicate["id"]
                else:
                    asset_id = conn.execute("""INSERT INTO assets(project_id,filename,storage_name,mime_type,width,height,
                        size_bytes,sha256,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (device["project_id"], image.filename, image.storage_name, image.mime_type, image.width,
                         image.height, image.size_bytes, image.sha256, now, now)).lastrowid
                conn.execute("""INSERT INTO edge_capture_events(device_id,project_id,capture_id,asset_id,sha256,
                    captured_at,received_at,batch_id,model_release,duplicate) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (device["id"], device["project_id"], capture_id, asset_id, image.sha256, captured_at, now,
                     batch_id, model_release, bool(duplicate)))
                conn.execute("UPDATE edge_devices SET last_capture_at=? WHERE id=?", (now, device["id"]))
                if not duplicate:
                    conn.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, device["project_id"]))
                result = {"received": True, "capture_id": capture_id, "asset_id": asset_id, "duplicate": bool(duplicate)}
            _contact(conn, device, now)
        # Only preserve a newly staged file after its SQLite transaction commits.
        retained = previous is None and not duplicate
        return result
    finally:
        if image is not None and not retained:
            (settings.media_dir / image.storage_name).unlink(missing_ok=True)


@router.post("/v1/captures", status_code=201)
async def ingest_capture(file: UploadFile = File(...), capture_id: str = Form(...),
                         captured_at: str = Form(...), batch_id: str = Form(default=""),
                         model_release: str = Form(default=""), device: dict = Depends(get_device)) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", capture_id):
        raise HTTPException(422, "capture_id 必须为 1–128 位字母、数字、点、下划线、冒号或连字符")
    if len(batch_id) > 128 or len(model_release) > 128:
        raise HTTPException(422, "批次或模型版本标识不能超过 128 个字符")
    captured_at = _capture_time(captured_at)
    content = await file.read(MAX_CAPTURE_BYTES + 1)
    if len(content) > MAX_CAPTURE_BYTES:
        raise HTTPException(413, "设备单张图片不能超过 10 MiB")
    filename = (file.filename or f"capture-{capture_id}.jpg").replace("\\", "/").split("/")[-1][:255]
    return await asyncio.to_thread(_commit_capture, device, content, filename, capture_id, captured_at, batch_id, model_release)
