"""Fixed stdlib worker streamed over SSH; all mutations use the public App Center API.

Only cvsprout-inference can be installed. This worker never extracts a package,
changes firmware, weakens signature policy, or writes the shared Python runtime.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import ssl
import sys
import tempfile
import time
import uuid
from urllib.parse import urlsplit

PREFIX = "/api/app-center/v1"
CERT = "/userdata/config/system/ssl/server.crt"
LIMIT = 200 * 1024 * 1024


class Uncertain(RuntimeError):
    """A submitted operation needs inspection, never automatic resubmission."""


class AppCenter:
    def __init__(self):
        self.secure = False
        self.context = None
        self.certificate = None
        connection = http.client.HTTPConnection("127.0.0.1", timeout=10)
        try:
            connection.request("GET", PREFIX + "/policy", headers={"Origin": "http://127.0.0.1"})
            reply = connection.getresponse()
            if reply.status in {301, 302, 307, 308}:
                url = urlsplit(reply.getheader("Location", ""))
                if (url.scheme != "https" or url.hostname != "127.0.0.1" or url.port not in {None, 443}
                        or url.path != PREFIX + "/policy" or url.query or url.fragment or url.username):
                    raise RuntimeError("设备返回了不受支持的管理地址跳转")
                self.context = ssl.create_default_context(cafile=CERT)
                self.context.check_hostname = False
                self.certificate = ssl.PEM_cert_to_DER_cert(Path(CERT).read_text())
                self.secure = True
            elif reply.status != 200:
                raise RuntimeError(f"设备管理接口未授权或不可用：HTTP {reply.status}")
        finally:
            connection.close()

    def request(self, method, path, body=None, package=None):
        origin = "https://127.0.0.1" if self.secure else "http://127.0.0.1"
        if self.secure:
            connection = http.client.HTTPSConnection("127.0.0.1", context=self.context, timeout=20)
            connection.connect()
            if connection.sock.getpeercert(binary_form=True) != self.certificate:
                connection.close()
                raise RuntimeError("设备 HTTPS 证书与 SSH 验证的设备证书不一致")
        else:
            connection = http.client.HTTPConnection("127.0.0.1", timeout=20)
        try:
            headers = {"Origin": origin, "Accept": "application/json"}
            if package:
                boundary = uuid.uuid4().hex
                head = (f'--{boundary}\r\nContent-Disposition: form-data; name="package"; '
                        'filename="app.tar.gz"\r\nContent-Type: application/gzip\r\n\r\n').encode()
                tail = f"\r\n--{boundary}--\r\n".encode()
                connection.putrequest(method, PREFIX + path)
                for key, value in {**headers, "Content-Type": f"multipart/form-data; boundary={boundary}",
                                   "Content-Length": str(len(head) + package.stat().st_size + len(tail))}.items():
                    connection.putheader(key, value)
                connection.endheaders()
                connection.send(head)
                with package.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        connection.send(chunk)
                connection.send(tail)
            else:
                headers["Content-Type"] = "application/json"
                connection.request(method, PREFIX + path, json.dumps(body).encode() if body is not None else None, headers)
            reply = connection.getresponse()
            data = reply.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                raise RuntimeError("设备返回数据超过限制")
            if not 200 <= reply.status < 300:
                raise RuntimeError(f"设备接口 {method} {path} 返回 HTTP {reply.status}")
            return json.loads(data) if data else {}
        finally:
            connection.close()


def find_app(api):
    return next((a for a in api.request("GET", "/apps")["apps"] if a.get("id") == "cvsprout-inference"), None)


def wait_operation(api, submitted, kind, report, timeout=100):
    operation = submitted.get("operation") or {}
    operation_id = operation.get("id")
    if (not operation_id or operation.get("type") != kind or operation.get("app_id") != "cvsprout-inference"):
        raise Uncertain("设备未返回匹配的操作编号，请在设备应用中心核查")
    report["operation_id"] = operation_id
    deadline = time.monotonic() + timeout
    while True:
        if operation.get("status") == "succeeded":
            return
        if operation.get("status") == "failed":
            report["mutation_pending"] = False
            raise RuntimeError("设备操作失败：" + str(operation.get("error") or kind)[:500])
        if time.monotonic() >= deadline:
            raise Uncertain("设备操作仍未结束，请核查应用中心操作记录后再部署")
        time.sleep(0.5)
        operation = next((op for op in api.request("GET", "/operations")["operations"] if op.get("id") == operation_id), {})
        if operation.get("type") != kind or operation.get("app_id") != "cvsprout-inference":
            raise Uncertain("设备操作记录已消失或身份不匹配")


def install(api, package, config, report):
    manifest = config["manifest"]
    if manifest["id"] != "cvsprout-inference":
        raise RuntimeError("不支持的应用")
    policy = api.request("GET", "/policy")
    if package.stat().st_size > policy.get("upload", {}).get("max_package_bytes", LIMIT):
        raise RuntimeError("安装包超过设备大小限制")
    existing = find_app(api)
    if existing and not config["replace"]:
        raise RuntimeError("设备已有推理应用，请明确选择替换后重试")
    report["previous_version"] = (existing or {}).get("version")
    report["stage"] = "preflight"
    receipt = api.request("POST", "/uploads", package=package)
    preflight = receipt.get("preflight") or {}
    upload_id = receipt.get("upload_id")
    submitted = False
    try:
        if preflight.get("manifest") != manifest or preflight.get("release_id") != config["release_id"]:
            raise RuntimeError("设备预检安装包与本地校验结果不一致")
        checks = preflight.get("checks")
        if not checks or any(c.get("passed") is not True for c in checks):
            raise RuntimeError("设备预检未通过，请检查签名策略、权限、空间和依赖")
        context = preflight.get("install_context") or {}
        if context.get("mode") not in {"new", "upgrade", "reinstall"}:
            raise RuntimeError("不支持此设备安装模式")
        confirmations = context.get("confirmation_required") or []
        if any(c not in {"running_upgrade_confirmed", "force_reinstall_confirmed"} for c in confirmations):
            raise RuntimeError("设备要求额外的安装确认")
        if (context["mode"] != "new" or confirmations) and not config["replace"]:
            raise RuntimeError("应用已存在，尚未授权替换")
        report["stage"] = "installing"
        submitted = True
        report["mutation_pending"] = True
        body = {"upload_id": upload_id, "permissions_confirmed": True, "permissions": manifest["permissions"],
                **{c: True for c in confirmations}}
        wait_operation(api, api.request("POST", "/apps", body), "install", report)
        report["mutation_pending"] = False
    finally:
        if upload_id and not submitted:
            try:
                api.request("DELETE", "/uploads/" + upload_id)
            except Exception:
                pass
    installed_root = Path("/userdata/local/apps/cvsprout-inference")
    if (json.loads((installed_root / "manifest.json").read_text()) != manifest
            or json.loads((installed_root / "release.lock.json").read_text()).get("release_id") != config["release_id"]):
        raise RuntimeError("安装后的应用版本或完整性不匹配")
    report["installed"] = True
    if config["start"]:
        report["stage"] = "starting"
        report["mutation_pending"] = True
        wait_operation(api, api.request("POST", "/apps/cvsprout-inference/start", {}), "start", report)
        report["mutation_pending"] = False
        deadline = time.monotonic() + 120
        while True:
            try:
                current = find_app(api) or {}
            except Exception as exc:
                raise Uncertain("启动操作已完成，但设备运行状态暂不可读，请核查应用中心后再部署") from exc
            state = (current.get("runtime") or {}).get("observed_state")
            instance = current.get("instance") or {}
            if (current.get("version") == manifest["version"] and current.get("running") and state in {"ready", "running"}
                    and instance.get("id") and type(instance.get("generation")) is int):
                old_instance = (existing or {}).get("instance") or {}
                if existing and old_instance.get("id") == instance["id"] and old_instance.get("generation") == instance["generation"]:
                    raise RuntimeError("设备仍在运行升级前的实例")
                report.update(instance_id=instance["id"], generation=instance["generation"], running=True)
                break
            if state in {"failed", "crash_loop", "degraded"}:
                raise RuntimeError("推理应用未正常启动：" + str(state))
            if time.monotonic() >= deadline:
                raise Uncertain("安装完成但未观察到应用就绪，请检查设备资源和日志")
            time.sleep(0.5)
    report.update(status="succeeded", stage="running" if config["start"] else "installed",
                  results="not_run", task_quality="not_run")


def main():
    report = {"status": "failed", "stage": "uploading"}
    directory = None
    try:
        line = sys.stdin.buffer.readline(1024 * 1024 + 1)
        if len(line) > 1024 * 1024 or not line.endswith(b"\n"):
            raise RuntimeError("无效的传输头")
        config = json.loads(line)
        size = config["size"]
        if type(size) is not int or not 0 < size <= LIMIT:
            raise RuntimeError("无效的安装包大小")
        root = Path("/userdata/appstage")
        root.mkdir(exist_ok=True, parents=True)
        if shutil.disk_usage(root).free < size + 16 * 1024 * 1024:
            raise RuntimeError("设备暂存空间不足")
        directory = Path(tempfile.mkdtemp(prefix="cvsprout-", dir=root))
        package = directory / "app.tar.gz"
        digest = hashlib.sha256()
        with package.open("xb") as stream:
            os.chmod(package, 0o600)
            while size:
                chunk = sys.stdin.buffer.read(min(size, 1024 * 1024))
                if not chunk:
                    raise RuntimeError("安装包未传输完整")
                stream.write(chunk)
                digest.update(chunk)
                size -= len(chunk)
        if digest.hexdigest() != config["sha256"]:
            raise RuntimeError("设备安装包 SHA-256 校验失败")
        install(AppCenter(), package, config, report)
    except Exception as exc:
        report["status"] = "unknown" if isinstance(exc, Uncertain) or report.get("mutation_pending") else "failed"
        report["error"] = str(exc)[:1000]
    finally:
        if directory:
            shutil.rmtree(directory, ignore_errors=True)
        print("CVSPROUT_DEPLOY=" + json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
