"""reCamera release creation and durable SSH/App Center deployment jobs."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.background import BackgroundTask

from .auth import Actor, get_actor, require_admin
from .config import settings
from .database import all_rows, audit, db, now_iso, one
from .file_responses import pinned_file_response
from .recamera_packages import ROOT, build_package, inspect_archive, model_contract, sha256, stage_inference

router = APIRouter(prefix="/api/edge", tags=["edge-deployment"])


def device_row(device_id: str) -> dict:
    row = one("SELECT d.*,p.archived_at FROM edge_devices d JOIN projects p ON p.id=d.project_id WHERE d.id=?", (device_id,))
    if not row:
        raise HTTPException(404, "设备不存在")
    return row


def release_json(row: dict) -> dict:
    row = dict(row)
    row["model_contract"] = json.loads(row.pop("contract_json"))
    row.pop("archive_name", None)
    row["download_url"] = f"/api/edge/releases/{row['id']}/download" if row["status"] == "ready" else None
    return row


def deployment_json(row: dict) -> dict:
    row = dict(row)
    row["report"] = json.loads(row.pop("report_json"))
    row.pop("lease_expires_at", None)
    return row


def ssh_readiness() -> dict:
    return {"ssh_available": bool(shutil.which("ssh")),
            "known_hosts_configured": bool(settings.recamera_known_hosts and Path(settings.recamera_known_hosts).is_file()),
            "key_configured": bool(settings.recamera_ssh_key and Path(settings.recamera_ssh_key).is_file())}


@router.get("/devices/{device_id}/deployment-options")
def options(device_id: str, actor: Actor = Depends(get_actor)):
    require_admin(actor)
    device = device_row(device_id)
    candidates = []
    for row in all_rows("""SELECT c.* FROM model_conversions c JOIN training_runs r ON r.id=c.training_run_id
        WHERE r.project_id=? AND c.status='completed' ORDER BY c.id DESC""", (device["project_id"],)):
        run = one("SELECT * FROM training_runs WHERE id=?", (row["training_run_id"],))
        if run is None:
            # A concurrent project deletion can remove a previously listed run.
            continue
        candidate = {"id": row["id"], "training_run_id": run["id"], "project_id": device["project_id"],
                     "model_arch": json.loads(run.get("artifact_metadata_json") or "{}").get("model_arch", ""),
                     "compatible": False, "reason": "", "labels": []}
        try:
            contract = model_contract(row, run)
            from .training import _validated_artifact
            _validated_artifact(row["artifact_path"], "转换", json.loads(row["artifact_metadata_json"]))
            candidate.update(compatible=True, image_size=contract["input_size"], precision=contract["precision"], labels=contract["classes"])
        except (ValueError, HTTPException) as exc:
            candidate["reason"] = str(exc.detail if isinstance(exc, HTTPException) else exc)
        candidates.append(candidate)
    return {"conversions": candidates, "deployment": ssh_readiness(),
            "capture_app": {"download_url": "/api/edge/apps/capture/download"}}


class CreateRelease(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversion_id: int = Field(gt=0, le=2**63 - 1, strict=True)


@router.post("/releases", status_code=201)
def create_release(payload: CreateRelease, actor: Actor = Depends(get_actor)):
    require_admin(actor)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conversion_row = conn.execute("SELECT * FROM model_conversions WHERE id=?", (payload.conversion_id,)).fetchone()
        if not conversion_row:
            raise HTTPException(404, "转换记录不存在")
        conversion = dict(conversion_row)
        run = dict(conn.execute("SELECT * FROM training_runs WHERE id=?", (conversion["training_run_id"],)).fetchone())
        project = conn.execute("SELECT * FROM projects WHERE id=?", (run["project_id"],)).fetchone()
        if project["archived_at"]:
            raise HTTPException(409, "项目已归档")
        try:
            contract = model_contract(conversion, run)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        metadata = json.loads(conversion["artifact_metadata_json"])
        if not re.fullmatch(r"[a-f0-9]{64}", str(metadata.get("sha256", ""))) or not metadata.get("size_bytes"):
            raise HTTPException(409, "转换文件缺少完整性记录，请重新转换")
        # Reuse an immutable verified release on double-click/retry.
        previous = conn.execute("SELECT * FROM edge_releases WHERE conversion_id=? AND status='ready' ORDER BY id DESC LIMIT 1",
                                (payload.conversion_id,)).fetchone()
        if previous:
            try:
                _release_path(dict(previous))
                return release_json(dict(previous))
            except HTTPException:
                conn.execute("UPDATE edge_releases SET status='failed',error='安装包丢失或校验失败' WHERE id=?", (previous["id"],))
        if conn.execute("SELECT 1 FROM edge_releases WHERE project_id=? AND status='building'", (run["project_id"],)).fetchone():
            raise HTTPException(409, "该项目正在生成设备应用包，请稍后刷新")
        cursor = conn.execute("""INSERT INTO edge_releases(project_id,conversion_id,status,created_at)
            VALUES (?,?,'building',?)""", (run["project_id"], conversion["id"], now_iso()))
        release_id = cursor.lastrowid
        # App Center compares release.sequence across versions of the same app.
        # Multiple models can finish in one second, so a timestamp alone is not
        # monotonic. The surrounding write transaction also covers other web
        # replicas and projects producing this shared inference app identity.
        previous_sequence = conn.execute("SELECT COALESCE(MAX(sequence),0) FROM edge_releases").fetchone()[0]
        version, sequence = f"1.0.{release_id}", max(int(time.time()), previous_sequence + 1)
        contract["release_id"] = str(release_id)
        conn.execute("UPDATE edge_releases SET version=?,sequence=?,contract_json=? WHERE id=?",
                     (version, sequence, json.dumps(contract, ensure_ascii=False), release_id))
    output = settings.training_dir / f"edge-release-{release_id}"
    try:
        from .training import _validated_artifact
        model = _validated_artifact(conversion["artifact_path"], "转换", metadata)
        with tempfile.TemporaryDirectory(prefix="cvsprout-rknn-") as temporary:
            source = Path(temporary) / "app"
            stage_inference(source, model, contract, version, sequence)
            archive = build_package(source, output)
        digest, size = sha256(archive), archive.stat().st_size
        with db() as conn:
            conn.execute("""UPDATE edge_releases SET status='ready',archive_name=?,sha256=?,size_bytes=?,finished_at=? WHERE id=?""",
                         (archive.name, digest, size, now_iso(), release_id))
        audit(actor.id, "create", "edge_release", release_id, run["project_id"])
    except Exception as exc:
        shutil.rmtree(output, ignore_errors=True)
        message = str(exc.detail if isinstance(exc, HTTPException) else exc)[:2000]
        with db() as conn:
            conn.execute("UPDATE edge_releases SET status='failed',error=?,finished_at=? WHERE id=?", (message, now_iso(), release_id))
        raise HTTPException(409, "设备应用包生成失败：" + message) from exc
    return release_json(one("SELECT * FROM edge_releases WHERE id=?", (release_id,)))


def _release_path(row: dict) -> Path:
    name = row["archive_name"]
    path = settings.training_dir / f"edge-release-{row['id']}" / name
    if (row["status"] != "ready" or not name or Path(name).name != name or path.is_symlink()
            or not path.is_file() or path.resolve().parent != (settings.training_dir.resolve() / f"edge-release-{row['id']}")
            or path.stat().st_size != row["size_bytes"] or sha256(path) != row["sha256"]):
        raise HTTPException(409, "设备应用包已丢失或校验失败，请重新生成")
    return path


@router.get("/releases")
def releases(device_id: str = Query(...), actor: Actor = Depends(get_actor)):
    require_admin(actor)
    device = device_row(device_id)
    return {"releases": [release_json(row) for row in all_rows(
        "SELECT * FROM edge_releases WHERE project_id=? ORDER BY id DESC LIMIT 100", (device["project_id"],))]}


@router.get("/releases/{release_id}/download")
def download_release(release_id: int, actor: Actor = Depends(get_actor)):
    require_admin(actor)
    row = one("SELECT * FROM edge_releases WHERE id=?", (release_id,))
    if not row:
        raise HTTPException(404, "发布记录不存在")
    path = _release_path(row)
    return pinned_file_response(path, filename=path.name, media_type="application/gzip")


@router.get("/apps/capture/download")
def capture_package(actor: Actor = Depends(get_actor)):
    require_admin(actor)
    temporary = Path(tempfile.mkdtemp(prefix="cvsprout-capture-package-"))
    try:
        archive = build_package(ROOT / "templates/cvsprout-capture", temporary)
        return FileResponse(archive, filename=archive.name, media_type="application/gzip",
                            background=BackgroundTask(shutil.rmtree, temporary, ignore_errors=True))
    except Exception as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        raise HTTPException(409, "采集应用包生成失败：" + str(exc)[:1000]) from exc


class DeployRelease(BaseModel):
    model_config = ConfigDict(extra="forbid")
    release_id: int = Field(gt=0, le=2**63 - 1, strict=True)
    host: str = Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9][A-Za-z0-9.:-]*$")
    port: int = Field(default=22, ge=1, le=65535, strict=True)
    username: str = Field(default="root", max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_.-]*$")
    replace: bool = Field(default=False, strict=True)
    start: bool = Field(default=True, strict=True)

    @field_validator("host")
    @classmethod
    def normalize_host(cls, value):
        return value.lower().rstrip(".")


@router.post("/devices/{device_id}/deployments", status_code=202)
def deploy_release(device_id: str, payload: DeployRelease, actor: Actor = Depends(get_actor)):
    require_admin(actor)
    readiness = ssh_readiness()
    if not readiness["ssh_available"] or not readiness["known_hosts_configured"]:
        raise HTTPException(409, "请先在服务端配置 OpenSSH 和 MDP_RECAMERA_KNOWN_HOSTS，再部署设备")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        device = device_row(device_id)
        if not device["enabled"] or device["archived_at"]:
            raise HTTPException(409, "设备已撤销或项目已归档")
        release = conn.execute("SELECT * FROM edge_releases WHERE id=?", (payload.release_id,)).fetchone()
        if not release or release["project_id"] != device["project_id"]:
            raise HTTPException(404, "当前设备项目没有此模型发布记录")
        _release_path(dict(release))
        try:
            cursor = conn.execute("""INSERT INTO edge_deployments(device_id,release_id,project_id,host,port,username,replace_app,start_app,status,created_at)
                VALUES (?,?,?,?,?,?,?,?,'queued',?)""", (device_id, payload.release_id, device["project_id"], payload.host,
                payload.port, payload.username, int(payload.replace), int(payload.start), now_iso()))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "该设备已有排队或进行中的部署，请等待结束") from exc
        job_id = cursor.lastrowid
    audit(actor.id, "deploy", "edge_release", payload.release_id, device["project_id"])
    return deployment_json(one("SELECT * FROM edge_deployments WHERE id=?", (job_id,)))


@router.get("/devices/{device_id}/deployments")
def deployments(device_id: str, actor: Actor = Depends(get_actor)):
    require_admin(actor)
    device_row(device_id)
    return {"deployments": [deployment_json(row) for row in all_rows(
        "SELECT * FROM edge_deployments WHERE device_id=? ORDER BY id DESC LIMIT 50", (device_id,))]}


def execute_deployment(job: dict, stop_event: threading.Event | None = None) -> dict:
    release = one("SELECT * FROM edge_releases WHERE id=?", (job["release_id"],))
    archive = _release_path(release)
    identity = inspect_archive(archive)
    config = {**identity, "replace": bool(job["replace_app"]), "start": bool(job["start_app"])}
    known_hosts = Path(settings.recamera_known_hosts).resolve(strict=True)
    source = Path(__file__).with_name("recamera_remote.py").read_text()
    command = ["ssh", "-T", "-p", str(job["port"]), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
               "-o", "LogLevel=ERROR", "-o", "UserKnownHostsFile=" + str(known_hosts)]
    if settings.recamera_ssh_key:
        command += ["-o", "IdentitiesOnly=yes", "-i", str(Path(settings.recamera_ssh_key).resolve(strict=True))]
    command += ["-l", job["username"], "--", job["host"], "python3 -u -c " + shlex.quote(source)]
    with tempfile.TemporaryFile() as transfer:
        transfer.write(json.dumps(config).encode() + b"\n")
        with archive.open("rb") as stream:
            shutil.copyfileobj(stream, transfer)
        transfer.seek(0)
        process = subprocess.Popen(command, stdin=transfer, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 360
        try:
            try:
                while True:
                    try:
                        stdout, _ = process.communicate(timeout=1)
                        break
                    except subprocess.TimeoutExpired:
                        if time.monotonic() >= deadline or (stop_event and stop_event.is_set()):
                            return {"status": "unknown", "stage": "connection_lost", "error": "SSH 等待超时或服务停止；设备可能已接受操作，请先核查应用中心后再部署"}
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate()
        except Exception:
            # Once SSH was launched, a local pipe/read/cleanup failure cannot
            # establish whether the remote install has already been accepted.
            return {"status": "unknown", "stage": "connection_lost", "error": "SSH 通信意外中断；设备可能已接受操作，请先核查应用中心后再部署"}
    lines = [line.removeprefix("CVSPROUT_DEPLOY=") for line in stdout.decode(errors="replace").splitlines()
             if line.startswith("CVSPROUT_DEPLOY=")]
    if process.returncode or len(lines) != 1:
        return {"status": "unknown", "stage": "connection_lost", "error": "SSH 未返回确定结果，请检查连接、主机指纹及设备应用中心后再部署"}
    try:
        report = json.loads(lines[0])
        if not isinstance(report, dict) or report.get("status") not in {"succeeded", "failed", "unknown"}:
            raise ValueError("invalid report")
    except ValueError:
        return {"status": "unknown", "stage": "connection_lost", "error": "设备返回状态不完整，请先核查设备应用中心后再部署"}
    return report


class EdgeDeploymentWorker:
    def __init__(self):
        self.stopping = False
        self.stop_event = threading.Event()
        self.wake = asyncio.Event()

    def stop(self):
        self.stopping = True
        self.stop_event.set()
        self.wake.set()

    def claim(self):
        now = now_iso()
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Lost SSH replies are NOT a license to replay an install/start.
            conn.execute("""UPDATE edge_deployments SET status='unknown',stage='interrupted',error=?,finished_at=?
                WHERE status='running' AND lease_expires_at<?""", ("部署连接中断，请核查设备状态后再操作", now, now))
            conn.execute("""UPDATE edge_releases SET status='failed',error='服务重启或打包超时，请重新生成',finished_at=?
                WHERE status='building' AND created_at<?""", (now, (datetime.now(UTC) - timedelta(minutes=10)).isoformat()))
            row = conn.execute("SELECT * FROM edge_deployments WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if row:
                conn.execute("UPDATE edge_deployments SET status='running',stage='connecting',lease_expires_at=? WHERE id=?",
                             ((datetime.now(UTC) + timedelta(seconds=420)).isoformat(), row["id"]))
                return dict(row)
        return None

    def process(self, job):
        try:
            device = device_row(job["device_id"])
            if not device["enabled"] or device["archived_at"]:
                raise ValueError("设备已撤销或项目已归档，部署已取消")
            report = execute_deployment(job, self.stop_event)
        except Exception as exc:
            report = {"status": "failed", "stage": "preparation", "error": str(exc.detail if isinstance(exc, HTTPException) else exc)[:1000]}
        with db() as conn:
            conn.execute("""UPDATE edge_deployments SET status=?,stage=?,error=?,report_json=?,finished_at=?,lease_expires_at=NULL
                WHERE id=? AND status='running'""", (report["status"], report.get("stage", ""), report.get("error", ""),
                json.dumps(report, ensure_ascii=False), now_iso(), job["id"]))

    async def run_forever(self):
        while not self.stopping:
            job = await asyncio.to_thread(self.claim)
            if job:
                await asyncio.to_thread(self.process, job)
            else:
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=1)
                except TimeoutError:
                    pass
