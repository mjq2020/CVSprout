"""Immutable system models and fenced device-pulled deployment operations.

An expired lease never gives a possibly mutated device to another operation.
Only its persisted agent identity and lease secret can reconcile it. Device
credential rotation invalidates that lease as well as ordinary device access.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import Actor, get_actor, require_admin
from .config import settings
from .database import all_rows, audit, db, now_iso, one
from .edge import _contact, _current_device, _device, _project, _timestamp, get_device
from .file_responses import pinned_file_response

router = APIRouter(prefix="/api/edge", tags=["edge-system-models"])
LEASE_SECONDS = 120
STATUS_SECONDS = 90
BUILD_TIMEOUT_SECONDS = 15 * 60
MAX_MODEL_BYTES = 500 * 1024 * 1024
MAX_TASK_METADATA_BYTES = 1024 * 1024
PROFILE = "rv1126b-yolo-dfl-int8-v1"
MODEL_NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
ACTIVE = ("queued", "running", "unknown")
TERMINAL = ("succeeded", "failed", "cancelled")
STAGES = {"queued": 0, "downloading": 1, "uploading": 2, "uploaded": 3,
          "activating": 4, "activated": 5, "verified": 6}
ERROR_CODES = {
    "", "device_error", "system_api_unavailable", "system_api_unauthorized",
    "system_api_unsupported", "system_model_incompatible", "system_model_busy",
    "artifact_download_failed", "artifact_integrity_failed", "insufficient_storage",
    "upload_failed", "activation_failed", "verification_failed", "rollback_failed",
    "lease_expired", "credential_changed", "deployment_interrupted",
    "server_timeout", "server_connection_refused", "server_dns_failed",
    "server_tls_verification_failed", "server_unreachable_or_invalid_response",
    "server_redirect_refused", "artifact_unavailable",
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _age(value, now: str) -> float | None:
    parsed, current = _timestamp(value), _timestamp(now)
    if parsed is None or current is None:
        return None
    age = (current - parsed).total_seconds()
    return age if age >= -5 else None


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class RuntimeStatus(StrictModel):
    iEnable: int = Field(ge=0, le=1, strict=True)
    sModel: str = Field(default="", max_length=255)
    sStatus: Literal["running", "starting", "stopping", "stopped", "error", "unknown"]
    iFPS: int = Field(default=0, ge=0, le=1000, strict=True)
    iActualFPS: float = Field(default=0, ge=0, le=1000)
    bExternalHold: bool = Field(default=False, strict=True)

    @field_validator("sModel")
    @classmethod
    def filename(cls, value):
        if any(ord(c) < 32 for c in value) or "/" in value or "\\" in value:
            raise ValueError("invalid system model filename")
        return value


class Capabilities(StrictModel):
    schema_version: Literal[1] = 1
    target_platform: Literal["rv1126b"] = "rv1126b"
    model_api: bool = Field(strict=True)
    algorithms: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("algorithms")
    @classmethod
    def algorithms_valid(cls, values):
        if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", value) for value in values):
            raise ValueError("invalid algorithm name")
        return sorted(set(values))


class AgentIdentity(StrictModel):
    agent_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class SystemStatus(AgentIdentity):
    capabilities: Capabilities
    current: RuntimeStatus | None = None
    firmware_version: str = Field(default="", max_length=200)
    available_bytes: int | None = Field(default=None, ge=0, le=2**63 - 1, strict=True)
    error_code: str = Field(default="", max_length=2000)

    @field_validator("error_code")
    @classmethod
    def safe_error(cls, value):
        return value if value in ERROR_CODES or re.fullmatch(r"http_[1-5][0-9]{2}", value) else "device_error"

    @field_validator("firmware_version")
    @classmethod
    def safe_version(cls, value):
        # A firmware version is not a place for raw responses or HTTP headers.
        return value if re.fullmatch(r"[A-Za-z0-9 ._+()/-]{0,200}", value) else "unknown"


class Claim(AgentIdentity):
    task_id: int | None = Field(default=None, gt=0, le=2**63 - 1, strict=True)
    lease_token: str | None = Field(default=None, min_length=32, max_length=128)
    observe_only: bool = Field(default=False, strict=True)


class Report(AgentIdentity):
    lease_token: str = Field(min_length=32, max_length=128)
    sequence: int = Field(ge=0, le=2**53 - 1, strict=True)
    status: Literal["running", "succeeded", "failed", "unknown"]
    stage: Literal["downloading", "uploading", "uploaded", "activating", "activated", "verified", "failed", "unknown"]
    error_code: str = Field(default="", max_length=2000)
    current: RuntimeStatus | None = None
    rollback_status: Literal["not_needed", "restored", "failed", "unknown"] = "not_needed"

    _safe_error = field_validator("error_code")(SystemStatus.safe_error.__func__)


class CreateRelease(StrictModel):
    conversion_id: int = Field(gt=0, le=2**63 - 1, strict=True)


class CreateDeployment(StrictModel):
    release_id: int = Field(gt=0, le=2**63 - 1, strict=True)
    activate: bool = Field(default=False, strict=True)
    fps: int = Field(default=10, ge=1, le=60, strict=True)
    model_name: str = Field(default="", max_length=64, strict=True)

    @field_validator("model_name", mode="before")
    @classmethod
    def safe_model_name(cls, value):
        if not isinstance(value, str):
            return value
        value = value.strip()
        if value and not re.fullmatch(MODEL_NAME_PATTERN, value):
            raise ValueError("模型名称须以英文字母或数字开头，只能包含英文字母、数字、下划线和连字符，最多 64 个字符")
        return value


class ResolveDeployment(StrictModel):
    confirmation_name: str = Field(min_length=1, max_length=200)
    acknowledged: Literal[True]
    expected_updated_at: str = Field(min_length=1, max_length=64)


def model_contract(conversion: dict, run: dict) -> dict:
    """Fail closed on the system decoder's tensor and preprocessing contract."""
    if conversion.get("status") != "completed" or conversion.get("format") != "rknn":
        raise ValueError("请先完成系统模型 RKNN 转换")
    try:
        metadata = json.loads(conversion.get("artifact_metadata_json") or "{}")
        deployment = metadata["deployment"]
        system = deployment["system_model"]
        if (deployment.get("deployment_profile") != "recamera_system"
                or deployment.get("target_platform") != "rv1126b"
                or deployment.get("precision") != "int8"
                or deployment.get("task") != "detect" or run.get("task") != "detect"
                or system.get("schema_version") != 1 or system.get("profile") != PROFILE
                or system.get("category") != "Detection"
                or system.get("algorithm") not in ("yolov8", "yolo11")):
            raise ValueError("需要 RV1126B INT8 的 YOLOv8/11 系统检测模型配置")
        size, names = system.get("input_size"), system.get("classes")
        if (type(size) is not int or not 32 <= size <= 2048 or size % 32
                or not isinstance(names, list) or not 1 <= len(names) <= 10000
                or any(not isinstance(name, str) or not name.strip() or len(name) > 200
                       or any(ord(c) < 32 for c in name) for name in names)
                or len(set(names)) != len(names)):
            raise ValueError("系统模型输入尺寸或类别信息无效")
        labels = deployment.get("labels")
        if labels != [{"category_index": i, "name": name} for i, name in enumerate(names)]:
            # Some converters preserve extra label provenance fields.
            if (not isinstance(labels, list) or len(labels) != len(names)
                    or any(not isinstance(item, dict) or item.get("category_index") != i
                           or item.get("name") != names[i] for i, item in enumerate(labels))):
                raise ValueError("系统模型类别顺序与转换标签不一致")
        pre = system.get("preprocessing") or {}
        expected_pre = {"color_space": "RGB", "dtype": "uint8", "resize": "letterbox",
                        "padding_value": 114, "padding_position": "bottom_right"}
        if any(pre.get(k) != v for k, v in expected_pre.items()):
            raise ValueError("系统模型必须使用 RGB、右下补边、114 填充预处理")
        compiled_pre = deployment.get("preprocessing") or {}
        if (compiled_pre.get("input_color") != "rgb" or compiled_pre.get("resize") != "letterbox"
                or compiled_pre.get("resize_rounding") != "floor_even"
                or compiled_pre.get("padding_position") != "bottom_right" or compiled_pre.get("padding_value") != 114
                or compiled_pre.get("mean_values") != [0, 0, 0] or compiled_pre.get("std_values") != [255, 255, 255]
                or compiled_pre.get("normalization_location") != "rknn_input_preprocessing"
                or compiled_pre.get("auto_padding") is not False or compiled_pre.get("image_size") != [size, size]):
            raise ValueError("RKNN 编译预处理与系统运行约定不一致")
        shapes = [[1, channels, size // stride, size // stride]
                  for stride in (8, 16, 32) for channels in (64, len(names))]
        outputs = system.get("outputs") or []
        if (len(outputs) != 6 or any(not isinstance(out, dict) or out.get("shape") != shape
                or out.get("dtype") != "INT8" or out.get("role") != ("box" if i % 2 == 0 else "class")
                for i, (out, shape) in enumerate(zip(outputs, shapes)))):
            raise ValueError("系统解码器要求六个 INT8 DFL 输出，不能使用旧版单输出模型")
        source = deployment.get("source_onnx") or {}
        inputs, source_outputs = source.get("inputs") or [], source.get("outputs") or []
        post = deployment.get("postprocessing") or {}
        if (len(inputs) != 1 or inputs[0].get("shape") != [1, 3, size, size]
                or inputs[0].get("dtype") != "FLOAT" or len(source_outputs) != 6
                or any(out.get("shape") != shape or out.get("dtype") != "FLOAT"
                       for out, shape in zip(source_outputs, shapes))
                or source.get("contains_non_max_suppression") is not False
                or post.get("family") != "recamera_system_dfl"
                or post.get("nms_in_graph") is not False or post.get("end2end") is not False):
            raise ValueError("ONNX 输出与系统 DFL 解码契约不一致")
        run_metadata = json.loads(run.get("artifact_metadata_json") or "{}")
        arch = str(run_metadata.get("model_arch") or run.get("model_arch") or "").lower()
        family = re.fullmatch(r"(yolov8|yolo11)(?:[nslmx])?(?:\.yaml|\.pt)?", arch)
        if not family or family[1] != system["algorithm"]:
            raise ValueError("训练模型架构与系统后处理算法不一致")
        if (not re.fullmatch(r"[a-f0-9]{64}", metadata.get("sha256", ""))
                or type(metadata.get("size_bytes")) is not int or not 0 < metadata["size_bytes"] <= MAX_MODEL_BYTES):
            raise ValueError("转换模型缺少有效完整性记录，或超过 500 MiB")
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError("转换元数据缺少完整的系统模型契约，请重新转换") from exc
    contract = {**system, "task": "detect", "target_platform": "rv1126b", "precision": "int8",
                "conversion_id": conversion["id"], "training_run_id": run["id"], "board_validated": False}
    info = _model_info(contract, "0" * 32, metadata["size_bytes"])
    # Count escaped JSON too: the device journal uses ensure_ascii and the task
    # carries the ordered classes in both the contract and model information.
    if len(json.dumps({"contract": contract, "model_info": info}, ensure_ascii=True,
                      separators=(",", ":"), allow_nan=False).encode()) > MAX_TASK_METADATA_BYTES:
        raise ValueError("系统模型任务元数据超过 1 MiB，请减少类别名称长度或类别数量后重新转换")
    return contract


def _model_info(contract, md5, size):
    return {"category": "Detection", "algorithm": contract["algorithm"], "classes": contract["classes"],
            "metrics": {"confidence": 0.25, "iou": 0.45, "max_obj": 128}, "md5sum": md5, "size": size}


def _default_model_name(contract):
    return f"{contract['algorithm']}-run{contract['training_run_id']}-conv{contract['conversion_id']}"


def _task_filename(row, release_filename):
    name = dict(row).get("model_name", "")
    if not name:
        # Existing tasks must keep the filename persisted by older Connectors.
        return release_filename
    if not re.fullmatch(MODEL_NAME_PATTERN, name):
        raise HTTPException(409, "部署任务的模型名称无效")
    return f"{name}-r{row['release_id']}.rknn"


def _release_json(row) -> dict:
    value = dict(row)
    value["contract"] = json.loads(value.pop("contract_json"))
    value["model_info"] = json.loads(value.pop("model_info_json"))
    value["default_model_name"] = _default_model_name(value["contract"]) if value["status"] == "ready" else ""
    return value


def _release_path(row) -> Path:
    root = settings.training_dir / f"edge-model-release-{row['id']}"
    path = root / row["filename"]
    if (row["status"] != "ready" or not re.fullmatch(r"cvsprout-[0-9]+-[0-9a-f]{12}\.rknn", row["filename"])
            or root.is_symlink() or path.is_symlink() or root.resolve().parent != settings.training_dir.resolve()
            or not path.is_file() or path.stat().st_size != row["size_bytes"]):
        raise HTTPException(409, "系统模型发布文件已丢失或损坏")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != row["sha256"]:
        raise HTTPException(409, "系统模型发布文件完整性校验失败")
    return path


def _task_json(row, now: str | None = None) -> dict:
    value = dict(row)
    for key in ("lease_hash", "credential_hash", "report_hash"):
        value.pop(key, None)
    value["current"] = json.loads(value.pop("current_json"))
    value["activate"] = bool(value["activate"])
    value["lease_expired"] = bool(value["status"] in ("running", "unknown")
        and (not value["lease_expires_at"] or value["lease_expires_at"] <= (now or now_iso())))
    if "filename" not in value:
        release = one("SELECT filename FROM edge_model_releases WHERE id=?", (row["release_id"],))
        value["filename"] = release["filename"] if release else ""
    value["filename"] = _task_filename(row, value["filename"])
    return value


def _device_write(conn, authenticated: dict):
    device = _current_device(conn, authenticated)
    _project(conn, device["project_id"], write=True)
    return device


def _admin_device(conn, device_id):
    device = _device(conn, device_id)
    project = _project(conn, device["project_id"])
    return device, project


def _require_available(device, project):
    if not device["enabled"] or device["revoked_at"] or project["archived_at"]:
        raise HTTPException(409, "设备已撤销或项目已归档")


def _capabilities(conn, device):
    row = conn.execute("SELECT * FROM edge_system_status WHERE device_id=? AND credential_hash=?",
                       (device["id"], device["token_hash"])).fetchone()
    if not row:
        return None, None
    return dict(row), json.loads(row["capabilities_json"])


@router.post("/model-releases", status_code=201)
def create_release(payload: CreateRelease, actor: Actor = Depends(get_actor)):
    require_admin(actor)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        source = conn.execute("SELECT * FROM model_conversions WHERE id=?", (payload.conversion_id,)).fetchone()
        if not source:
            raise HTTPException(404, "转换记录不存在")
        conversion = dict(source)
        run = dict(conn.execute("SELECT * FROM training_runs WHERE id=?", (conversion["training_run_id"],)).fetchone())
        _project(conn, run["project_id"], write=True)
        try:
            contract = model_contract(conversion, run)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        previous = conn.execute("SELECT * FROM edge_model_releases WHERE conversion_id=? AND status IN ('building','ready')",
                                (conversion["id"],)).fetchone()
        if previous:
            if previous["status"] == "building":
                age = _age(previous["created_at"], now_iso())
                if age is None or age <= BUILD_TIMEOUT_SECONDS:
                    raise HTTPException(409, "系统模型发布正在生成，请稍后刷新")
                # A crashed API process must not permanently prevent another
                # release. A late original builder is fenced at its commit.
                conn.execute("UPDATE edge_model_releases SET status='failed',error_code='release_build_interrupted',finished_at=? WHERE id=?",
                             (now_iso(), previous["id"]))
            else:
                _release_path(previous)
                return _release_json(previous)
        now = now_iso()
        release_id = conn.execute("""INSERT INTO edge_model_releases(project_id,conversion_id,status,created_at,contract_json)
            VALUES (?,?,'building',?,?)""", (run["project_id"], conversion["id"], now, _json(contract))).lastrowid
    output = settings.training_dir / f"edge-model-release-{release_id}"
    try:
        from .training import _validated_artifact
        metadata = json.loads(conversion["artifact_metadata_json"])
        source_path = _validated_artifact(conversion["artifact_path"], "转换", metadata)
        with source_path.open("rb") as source_file:
            if source_file.read(4) != b"RKNN":
                raise ValueError("invalid RKNN artifact header")
        filename = f"cvsprout-{release_id}-{metadata['sha256'][:12]}.rknn"
        output.mkdir(exist_ok=False)
        temporary = output / "model.part"
        sha, md5, size = hashlib.sha256(), hashlib.md5(usedforsecurity=False), 0
        with source_path.open("rb") as reader, temporary.open("xb") as writer:
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                size += len(chunk)
                if size > MAX_MODEL_BYTES:
                    raise ValueError("model too large")
                writer.write(chunk)
                sha.update(chunk)
                md5.update(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if sha.hexdigest() != metadata["sha256"] or size != metadata["size_bytes"]:
            raise ValueError("artifact changed while copying")
        temporary.rename(output / filename)
        (output / filename).chmod(0o444)
        info = _model_info(contract, md5.hexdigest(), size)
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _project(conn, run["project_id"], write=True)
            changed = conn.execute("""UPDATE edge_model_releases SET status='ready',filename=?,sha256=?,md5=?,size_bytes=?,
                model_info_json=?,finished_at=? WHERE id=? AND status='building'""",
                (filename, sha.hexdigest(), md5.hexdigest(), size, _json(info), now_iso(), release_id))
            if changed.rowcount != 1:
                raise ValueError("release builder no longer owns this operation")
        audit(actor.id, "create", "edge_system_model_release", release_id, run["project_id"])
    except Exception as exc:
        shutil.rmtree(output, ignore_errors=True)
        with db() as conn:
            conn.execute("UPDATE edge_model_releases SET status='failed',error_code='artifact_unavailable',finished_at=? WHERE id=?",
                         (now_iso(), release_id))
        raise HTTPException(409, "系统模型发布失败，转换文件缺失、损坏或项目状态已变化") from exc
    return _release_json(one("SELECT * FROM edge_model_releases WHERE id=?", (release_id,)))


@router.get("/devices/{device_id}/system-models")
def system_models(device_id: str, conversion_id: int | None = Query(None, gt=0, le=2**63 - 1),
                  actor: Actor = Depends(get_actor)):
    require_admin(actor)
    now = now_iso()
    with db() as conn:
        device, project = _admin_device(conn, device_id)
        status, capabilities = _capabilities(conn, device)
        # Deployment occupancy belongs to the device, regardless of the selected model.
        jobs = conn.execute("""SELECT d.*,r.filename FROM edge_model_deployments d JOIN edge_model_releases r ON r.id=d.release_id
            WHERE d.device_id=? ORDER BY d.id DESC LIMIT 50""", (device_id,)).fetchall()
        legacy = conn.execute("SELECT 1 FROM edge_deployments WHERE device_id=? AND status IN ('queued','running','unknown')", (device_id,)).fetchone()
        model_params = (device["project_id"],) + ((conversion_id,) if conversion_id is not None else ())
        release_filter = " AND conversion_id=?" if conversion_id is not None else ""
        releases = conn.execute("SELECT * FROM edge_model_releases WHERE project_id=?" + release_filter
                                + " ORDER BY id DESC LIMIT 100", model_params).fetchall()
    candidates = []
    conversion_filter = " AND c.id=?" if conversion_id is not None else ""
    for conversion in all_rows("""SELECT c.* FROM model_conversions c JOIN training_runs r ON r.id=c.training_run_id
        WHERE r.project_id=? AND c.status='completed'""" + conversion_filter + " ORDER BY c.id DESC LIMIT 100", model_params):
        run = one("SELECT * FROM training_runs WHERE id=?", (conversion["training_run_id"],))
        if not run:
            continue
        candidate = {"id": conversion["id"], "training_run_id": run["id"], "model_arch": run["model_arch"],
                     "compatible": False, "reason": "", "default_model_name": ""}
        try:
            contract = model_contract(conversion, run)
            candidate.update(compatible=True, algorithm=contract["algorithm"], input_size=contract["input_size"],
                             labels=contract["classes"], precision="int8", default_model_name=_default_model_name(contract))
            if capabilities and contract["algorithm"] not in capabilities["algorithms"]:
                candidate.update(compatible=False, reason="当前设备系统没有该模型的后处理算法")
        except ValueError as exc:
            candidate["reason"] = str(exc)
        candidates.append(candidate)
    blocked = ""
    if not device["enabled"] or device["revoked_at"] or project["archived_at"]:
        blocked = "设备已撤销或项目已归档"
    elif any(row["status"] in ACTIVE for row in jobs) or legacy:
        blocked = "设备已有部署任务；未确认结果的任务需要原设备应用恢复对账"
    elif not capabilities or not capabilities["model_api"]:
        blocked = "请先启动新版设备连接应用，并确认系统模型接口可用"
    public_status = None
    if status:
        age = _age(status["reported_at"], now)
        public_status = {key: status[key] for key in ("agent_id", "reported_at", "firmware_version", "available_bytes", "error_code")}
        public_status.update(age_seconds=age, fresh=age is not None and age <= STATUS_SECONDS)
    return {"device_id": device_id, "project_id": device["project_id"], "capabilities": capabilities,
            "current": json.loads(status["current_json"]) if status else None, "system_status": public_status,
            "conversions": candidates, "releases": [_release_json(row) for row in releases],
            "deployments": [_task_json(row, now) for row in jobs], "can_deploy": not blocked, "blocked_reason": blocked,
            "server_time": now}


@router.post("/devices/{device_id}/model-deployments", status_code=202)
def create_deployment(device_id: str, payload: CreateDeployment, actor: Actor = Depends(get_actor)):
    require_admin(actor)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        device, project = _admin_device(conn, device_id)
        _require_available(device, project)
        release = conn.execute("SELECT * FROM edge_model_releases WHERE id=? AND project_id=?",
                               (payload.release_id, device["project_id"])).fetchone()
        if not release:
            raise HTTPException(404, "当前设备项目没有此系统模型发布记录")
        _release_path(release)
        status, capabilities = _capabilities(conn, device)
        contract = json.loads(release["contract_json"])
        if not capabilities or not capabilities["model_api"] or contract["algorithm"] not in capabilities["algorithms"]:
            raise HTTPException(409, "设备尚未报告兼容的系统模型接口，请先启动新版连接应用")
        now = now_iso()
        model_name = payload.model_name or _default_model_name(contract)
        try:
            task_id = conn.execute("""INSERT INTO edge_model_deployments(device_id,project_id,release_id,activate,fps,model_name,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,'queued',?,?)""", (device_id, device["project_id"], release["id"], int(payload.activate), payload.fps, model_name, now, now)).lastrowid
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "设备已有进行中或结果未知的部署，请等待原任务恢复确认") from exc
    audit(actor.id, "deploy", "edge_system_model_release", payload.release_id, device["project_id"])
    return _task_json(one("SELECT * FROM edge_model_deployments WHERE id=?", (task_id,)))


@router.post("/devices/{device_id}/model-deployments/{task_id}/cancel")
def cancel_deployment(device_id: str, task_id: int, actor: Actor = Depends(get_actor)):
    require_admin(actor)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _admin_device(conn, device_id)
        row = conn.execute("SELECT * FROM edge_model_deployments WHERE id=? AND device_id=?", (task_id, device_id)).fetchone()
        if not row:
            raise HTTPException(404, "部署任务不存在")
        if row["status"] == "cancelled":
            return _task_json(row)
        if row["status"] != "queued":
            raise HTTPException(409, "任务已被设备领取，不能取消或假定设备尚未修改")
        now = now_iso()
        conn.execute("UPDATE edge_model_deployments SET status='cancelled',stage='cancelled',updated_at=?,finished_at=? WHERE id=?", (now, now, task_id))
    audit(actor.id, "cancel", "edge_system_model_deployment", task_id, row["project_id"])
    return _task_json(one("SELECT * FROM edge_model_deployments WHERE id=?", (task_id,)))


@router.post("/v1/system/status")
def device_status(payload: SystemStatus, device: dict = Depends(get_device)):
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        device = _device_write(conn, device)
        active = conn.execute("SELECT agent_id FROM edge_model_deployments WHERE device_id=? AND status IN ('running','unknown')", (device["id"],)).fetchone()
        if active and active["agent_id"] != payload.agent_id:
            raise HTTPException(409, "已有其他设备应用实例持有部署任务，请恢复原实例")
        now = now_iso()
        conn.execute("""INSERT INTO edge_system_status(device_id,agent_id,credential_hash,capabilities_json,current_json,
            firmware_version,available_bytes,error_code,reported_at) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(device_id) DO UPDATE SET agent_id=excluded.agent_id,credential_hash=excluded.credential_hash,
            capabilities_json=excluded.capabilities_json,current_json=excluded.current_json,firmware_version=excluded.firmware_version,
            available_bytes=excluded.available_bytes,error_code=excluded.error_code,reported_at=excluded.reported_at""",
            (device["id"], payload.agent_id, device["token_hash"], _json(payload.capabilities.model_dump()),
             _json(payload.current.model_dump() if payload.current else None), payload.firmware_version,
             payload.available_bytes, payload.error_code, now))
        _contact(conn, device, now)
    return {"ok": True, "server_time": now}


@router.post("/devices/{device_id}/model-deployments/{task_id}/resolve")
def resolve_deployment(device_id: str, task_id: int, payload: ResolveDeployment,
                       actor: Actor = Depends(get_actor)):
    """Explicit administrative reconciliation; never claim a device rollback.

Wait until the original lease has expired so a conforming agent cannot still
start a firmware mutation under a cached lease. A typed confirmation records
the administrator's separate on-device inspection/stopping of the old agent.
"""
    require_admin(actor)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        device, _ = _admin_device(conn, device_id)
        if payload.confirmation_name != device["name"]:
            raise HTTPException(409, "请输入完整设备名称确认已在设备上核实并停止原部署")
        row = conn.execute("SELECT * FROM edge_model_deployments WHERE id=? AND device_id=?", (task_id, device_id)).fetchone()
        if not row:
            raise HTTPException(404, "部署任务不存在")
        now = now_iso()
        if (row["status"] not in ("running", "unknown") or row["updated_at"] != payload.expected_updated_at
                or row["lease_expires_at"] and row["lease_expires_at"] > now):
            raise HTTPException(409, "任务状态已变化或租约尚未到期，请刷新并在设备核实后重试")
        conn.execute("""UPDATE edge_model_deployments SET status='failed',stage='failed',error_code='admin_resolved_unknown',
            credential_hash=NULL,lease_expires_at=NULL,rollback_status='unknown',updated_at=?,finished_at=? WHERE id=?""",
            (now, now, task_id))
    audit(actor.id, "resolve_unknown", "edge_system_model_deployment", task_id, device["project_id"])
    return _task_json(one("SELECT * FROM edge_model_deployments WHERE id=?", (task_id,)))


def _authorize_task(conn, task_id, device, agent_id, lease_token, *, terminal_receipt=False):
    row = conn.execute("SELECT * FROM edge_model_deployments WHERE id=? AND device_id=? AND project_id=?",
                       (task_id, device["id"], device["project_id"])).fetchone()
    if not row:
        raise HTTPException(404, "部署任务不存在")
    if (not row["lease_hash"] or row["agent_id"] != agent_id or not lease_token
            or not secrets.compare_digest(row["lease_hash"], _digest(lease_token))):
        raise HTTPException(409, "部署任务租约不匹配，请恢复原设备应用的持久记录")
    # A resolved task has NO valid mutation/download lease. Its original agent
    # may read the terminal receipt using the CURRENT device credential so it
    # can discard its durable journal and accept a later task. Keeping only the
    # old secret's hash for this receipt never revives its authorization.
    if terminal_receipt and row["status"] == "failed" and row["error_code"] == "admin_resolved_unknown":
        return dict(row)
    if row["credential_hash"] != device["token_hash"]:
        raise HTTPException(409, "设备凭据已轮换，旧部署租约已失效；请管理员核实设备结果")
    return dict(row)


def _lease_until(now):
    return (_timestamp(now) + timedelta(seconds=LEASE_SECONDS)).isoformat()


def _claim_response(conn, row, token, now):
    release = conn.execute("SELECT * FROM edge_model_releases WHERE id=?", (row["release_id"],)).fetchone()
    task = _task_json(row, now)
    task.update({key: release[key] for key in ("sha256", "md5", "size_bytes")})
    task.update(contract=json.loads(release["contract_json"]), model_info=json.loads(release["model_info_json"]),
                artifact_url=f"/api/edge/v1/model-tasks/{row['id']}/artifact", lease_token=token)
    return {"task": task, "server_time": now, "lease_seconds": LEASE_SECONDS}


@router.post("/v1/model-tasks/claim")
def claim_task(payload: Claim, device: dict = Depends(get_device)):
    if payload.observe_only and payload.task_id is None:
        raise HTTPException(422, "只读对账必须指定原 task_id 和 lease_token，不能领取新任务")
    if payload.task_id is not None and payload.lease_token is None:
        raise HTTPException(422, "恢复部署需要同时提供 task_id 和 lease_token")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        device = _device_write(conn, device)
        now = now_iso()
        # Persisting a client-generated secret BEFORE the initial claim makes a
        # lost first response recoverable without handing out another lease.
        if payload.task_id is None and payload.lease_token:
            previous = conn.execute("SELECT id FROM edge_model_deployments WHERE device_id=? AND agent_id=? AND lease_hash=? ORDER BY id DESC LIMIT 1",
                                    (device["id"], payload.agent_id, _digest(payload.lease_token))).fetchone()
            if previous:
                payload = payload.model_copy(update={"task_id": previous["id"]})
        if payload.task_id is not None:
            row = _authorize_task(conn, payload.task_id, device, payload.agent_id, payload.lease_token, terminal_receipt=True)
            if row["status"] not in TERMINAL and not payload.observe_only:
                row["lease_expires_at"] = _lease_until(now)
                conn.execute("UPDATE edge_model_deployments SET lease_expires_at=?,updated_at=? WHERE id=?",
                             (row["lease_expires_at"], now, row["id"]))
            _contact(conn, device, now)
            return _claim_response(conn, row, payload.lease_token, now)
        active = conn.execute("SELECT * FROM edge_model_deployments WHERE device_id=? AND status IN ('queued','running','unknown')", (device["id"],)).fetchone()
        if not active:
            _contact(conn, device, now)
            return {"task": None, "server_time": now, "lease_seconds": LEASE_SECONDS}
        if active["status"] != "queued":
            raise HTTPException(409, "设备已有未完成部署，必须使用原任务租约恢复，不能重复领取")
        status, capabilities = _capabilities(conn, device)
        release = conn.execute("SELECT * FROM edge_model_releases WHERE id=?", (active["release_id"],)).fetchone()
        if (not status or status["agent_id"] != payload.agent_id or not capabilities["model_api"]
                or json.loads(release["contract_json"])["algorithm"] not in capabilities["algorithms"]):
            raise HTTPException(409, "请先上报当前设备实例的兼容系统能力")
        _release_path(release)
        token = payload.lease_token or secrets.token_urlsafe(32)
        conn.execute("""UPDATE edge_model_deployments SET status='running',agent_id=?,credential_hash=?,lease_hash=?,
            lease_expires_at=?,updated_at=? WHERE id=? AND status='queued'""",
            (payload.agent_id, device["token_hash"], _digest(token), _lease_until(now), now, active["id"]))
        row = dict(conn.execute("SELECT * FROM edge_model_deployments WHERE id=?", (active["id"],)).fetchone())
        _contact(conn, device, now)
        return _claim_response(conn, row, token, now)


@router.post("/v1/model-tasks/{task_id}/report")
def report_task(task_id: int, payload: Report, device: dict = Depends(get_device)):
    values = payload.model_dump(exclude={"lease_token", "agent_id"})
    report_hash = _digest(_json(values))
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        device = _device_write(conn, device)
        row = _authorize_task(conn, task_id, device, payload.agent_id, payload.lease_token)
        now = now_iso()
        if payload.sequence == row["last_sequence"] and row["report_hash"] == report_hash:
            return {"ok": True, "task": _task_json(row, now), "server_time": now}
        if payload.sequence <= row["last_sequence"] or row["status"] in TERMINAL:
            raise HTTPException(409, "重复报告内容冲突或部署已结束")
        if not row["lease_expires_at"] or row["lease_expires_at"] <= now:
            raise HTTPException(409, "部署租约已过期，请先使用原租约恢复对账")
        if payload.status == "running":
            if payload.stage not in STAGES or STAGES[payload.stage] < STAGES.get(row["stage"], 0):
                raise HTTPException(409, "部署阶段不能回退")
            if not row["activate"] and STAGES[payload.stage] > STAGES["uploaded"]:
                raise HTTPException(409, "仅导入任务不能启用系统模型")
        elif payload.status == "succeeded":
            release = conn.execute("SELECT filename FROM edge_model_releases WHERE id=?", (row["release_id"],)).fetchone()
            if row["activate"]:
                current = payload.current
                if (payload.stage != "verified" or not current or current.sModel != _task_filename(row, release["filename"])
                        or current.sStatus != "running" or current.iEnable != 1 or current.bExternalHold
                        or current.iActualFPS <= 0 or current.iFPS != row["fps"]):
                    raise HTTPException(409, "需要匹配目标模型且有实际推理帧率的运行验证")
            elif payload.stage != "uploaded":
                raise HTTPException(409, "仅导入任务必须报告 uploaded，不能声称已运行")
        elif payload.stage != payload.status:
            raise HTTPException(409, "部署结果与阶段不一致")
        if payload.status == "failed" and payload.rollback_status in ("failed", "unknown"):
            raise HTTPException(409, "回滚结果未确认时必须报告 unknown，不能释放设备")
        expires = _lease_until(now)
        conn.execute("""UPDATE edge_model_deployments SET status=?,stage=?,last_sequence=?,report_hash=?,current_json=?,
            rollback_status=?,error_code=?,lease_expires_at=?,updated_at=?,finished_at=? WHERE id=?""",
            (payload.status, payload.stage, payload.sequence, report_hash, _json(values["current"]), payload.rollback_status,
             payload.error_code, expires, now, now if payload.status in TERMINAL else None, task_id))
        if payload.current:
            conn.execute("UPDATE edge_system_status SET current_json=?,reported_at=? WHERE device_id=? AND agent_id=? AND credential_hash=?",
                         (_json(values["current"]), now, device["id"], payload.agent_id, device["token_hash"]))
        _contact(conn, device, now)
        row = dict(conn.execute("SELECT * FROM edge_model_deployments WHERE id=?", (task_id,)).fetchone())
        return {"ok": True, "task": _task_json(row, now), "server_time": now}


@router.get("/v1/model-tasks/{task_id}/artifact")
def task_artifact(task_id: int, x_model_lease: str = Header(default="", max_length=128),
                  x_agent_id: str = Header(default="", max_length=128), device: dict = Depends(get_device)):
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        device = _device_write(conn, device)
        row = _authorize_task(conn, task_id, device, x_agent_id, x_model_lease)
        if row["status"] not in ("running", "unknown") or not row["lease_expires_at"] or row["lease_expires_at"] <= now_iso():
            raise HTTPException(409, "部署下载租约已失效")
        release = conn.execute("SELECT * FROM edge_model_releases WHERE id=?", (row["release_id"],)).fetchone()
        path = _release_path(release)
        # Pin under the same auth/lease transaction, before rotation/deletion.
        return pinned_file_response(path, filename=_task_filename(row, release["filename"]))
