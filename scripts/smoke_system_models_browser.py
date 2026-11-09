"""Real Chromium UI checks with isolated CVSprout and controlled system-model API fixtures.

No device or production service is contacted. This verifies browser/controller
behavior, not RKNN execution or hardware compatibility. Requires Playwright and
the project's Python environment. Pass --chromium for an installed executable.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

import httpx
from playwright.sync_api import expect, sync_playwright

from smoke_project_deployment_browser import TrainingRoutes, online_device, training_fixture

REPO = Path(__file__).resolve().parents[1]


def run(chromium, output):
    checks, errors, posted, unexpected = [], [], [], []

    def passed(label):
        checks.append(label)
        print("PASS " + label, flush=True)

    with tempfile.TemporaryDirectory(prefix="cvsprout-system-browser-") as temporary:
        root = Path(temporary)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        env = {**os.environ, "MDP_DATA_DIR": str(root / "data"), "MDP_MODELS_DIR": str(root / "models"),
               "MDP_AUTH_MODE": "disabled", "MDP_EMBEDDED_WORKER": "false", "MDP_EMBEDDED_PIPELINE_WORKER": "false",
               "MDP_TRAINING_ENABLED": "false", "MDP_ENABLE_DEMO_MODEL": "false", "MDP_WORKER_URL": "",
               "MDP_WORKER_TOKEN": "", "MDP_BOOTSTRAP_API_KEY": "", "MDP_PUBLIC_ORIGIN": "", "CUDA_VISIBLE_DEVICES": "",
               "YOLO_OFFLINE": "true", "YOLO_AUTOINSTALL": "false", "YOLO_CONFIG_DIR": str(root / "yolo")}
        with (output / "server.log").open("w") as log:
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--fd", str(listener.fileno()),
                                        "--no-access-log", "--log-level", "warning"], cwd=REPO, env=env,
                                       pass_fds=(listener.fileno(),), stdout=log, stderr=subprocess.STDOUT)
            listener.close()
            try:
                with httpx.Client(base_url=base, trust_env=False, headers={"X-MDP-Client": "browser", "X-Actor-ID": "system-browser"}) as client:
                    deadline = time.monotonic() + 40
                    while True:
                        try:
                            if client.get("/api/health").status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        if process.poll() is not None or time.monotonic() > deadline:
                            raise RuntimeError("Isolated server failed to start; inspect server.log")
                        time.sleep(.05)
                    response = client.post("/api/projects", json={"name": "系统模型 UI 验收", "task_type": "detect", "labels": ["object"]})
                    response.raise_for_status()
                    project = response.json()
                    response = client.post("/api/edge/devices", json={"project_id": project["id"], "name": "验收相机", "server_url": "http://192.168.1.20:8000"})
                    response.raise_for_status()
                    device_id = response.json()["id"]  # Never retain or print the one-time device token.
                now = datetime.now(timezone.utc).isoformat()
                training_run = training_fixture(project)
                training = TrainingRoutes([training_run])
                device = online_device(device_id, project, "验收相机")
                release = {"id": 1, "project_id": project["id"], "conversion_id": 7, "status": "ready", "filename": "cvsprout-test.rknn",
                           "size_bytes": 1048576, "sha256": "a" * 64, "contract": {"algorithm": "yolov8"}, "model_info": {"algorithm": "yolov8"},
                           "default_model_name": "yolov8-run101-conv7"}
                fixture = {"device_id": device_id, "project_id": project["id"],
                           "capabilities": {"schema_version": 1, "target_platform": "rv1126b", "model_api": True, "algorithms": ["yolov8", "yolo11"]},
                           "current": {"sModel": "existing.rknn", "iEnable": 1, "sStatus": "running", "iFPS": 30, "iActualFPS": 12, "bExternalHold": False},
                           "system_status": {"agent_id": "fixture", "reported_at": now, "age_seconds": 0, "fresh": True, "error_code": ""},
                           "conversions": [{"id": 7, "training_run_id": 101, "model_arch": "yolov8n", "compatible": True, "algorithm": "yolov8", "default_model_name": "yolov8-run101-conv7"}, {"id": 8, "training_run_id": 101, "model_arch": "yolo26n", "compatible": False, "reason": "不兼容的系统输出"}],
                           "releases": [], "deployments": [], "can_deploy": True, "blocked_reason": ""}
                fixture_error = False
                with sync_playwright() as pw:
                    launch = {"headless": True, "args": ["--no-sandbox", "--disable-gpu", "--no-zygote", "--disable-software-rasterizer", "--use-gl=disabled", "--no-proxy-server"]}
                    if chromium:
                        launch["executable_path"] = str(chromium)
                    browser = pw.chromium.launch(**launch)
                    context = browser.new_context(viewport={"width": 1440, "height": 1050})
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    page.on("pageerror", lambda error: errors.append(str(error)))

                    def intercept(route):
                        request = route.request
                        if not request.url.startswith(base + "/"):
                            unexpected.append("External request blocked")
                            route.abort()
                            return
                        if training.fulfill(route):
                            return
                        if urlsplit(request.url).path == "/api/edge/devices":
                            route.fulfill(status=200, json={"devices": [device]})
                            return
                        path = urlsplit(request.url).path.split("/api/edge/")[-1]
                        if path.endswith("/system-models"):
                            route.fulfill(status=503 if fixture_error else 200, json={"detail": "fixture unavailable"} if fixture_error else fixture)
                        elif path == "model-releases":
                            assert request.post_data_json == {"conversion_id": 7}
                            fixture["releases"] = [release]
                            route.fulfill(status=201, json=release)
                        elif "/model-deployments" in path and request.method == "POST":
                            body = request.post_data_json
                            posted.append({"path": path, "body": body})
                            if path.endswith("/cancel"):
                                fixture["deployments"][0].update(status="cancelled", stage="cancelled")
                            elif path.endswith("/resolve"):
                                assert body == {"confirmation_name": "验收相机", "acknowledged": True, "expected_updated_at": now}
                                fixture["deployments"][0].update(status="failed", stage="failed", lease_expired=False, error_code="admin_resolved_unknown")
                                fixture["can_deploy"] = True
                            else:
                                assert body["release_id"] == 1 and type(body["release_id"]) is int
                                assert "model_name" in body
                                model_name = body["model_name"] or release["default_model_name"]
                                fixture["deployments"] = [{"id": 1, "model_name": model_name, "filename": f"{model_name}-r1.rknn", "status": "queued", "stage": "queued", "activate": body["activate"], "fps": body["fps"], "updated_at": now}]
                            route.fulfill(status=200 if path.endswith(("/cancel", "/resolve")) else 202, json=fixture["deployments"][0])
                        elif request.method not in ("GET", "HEAD", "OPTIONS"):
                            unexpected.append(f"Unexpected write blocked: {request.method} {urlsplit(request.url).path}")
                            route.fulfill(status=409, json={"detail": "Browser fixture blocks non-fixture writes"})
                        else:
                            route.continue_()

                    context.route("**/*", intercept)
                    try:
                        page.goto(base + "/#/devices")
                        page.locator("[data-edge-model]").click()
                        expect(page.locator("#training-selected-heading")).to_contain_text(training_run["name"])
                        page.locator('[data-section-tab="train:conversions"]').click()
                        page.locator('[data-deploy-conversion="7"]').click()
                        page.locator("#edge-project-device-select").select_option(device_id)
                        page.locator("#modal-submit").click()
                        expect(page.locator("#edge-system-state")).to_contain_text("系统推理运行中")
                        expect(page.locator("#edge-system-state")).to_contain_text("30 / 12")
                        expect(page.locator("#edge-system-mode")).to_have_value("upload")
                        expect(page.locator("#modal-submit")).to_be_enabled()
                        expect(page.locator("#edge-system-conversion")).to_have_count(0)
                        expect(page.locator("#edge-system-release")).to_have_count(0)
                        expect(page.locator("#edge-system-build")).to_have_count(0)
                        name_input = page.locator("#edge-system-model-name")
                        expect(name_input).to_have_value("yolov8-run101-conv7")
                        name_input.fill("   ")
                        expect(page.locator("#modal-submit")).to_be_enabled()
                        expect(page.locator("#edge-system-model-name-preview")).to_contain_text("yolov8-run101-conv7")
                        page.clock.install()

                        def poll():
                            with page.expect_response(lambda response: urlsplit(response.url).path.endswith("/system-models")):
                                page.clock.fast_forward(5000)

                        expect(page.locator("#modal-submit")).to_be_enabled()
                        page.locator("#modal-submit").click()
                        expect(page.locator("#edge-system-jobs")).to_contain_text("等待设备领取")
                        expect(page.locator("#modal-submit")).to_be_disabled()
                        assert posted[-1]["body"] == {"release_id": 1, "activate": False, "fps": 30, "model_name": ""}
                        expect(name_input).to_have_value("   ")
                        expect(page.locator("#edge-system-progress")).to_contain_text("yolov8-run101-conv7-r1.rknn")
                        assert release["filename"] not in page.locator("#edge-system-progress").inner_text()
                        if not page.locator("#edge-system-history").evaluate("node=>node.open"):
                            page.locator("#edge-system-history summary").click()
                        page.locator("[data-edge-cancel-model]").click()
                        expect(page.locator("#edge-system-jobs")).to_contain_text("已取消")
                        passed("Blank model name uses the server's algorithm default, one-click preparation preserves input, and queued upload can be cancelled")

                        name_input.fill("  Assembly-A_v2  ")
                        page.locator("#edge-system-mode").select_option("activate")
                        expect(page.locator("#modal-submit")).to_be_disabled()
                        page.locator("#edge-system-fps-field summary").click()
                        page.locator("#edge-system-fps").fill("17")
                        page.locator("#edge-system-confirm").check()
                        expect(page.locator("#modal-submit")).to_be_enabled()
                        page.locator("#edge-system-details summary").click()
                        page.locator("#edge-system-fps").focus()
                        poll()
                        expect(page.locator("#edge-system-fps")).to_be_focused()
                        expect(page.locator("#edge-system-fps")).to_have_value("17")
                        assert page.locator("#edge-system-details").evaluate("node=>node.open")
                        expect(page.locator("#edge-system-confirm")).to_be_checked()
                        expect(name_input).to_have_value("  Assembly-A_v2  ")
                        expect(page.locator("#edge-system-model-name-preview")).to_contain_text("Assembly-A_v2-r1.rknn")
                        passed("Five-second polling preserves custom name, FPS, focus, expanded details and activation confirmation")

                        page.locator("#modal-submit").click()
                        expect(page.locator("#edge-system-jobs")).to_contain_text("等待设备领取")
                        assert posted[-1]["body"] == {"release_id": 1, "activate": True, "fps": 17, "model_name": "Assembly-A_v2"}
                        expect(page.locator("#edge-system-progress")).to_contain_text("Assembly-A_v2-r1.rknn")
                        expect(page.locator("#edge-system-jobs")).to_contain_text("Assembly-A_v2-r1.rknn")
                        assert release["filename"] == "cvsprout-test.rknn"
                        fixture["deployments"][0].update(status="running", stage="activated")
                        poll()
                        expect(page.locator("#edge-system-jobs")).to_contain_text("等待运行验证")
                        assert "已验证系统推理运行" not in page.locator("#edge-system-jobs").inner_text()
                        fixture["deployments"][0].update(status="succeeded", stage="verified")
                        poll()
                        expect(page.locator("#edge-system-jobs")).to_contain_text("已验证系统推理运行")
                        passed("Activation explicitly confirms replacement, carries FPS and requires verified terminal stage")

                        fixture_error = True
                        poll()
                        expect(page.locator("#edge-system-feedback")).to_be_visible()
                        expect(page.locator("#edge-system-progress")).to_contain_text("进度更新中断")
                        expect(page.locator("#edge-system-state")).to_contain_text("状态已过期")
                        assert "30 / 12" not in page.locator("#edge-system-state").inner_text()
                        expect(page.locator("#modal-submit")).to_be_disabled()
                        fixture_error = False
                        fixture["current"]["bExternalHold"] = True
                        poll()
                        expect(page.locator("#edge-system-state")).to_contain_text("等待 NPU")
                        passed("Refresh failures clear running evidence/FPS and block submission; NPU contention is explicit after recovery")

                        fixture["deployments"][0].update(status="unknown", stage="unknown", lease_expired=True)
                        fixture["can_deploy"] = False
                        poll()
                        page.locator("[data-edge-resolve-model]").click()
                        expect(page.locator("#edge-system-resolve-submit")).to_be_disabled()
                        page.locator("#edge-system-resolve-name").fill("wrong")
                        page.locator("#edge-system-resolve-ack").check()
                        expect(page.locator("#edge-system-resolve-submit")).to_be_disabled()
                        page.locator("#edge-system-resolve-name").fill("验收相机")
                        page.locator("#edge-system-resolve-submit").click()
                        expect(page.locator("#edge-system-resolve")).to_be_hidden()
                        expect(page.locator("#edge-system-jobs")).to_contain_text("部署失败")
                        passed("Expired unknown recovery requires exact device name, acknowledgement and optimistic task version")

                        fixture["current"]["bExternalHold"] = False
                        fixture["system_status"]["fresh"] = False
                        poll()
                        expect(page.locator("#edge-system-state")).to_contain_text("状态已过期")
                        page.locator("#edge-system-mode").select_option("upload")
                        expect(page.locator("#modal-submit")).to_be_enabled()
                        passed("Online device retains known capabilities for deployment preparation while stale system reports do not claim current inference")

                        for width in (1440, 390):
                            page.set_viewport_size({"width": width, "height": 1050 if width == 1440 else 844})
                            for theme in ("light", "dark"):
                                page.evaluate("theme=>window.CVSproutTheme.setPreference(theme)", theme)
                                assert page.locator(".modal-body").evaluate("node=>node.scrollWidth<=node.clientWidth+1"), "Modal overflows horizontally"
                                page.screenshot(path=str(output / f"system-models-{width}-{theme}.png"), full_page=True)
                        passed("Desktop and 390px mobile system deployment dialog fits light and dark themes")

                        page.set_viewport_size({"width": 1440, "height": 1050})
                        device["connection"] = {"state": "offline", "age_seconds": 180, "timeout_seconds": 90}
                        fixture["deployments"][0].update(status="queued", stage="queued", lease_expired=False)
                        page.goto(base + "/#/devices")
                        page.locator(".edge-more summary").click()
                        page.locator("[data-edge-model-history]").click()
                        expect(page.locator("#edge-system-jobs")).to_contain_text("等待设备领取")
                        expect(page.locator("#modal-submit")).to_be_hidden()
                        expect(page.locator("#edge-system-build")).to_have_count(0)
                        expect(name_input).to_have_count(0)
                        if not page.locator("#edge-system-history").evaluate("node=>node.open"):
                            page.locator("#edge-system-history summary").click()
                        page.locator("[data-edge-cancel-model]").click()
                        expect(page.locator("#edge-system-jobs")).to_contain_text("已取消")
                        fixture["deployments"][0].update(status="unknown", stage="unknown", lease_expired=True)
                        fixture["can_deploy"] = False
                        poll()
                        page.locator("[data-edge-resolve-model]").click()
                        page.locator("#edge-system-resolve-name").fill("验收相机")
                        page.locator("#edge-system-resolve-ack").check()
                        page.locator("#edge-system-resolve-submit").click()
                        expect(page.locator("#edge-system-jobs")).to_contain_text("部署失败")
                        expect(page.locator("#modal-submit")).to_be_hidden()
                        page.screenshot(path=str(output / "offline-device-history.png"), full_page=True)
                        passed("Offline device history hides new deployment controls while preserving queued cancellation and expired-unknown resolution")
                        assert not errors, errors
                        assert not unexpected, unexpected
                    except Exception:
                        page.screenshot(path=str(output / "failure.png"), full_page=True)
                        print("Page errors: " + json.dumps(errors), flush=True)
                        raise
                    finally:
                        context.close()
                        browser.close()
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    (output / "failure.png").unlink(missing_ok=True)
    return {"passed": True, "checks": checks, "page_errors": errors, "unexpected_requests": unexpected,
            "scope": "Real Chromium and isolated app; controlled system-model API fixtures; no hardware inference claim"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output = (args.output_dir or Path(tempfile.mkdtemp(prefix="cvsprout-system-browser-proof-"))).resolve()
    output.mkdir(parents=True, exist_ok=True)
    print("Artifacts: " + str(output), flush=True)
    result = run(args.chromium, output)
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
