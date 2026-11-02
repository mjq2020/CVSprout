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

import httpx
from playwright.sync_api import expect, sync_playwright

REPO = Path(__file__).resolve().parents[1]


def run(chromium, output):
    checks, errors, posted = [], [], []

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
                release = {"id": 1, "project_id": project["id"], "conversion_id": 7, "status": "ready", "filename": "cvsprout-test.rknn",
                           "size_bytes": 1048576, "sha256": "a" * 64, "contract": {"algorithm": "yolov8"}, "model_info": {"algorithm": "yolov8"}}
                fixture = {"device_id": device_id, "project_id": project["id"],
                           "capabilities": {"schema_version": 1, "target_platform": "rv1126b", "model_api": True, "algorithms": ["yolov8", "yolo11"]},
                           "current": {"sModel": "existing.rknn", "iEnable": 1, "sStatus": "running", "iFPS": 30, "iActualFPS": 12, "bExternalHold": False},
                           "system_status": {"agent_id": "fixture", "reported_at": now, "age_seconds": 0, "fresh": True, "error_code": ""},
                           "conversions": [{"id": 7, "model_arch": "yolov8n", "compatible": True}, {"id": 8, "model_arch": "yolo26n", "compatible": False, "reason": "不兼容的系统输出"}],
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
                        path = request.url.split("/api/edge/")[-1]
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
                                assert body == {"confirmation_name": "验收相机", "acknowledged": True, "expected_updated_at": "fixture-version"}
                                fixture["deployments"][0].update(status="failed", stage="failed", lease_expired=False, error_code="admin_resolved_unknown")
                                fixture["can_deploy"] = True
                            else:
                                assert body["release_id"] == 1 and type(body["release_id"]) is int
                                fixture["deployments"] = [{"id": 1, "filename": release["filename"], "status": "queued", "stage": "queued", "activate": body["activate"], "fps": body["fps"], "updated_at": "fixture-version"}]
                            route.fulfill(status=200 if path.endswith(("/cancel", "/resolve")) else 202, json=fixture["deployments"][0])
                        else:
                            route.continue_()

                    page.route("**/api/edge/**", intercept)
                    try:
                        page.goto(base + "/#/devices")
                        page.locator("[data-edge-model]").click()
                        expect(page.locator("#edge-system-state")).to_contain_text("系统推理运行中")
                        expect(page.locator("#edge-system-state")).to_contain_text("30 / 12")
                        expect(page.locator("#edge-system-mode")).to_have_value("upload")
                        expect(page.locator("#modal-submit")).to_be_disabled()
                        assert page.locator('#edge-system-conversion option[value="8"]').is_disabled()
                        page.clock.install()

                        def poll():
                            with page.expect_response(lambda response: response.url.endswith("/system-models")):
                                page.clock.fast_forward(5000)

                        page.locator("#edge-system-build").click()
                        expect(page.locator("#edge-system-release")).to_have_value("1")
                        expect(page.locator("#modal-submit")).to_be_enabled()
                        page.locator("#modal-submit").click()
                        expect(page.locator("#edge-system-jobs")).to_contain_text("等待设备领取")
                        expect(page.locator("#modal-submit")).to_be_disabled()
                        assert posted[-1]["body"] == {"release_id": 1, "activate": False, "fps": 30}
                        page.locator("[data-edge-cancel-model]").click()
                        expect(page.locator("#edge-system-jobs")).to_contain_text("已取消")
                        passed("Publish integer-ID release, default upload-only task, offline queue and cancel through real controls")

                        page.locator("#edge-system-mode").select_option("activate")
                        expect(page.locator("#modal-submit")).to_be_disabled()
                        page.locator("#edge-system-fps").fill("17")
                        page.locator("#edge-system-confirm").check()
                        expect(page.locator("#modal-submit")).to_be_enabled()
                        page.locator("summary").filter(has_text="查看转换兼容性").click()
                        page.locator("#edge-system-fps").focus()
                        poll()
                        expect(page.locator("#edge-system-fps")).to_be_focused()
                        expect(page.locator("#edge-system-fps")).to_have_value("17")
                        assert page.locator("details").filter(has=page.locator("#edge-system-compatibility")).evaluate("node=>node.open")
                        expect(page.locator("#edge-system-confirm")).to_be_checked()
                        passed("Five-second polling preserves FPS edits, focus, expanded details and activation confirmation")

                        page.locator("#modal-submit").click()
                        expect(page.locator("#edge-system-jobs")).to_contain_text("等待设备领取")
                        assert posted[-1]["body"] == {"release_id": 1, "activate": True, "fps": 17}
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
                        passed("Previously reported capabilities allow safe offline queuing without claiming current inference")

                        for width in (1440, 390):
                            page.set_viewport_size({"width": width, "height": 1050 if width == 1440 else 844})
                            for theme in ("light", "dark"):
                                page.evaluate("theme=>window.CVSproutTheme.setPreference(theme)", theme)
                                assert page.locator(".modal-body").evaluate("node=>node.scrollWidth<=node.clientWidth+1"), "Modal overflows horizontally"
                                page.screenshot(path=str(output / f"system-models-{width}-{theme}.png"), full_page=True)
                        passed("Desktop and 390px mobile system deployment dialog fits light and dark themes")
                        assert not errors, errors
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
    return {"passed": True, "checks": checks, "page_errors": errors,
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
