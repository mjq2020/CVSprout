"""Exercise project-to-device deployment through real Chromium and an isolated app.

Uses sys.executable for a temporary uvicorn/database. Browser routes provide
controlled training, conversion and deployment responses; no production service,
camera, model conversion, artifact download or hardware operation is contacted.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

import httpx
from playwright.sync_api import expect, sync_playwright

REPO = Path(__file__).resolve().parents[1]
LAUNCH_ARGS = ["--no-sandbox", "--disable-gpu", "--no-zygote", "--disable-software-rasterizer", "--use-gl=disabled", "--no-proxy-server"]


@contextmanager
def isolated_app(output):
    with tempfile.TemporaryDirectory(prefix="cvsprout-project-deploy-") as temporary:
        root = Path(temporary)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        env = {**os.environ, "MDP_DATA_DIR": str(root / "data"), "MDP_MODELS_DIR": str(root / "models"),
               "MDP_AUTH_MODE": "disabled", "MDP_EMBEDDED_WORKER": "false", "MDP_EMBEDDED_PIPELINE_WORKER": "false",
               "MDP_TRAINING_ENABLED": "false", "MDP_ENABLE_DEMO_MODEL": "false", "MDP_WORKER_URL": "", "MDP_WORKER_TOKEN": "",
               "MDP_BOOTSTRAP_API_KEY": "", "MDP_PUBLIC_ORIGIN": "", "MDP_RECAMERA_SSH_KEY": "", "MDP_RECAMERA_KNOWN_HOSTS": "",
               "MDP_MODEL_DEVICE": "cpu", "CUDA_VISIBLE_DEVICES": "", "YOLO_OFFLINE": "true", "YOLO_AUTOINSTALL": "false",
               "YOLO_CONFIG_DIR": str(root / "yolo"), "MDP_TRAINING_MODEL_DIRS": str(root / "models/training")}
        with (output / "server.log").open("w") as log:
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--fd", str(listener.fileno()),
                                        "--no-access-log", "--log-level", "warning"], cwd=REPO, env=env,
                                       pass_fds=(listener.fileno(),), stdout=log, stderr=subprocess.STDOUT)
            listener.close()
            try:
                with httpx.Client(base_url=origin, trust_env=False, timeout=30,
                                  headers={"X-MDP-Client": "browser", "X-Actor-ID": "project-deployment-browser"}) as client:
                    deadline = time.monotonic() + 40
                    while True:
                        try:
                            if client.get("/api/health").status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        if process.poll() is not None or time.monotonic() > deadline:
                            raise RuntimeError("Isolated app failed to start; inspect server.log")
                        time.sleep(.05)
                    yield origin, client
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def training_fixture(project, run_id=101, conversion_id=7):
    now = datetime.now(timezone.utc).isoformat()
    conversion = {"id": conversion_id, "training_run_id": run_id, "project_id": project["id"], "format": "rknn",
                  "status": "completed", "progress": 100, "model_arch": "yolov8n", "manifest_available": True,
                  "settings": {"format": "rknn", "image_size": 320, "device": "cpu", "rknn": {
                      "deployment_profile": "recamera_system", "target_platform": "rv1126b", "quantized": True}},
                  "artifact": {"available": True, "size_bytes": 1048576}, "created_at": now, "finished_at": now}
    run = {"id": run_id, "project_id": project["id"], "version_id": 1, "name": project["name"] + " · YOLOv8",
           "task": "detect", "model_arch": "yolov8n", "model_backend": "yolo", "status": "completed",
           "current_epoch": 2, "total_epochs": 2, "created_at": now, "finished_at": now,
           "artifact": {"available": True, "size_bytes": 2097152}, "settings": {"image_size": 320},
           "metrics": {"metrics/mAP50-95(B)": .5}, "conversions": [conversion]}
    return run


class TrainingRoutes:
    """Shared by both browser scripts so their entries use the actual train page."""
    def __init__(self, runs):
        self.runs = {item["id"]: item for item in runs}

    def fulfill(self, route):
        path = urlsplit(route.request.url).path
        result = None
        match = re.fullmatch(r"/api/projects/(\d+)/training-runs", path)
        if match:
            runs = [run for run in self.runs.values() if run["project_id"] == int(match[1])]
            result = {"runs": runs, "total": len(runs), "counts": {"completed": len(runs), "active": 0},
                      "pagination": {"page": 1, "page_size": 10, "total": len(runs), "total_pages": 1},
                      "availability": {"available": False, "reason": "浏览器隔离测试未启用训练"}}
        match = re.fullmatch(r"/api/training-runs/(\d+)(?:/(metrics|conversions))?", path)
        if match and int(match[1]) in self.runs:
            run = self.runs[int(match[1])]
            result = ({"history": [], "latest": {}, "latest_epoch": 2} if match[2] == "metrics" else
                      {"conversions": run["conversions"]} if match[2] == "conversions" else run)
        if path == "/api/training/catalog":
            result = {"models": [], "tasks": {}, "availability": {"available": False, "reason": "浏览器隔离测试"}}
        if path == "/api/training/resources":
            result = {"devices": [{"id": "cpu", "type": "cpu", "available": True}], "worker": {"available": False}}
        if result is None:
            return False
        route.fulfill(status=200, json=result)
        return True


def online_device(identifier, project, name="在线验收相机"):
    return {"id": identifier, "project_id": project["id"], "project_name": project["name"], "name": name,
            "enabled": True, "revoked_at": None, "capture_enabled": True, "capture_interval_seconds": 10,
            "connection": {"state": "online", "age_seconds": 0, "timeout_seconds": 90},
            "last_seen_at": datetime.now(timezone.utc).isoformat()}


def enter_training(page, origin, project, run):
    page.goto(f"{origin}/#/projects/{project['id']}/train?run={run['id']}")
    expect(page.locator("#training-selected-heading")).to_contain_text(run["name"])
    page.locator('[data-section-tab="train:conversions"]').click()
    expect(page.locator(f'[data-deploy-conversion="{run["conversions"][0]["id"]}"]')).to_be_visible()


def run(chromium, output):
    checks, errors, posted, unexpected = [], [], [], []

    def passed(label):
        checks.append(label)
        print("PASS " + label, flush=True)

    with isolated_app(output) as (origin, client):
        projects = []
        for name in ("部署验收项目甲", "部署验收项目乙"):
            response = client.post("/api/projects", json={"name": name, "task_type": "detect", "labels": ["object"]})
            response.raise_for_status()
            projects.append(response.json())
        first, second = projects
        run_a, run_b = training_fixture(first), training_fixture(second, 202, 17)
        run_b.update(model_arch="yolo11n", name=second["name"] + " · YOLO11")
        run_b["conversions"][0]["model_arch"] = "yolo11n"
        # Non-system, unfinished and missing-artifact conversions must not gain
        # the project deployment action merely because they are RKNN records.
        for identifier, change in ((8, {"settings": {"rknn": {"deployment_profile": "generic"}}}),
                                   (9, {"status": "running", "progress": 40}),
                                   (10, {"artifact": {"available": False}}), (11, {"format": "onnx"})):
            run_a["conversions"].append({**deepcopy(run_a["conversions"][0]), "id": identifier, **change})
        training = TrainingRoutes([run_a, run_b])
        camera = online_device("camera-a", first)
        offline = {**online_device("camera-offline", first, "离线设备"), "connection": {"state": "offline", "age_seconds": 180, "timeout_seconds": 90}}
        wrong_project = online_device("camera-other", second, "其他项目在线设备")
        revoked = {**online_device("camera-revoked", first, "已撤销设备"), "enabled": False, "revoked_at": "2026-11-01T00:00:00Z"}
        devices = [camera, offline, wrong_project, revoked]
        device_error = False
        system_error = False
        delayed_devices, delayed_releases = [], []
        hold_devices = hold_releases = False
        now = datetime.now(timezone.utc).isoformat()
        release = {"id": 41, "project_id": first["id"], "conversion_id": 7, "status": "ready",
                   "filename": "cvsprout-project-test.rknn", "size_bytes": 1048576, "sha256": "a" * 64,
                   "contract": {"algorithm": "yolov8"}, "model_info": {"algorithm": "yolov8"},
                   "default_model_name": "yolov8-run101-conv7"}
        other_release = {**release, "id": 42, "conversion_id": 8, "filename": "wrong-conversion.rknn"}
        system = {"device_id": camera["id"], "project_id": first["id"],
                  "capabilities": {"schema_version": 1, "target_platform": "rv1126b", "model_api": True, "algorithms": ["yolov8", "yolo11"]},
                  "current": {"sModel": "previous.rknn", "iEnable": 1, "sStatus": "running", "iFPS": 30, "iActualFPS": 12, "bExternalHold": False},
                  "system_status": {"agent_id": "browser-fixture", "reported_at": now, "age_seconds": 0, "fresh": True, "error_code": ""},
                  "conversions": [{"id": 7, "training_run_id": 101, "model_arch": "yolov8n", "compatible": True, "algorithm": "yolov8", "default_model_name": "yolov8-run101-conv7"}, {"id": 8, "training_run_id": 101, "model_arch": "yolo11n", "compatible": True}],
                  "releases": [other_release], "deployments": [], "can_deploy": True, "blocked_reason": ""}
        second_system = {**deepcopy(system), "device_id": wrong_project["id"], "project_id": second["id"],
                         "conversions": [{"id": 17, "training_run_id": 202, "model_arch": "yolo11n", "compatible": True,
                                          "algorithm": "yolo11", "default_model_name": "yolo11-run202-conv17"}],
                         "releases": [], "deployments": []}

        with sync_playwright() as pw:
            launch = {"headless": True, "args": LAUNCH_ARGS}
            if chromium:
                launch["executable_path"] = str(chromium)
            browser = pw.chromium.launch(**launch)
            context = browser.new_context(viewport={"width": 1440, "height": 1050})
            page = context.new_page()
            page.set_default_timeout(10000)
            page.on("pageerror", lambda error: errors.append(str(error)))

            def intercept(route):
                request = route.request
                parsed = urlsplit(request.url)
                if not request.url.startswith(origin + "/"):
                    unexpected.append("external request blocked")
                    route.abort()
                    return
                if training.fulfill(route):
                    return
                if parsed.path == "/api/edge/devices":
                    selected_project = parse_qs(parsed.query).get("project_id")
                    assert selected_project in ([str(first["id"])], [str(second["id"])]), "Device list must scope to the project"
                    if selected_project == [str(second["id"])]:
                        route.fulfill(status=200, json={"devices": [wrong_project]})
                    elif hold_devices:
                        delayed_devices.append(route)
                    else:
                        route.fulfill(status=503 if device_error else 200, json={"detail": "Controlled device-list failure"} if device_error else {"devices": devices})
                elif parsed.path.endswith("/system-models"):
                    if parsed.path == f'/api/edge/devices/{wrong_project["id"]}/system-models':
                        assert parse_qs(parsed.query).get("conversion_id") == ["17"]
                        route.fulfill(status=200, json=second_system)
                    else:
                        assert parsed.path == f'/api/edge/devices/{camera["id"]}/system-models'
                        assert parse_qs(parsed.query).get("conversion_id") == ["7"]
                        route.fulfill(status=503 if system_error else 200, json={"detail": "Controlled system-state failure"} if system_error else system)
                elif parsed.path == "/api/edge/model-releases":
                    assert request.method == "POST" and request.post_data_json == {"conversion_id": 7}
                    posted.append({"kind": "release", "body": request.post_data_json})
                    if hold_releases:
                        delayed_releases.append(route)
                    else:
                        system["releases"] = [other_release, release]
                        route.fulfill(status=201, json=release)
                elif parsed.path == f'/api/edge/devices/{camera["id"]}/model-deployments':
                    assert request.method == "POST"
                    body = request.post_data_json
                    assert body["release_id"] == release["id"] and type(body["release_id"]) is int
                    posted.append({"kind": "deployment", "body": body})
                    assert "model_name" in body, "Deployment should explicitly carry the user's model name"
                    chosen_name = body["model_name"] or release["default_model_name"]
                    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", chosen_name)
                    task = {"id": len(posted), "release_id": release["id"], "model_name": chosen_name,
                            "filename": f"{chosen_name}-r41.rknn", "status": "queued", "stage": "queued",
                            "activate": body["activate"], "fps": body["fps"], "updated_at": now}
                    system["deployments"] = [task]
                    route.fulfill(status=202, json=task)
                elif request.method not in ("GET", "HEAD", "OPTIONS"):
                    unexpected.append(f"Unexpected write blocked: {request.method} {parsed.path}")
                    route.fulfill(status=409, json={"detail": "Browser fixture blocks non-fixture writes"})
                else:
                    route.continue_()

            context.route("**/*", intercept)

            def wait_held(routes):
                deadline = time.monotonic() + 10
                while not routes:
                    if time.monotonic() >= deadline:
                        raise AssertionError("Expected delayed fixture request did not arrive")
                    # Pump Playwright events while waiting on a Python route
                    # condition; release publishing first performs async checks.
                    page.wait_for_timeout(10)

            def fulfill_late(route, payload, status=200):
                with page.expect_response(lambda response: response.url == route.request.url) as observed:
                    route.fulfill(status=status, json=payload)
                # Reading the body waits for delivery without Response.finished's
                # extra close-listener task in the installed Playwright release.
                observed.value.body()
                page.evaluate("() => new Promise(resolve=>queueMicrotask(resolve))")

            try:
                enter_training(page, origin, first, run_a)
                expect(page.locator("[data-deploy-conversion]")).to_have_count(1)
                page.locator('[data-deploy-conversion="7"]').click()
                expect(page.locator("#edge-project-device-select")).to_be_visible()
                options = page.locator("#edge-project-device-select option").evaluate_all("nodes=>nodes.filter(n=>!n.disabled&&n.value).map(n=>n.value)")
                assert options == [camera["id"]], options
                assert wrong_project["name"] not in page.locator("#edge-project-device-content").inner_text()
                passed("Real training conversion action appears only for completed available system RKNN and filters devices by project/online/revocation")
                page.locator("#edge-project-device-select").select_option(camera["id"])
                page.locator("#modal-submit").click()
                expect(page.locator("#edge-system-content")).to_be_visible()
                expect(page.locator("#edge-system-conversion")).to_have_count(0)
                expect(page.locator("#edge-system-release")).to_have_count(0)
                expect(page.locator("#edge-system-build")).to_have_count(0)
                expect(page.locator("#modal-submit")).to_be_enabled()
                expect(page.locator("#edge-system-mode")).to_have_value("upload")
                name_input = page.locator("#edge-system-model-name")
                expect(name_input).to_have_value("yolov8-run101-conv7")
                writes_before = len(posted)
                for invalid in ("../model", "part/model", "模型", "_leading", "model.rknn", "two words"):
                    name_input.fill(invalid)
                    expect(name_input).to_have_attribute("aria-invalid", "true")
                    expect(page.locator("#edge-system-model-name-error")).to_be_visible()
                    expect(page.locator("#modal-submit")).to_be_disabled()
                name_input.fill("A" * 64)
                expect(page.locator("#modal-submit")).to_be_enabled()
                name_input.fill("A" * 65)
                expect(page.locator("#modal-submit")).to_be_disabled()
                expect(name_input).to_have_attribute("aria-invalid", "true")
                assert len(posted) == writes_before
                name_input.fill("Assembly_line-A")
                expect(name_input).to_have_attribute("aria-invalid", "false")
                expect(page.locator("#modal-submit")).to_be_enabled()
                passed("Algorithm default name is editable; ASCII naming and the 64-character limit block invalid submissions")
                for width in (1440, 390):
                    page.set_viewport_size({"width": width, "height": 1050 if width == 1440 else 844})
                    assert page.locator(".modal-body").evaluate("node=>node.scrollWidth<=node.clientWidth+1")
                    page.screenshot(path=str(output / f"deployment-ready-{width}.png"), full_page=True)
                page.set_viewport_size({"width": 1440, "height": 1050})
                page.locator("#modal-submit").click()
                expect(page.locator("#edge-system-progress")).to_contain_text("等待设备领取")
                assert [item["kind"] for item in posted] == ["release", "deployment"]
                assert posted[-1] == {"kind": "deployment", "body": {"release_id": 41, "activate": False, "fps": 30, "model_name": "Assembly_line-A"}}
                expect(name_input).to_have_value("Assembly_line-A")
                expect(page.locator("#edge-system-progress")).to_contain_text("Assembly_line-A-r41.rknn")
                expect(page.locator("#edge-system-jobs")).to_contain_text("Assembly_line-A-r41.rknn")
                assert release["filename"] not in page.locator("#edge-system-progress").inner_text()
                assert release["filename"] == "cvsprout-project-test.rknn"
                page.screenshot(path=str(output / "named-model-queued.png"), full_page=True)
                passed("One click prepares the locked conversion and submits the custom name; progress uses the task filename without renaming its release")

                page.clock.install()

                def poll():
                    with page.expect_response(lambda response: urlsplit(response.url).path.endswith("/system-models")):
                        page.clock.fast_forward(5000)

                system["deployments"][0].update(status="succeeded", stage="uploaded")
                poll()
                expect(name_input).to_have_value("Assembly_line-A")
                expect(page.locator("#edge-system-model-name-preview")).to_contain_text("Assembly_line-A-r41.rknn")
                name_input.fill("Assembly_line-B")
                name_input.focus()
                poll()
                expect(name_input).to_have_value("Assembly_line-B")
                expect(name_input).to_be_focused()
                passed("Preparation and polling preserve the user name, filename preview and input focus")
                page.locator("#edge-system-mode").select_option("activate")
                expect(page.locator("#modal-submit")).to_be_disabled()
                page.locator("#edge-system-fps-field summary").click()
                page.locator("#edge-system-fps").fill("17")
                page.locator("#edge-system-confirm").check()
                name_input.fill("Assembly_line-C")
                expect(page.locator("#edge-system-confirm")).not_to_be_checked()
                expect(page.locator("#modal-submit")).to_be_disabled()
                page.locator("#edge-system-confirm").check()
                with page.expect_response(lambda response: urlsplit(response.url).path.endswith("/model-deployments") and response.request.method == "POST"):
                    page.locator("#modal-submit").click()
                expect(page.locator("#edge-system-jobs")).to_contain_text("等待设备领取")
                assert posted[-1] == {"kind": "deployment", "body": {"release_id": 41, "activate": True, "fps": 17, "model_name": "Assembly_line-C"}}
                expect(page.locator("#edge-system-progress")).to_contain_text("Assembly_line-C-r41.rknn")
                for stage, completed in (("downloading", 1), ("uploading", 2), ("activating", 3), ("activated", 4)):
                    system["deployments"][0].update(status="running", stage=stage)
                    poll()
                    expect(page.locator("#edge-system-progress progress")).to_have_attribute("value", str(completed))
                    expect(page.locator("#edge-system-progress")).to_contain_text(f"已完成 {completed} / 5 个阶段")
                page.screenshot(path=str(output / "deployment-verifying.png"), full_page=True)
                system["deployments"][0].update(status="running", stage="activated")
                poll()
                expect(page.locator("#edge-system-jobs")).to_contain_text("等待运行验证")
                assert "已验证系统推理运行" not in page.locator("#edge-system-jobs").inner_text()
                system["deployments"][0].update(status="succeeded", stage="verified")
                poll()
                expect(page.locator("#edge-system-jobs")).to_contain_text("已验证系统推理运行")
                expect(page.locator("#edge-system-progress progress")).to_have_attribute("value", "5")
                passed("Explicit activation confirmation carries FPS and shows queued/running/verified progress without premature success")

                for width in (1440, 390):
                    page.set_viewport_size({"width": width, "height": 1050 if width == 1440 else 844})
                    for theme in ("light", "dark"):
                        page.evaluate("theme=>window.CVSproutTheme.setPreference(theme)", theme)
                        assert page.locator(".modal-body").evaluate("node=>node.scrollWidth<=node.clientWidth+1")
                        page.screenshot(path=str(output / f"project-deployment-{width}-{theme}.png"), full_page=True)
                passed("Project deployment dialog fits desktop and 390px mobile in light/dark themes")
                page.set_viewport_size({"width": 1440, "height": 1050})
                page.locator("#modal-close").click()

                def open_picker():
                    page.locator('[data-deploy-conversion="7"]').click()
                    expect(page.locator("#edge-project-device-content")).to_be_visible()

                def open_deployment():
                    open_picker()
                    page.locator("#edge-project-device-select").select_option(camera["id"])
                    page.locator("#modal-submit").click()
                    expect(page.locator("#edge-system-model-name")).to_be_visible()

                open_deployment()
                expect(name_input).to_have_value("yolov8-run101-conv7")
                name_input.fill("Discard_me")
                page.locator("#modal-cancel").click()
                open_deployment()
                expect(name_input).to_have_value("yolov8-run101-conv7")
                page.locator("#modal-cancel").click()
                passed("Closing and reopening deployment restores the algorithm default without reusing discarded names")

                devices[:] = [offline, wrong_project, revoked]
                open_picker()
                expect(page.locator("#modal-submit")).to_be_disabled()
                assert not page.locator("#edge-project-device-select option").evaluate_all("nodes=>nodes.some(n=>!n.disabled&&n.value)")
                page.locator("#modal-close").click()
                devices[:] = []
                open_picker()
                expect(page.locator("#modal-submit")).to_be_disabled()
                page.locator("#modal-close").click()
                device_error = True
                open_picker()
                expect(page.locator("#edge-project-device-feedback")).to_be_visible()
                expect(page.locator("#modal-submit")).to_be_disabled()
                device_error = False
                devices[:] = [camera]
                page.locator("#edge-project-device-refresh").click()
                expect(page.locator(f'#edge-project-device-select option[value="{camera["id"]}"]')).to_be_enabled()
                passed("Offline-only, no-device and failed-device-list states block continuation; explicit refresh recovers")
                page.locator("#edge-project-device-select").select_option(camera["id"])
                camera["connection"] = {"state": "offline", "age_seconds": 180, "timeout_seconds": 90}
                page.locator("#modal-submit").click()
                expect(page.locator("#modal-submit")).to_be_disabled()
                expect(page.locator("#edge-system-content")).to_have_count(0)
                expect(page.locator("#edge-project-device-status")).to_contain_text("离线")
                camera["connection"] = {"state": "online", "age_seconds": 0, "timeout_seconds": 90}
                page.locator("#edge-project-device-refresh").click()
                expect(page.locator(f'#edge-project-device-select option[value="{camera["id"]}"]')).to_be_enabled()
                passed("Continue rechecks connectivity and refuses a device that went offline after selection")
                system_error = True
                page.locator("#edge-project-device-select").select_option(camera["id"])
                page.locator("#modal-submit").click()
                expect(page.locator("#edge-system-content .inline-error")).to_be_visible()
                expect(page.locator("#modal-submit")).to_be_disabled()
                passed("Failed system-model capability lookup blocks deployment independently of device connection")
                system_error = False
                page.locator("#modal-close").click()

                hold_devices = True
                open_picker()
                expect(page.locator("#modal-submit")).to_be_disabled()
                page.wait_for_function("document.querySelector('#edge-project-device-content') !== null")
                wait_held(delayed_devices)
                page.evaluate("hash=>location.hash=hash", f"#/projects/{second['id']}/train?run={run_b['id']}")
                expect(page.locator("#training-selected-heading")).to_contain_text(run_b["name"])
                page.locator('[data-section-tab="train:conversions"]').click()
                page.locator('[data-deploy-conversion="17"]').click()
                expect(page.locator('#edge-project-device-select option[value="camera-other"]')).to_be_enabled()
                for route in delayed_devices:
                    fulfill_late(route, {"devices": [camera]})
                delayed_devices.clear()
                hold_devices = False
                assert page.locator("#edge-project-device-select option").evaluate_all("nodes=>nodes.filter(n=>!n.disabled&&n.value).map(n=>n.value)") == [wrong_project["id"]]
                assert not page.locator('#edge-project-device-select option[value="camera-a"]').count()
                expect(page.locator("#edge-system-content")).to_have_count(0)
                expect(page.locator("#training-selected-heading")).to_contain_text(run_b["name"])
                page.locator("#edge-project-device-select").select_option(wrong_project["id"])
                page.locator("#modal-submit").click()
                expect(name_input).to_have_value("yolo11-run202-conv17")
                assert "Assembly_line" not in name_input.input_value()
                name_input.fill("Other_project_only")
                passed("Late responses cannot replace another project's selector or leak names into its YOLO11 deployment")
                page.locator("#modal-close").click()

                enter_training(page, origin, first, run_a)
                system["releases"] = [other_release]
                open_picker()
                page.locator("#edge-project-device-select").select_option(camera["id"])
                page.locator("#modal-submit").click()
                expect(page.locator("#modal-submit")).to_be_enabled()
                expect(name_input).to_have_value("yolov8-run101-conv7")
                name_input.fill("Pending_project_A")
                original_name_node = name_input.element_handle()
                hold_releases = True
                page.locator("#modal-submit").click()
                wait_held(delayed_releases)
                expect(name_input).to_have_value("Pending_project_A")
                # The shared modal freezes real user edits during submission.
                # Deliberately simulate a programmatic/late state change to
                # exercise the controller's extra snapshot protection.
                assert page.locator(".modal-body").evaluate("node=>node.inert")
                name_input.evaluate("node=>{node.value='Changed_while_preparing';node.dispatchEvent(new Event('input',{bubbles:true}))}")
                assert original_name_node.evaluate("node=>node.isConnected && node.value==='Changed_while_preparing'"), "Preparation replaced the name input instead of preserving the node holding its edit"
                writes_before = len(posted)
                system["releases"] = [other_release, release]
                for route in delayed_releases:
                    fulfill_late(route, release, 201)
                delayed_releases.clear()
                hold_releases = False
                expect(page.locator("#edge-system-progress")).to_contain_text("模型名称已变化")
                expect(name_input).to_have_value("Changed_while_preparing")
                expect(page.locator("#modal-submit")).to_be_enabled()
                assert len(posted) == writes_before, "Changing the name during preparation must not deploy either name"
                page.screenshot(path=str(output / "model-name-changed-during-prepare.png"), full_page=True)
                passed("Preparation freezes user edits; a simulated late name change is preserved and requires fresh confirmation instead of deploying the old name")
                page.locator("#modal-close").click()

                system["releases"] = [other_release]
                open_deployment()
                expect(name_input).to_have_value("yolov8-run101-conv7")
                name_input.fill("Pending_project_A")
                hold_releases = True
                page.locator("#modal-submit").click()
                wait_held(delayed_releases)
                page.evaluate("hash=>location.hash=hash", f"#/projects/{second['id']}/train?run={run_b['id']}")
                expect(page.locator("#training-selected-heading")).to_contain_text(run_b["name"])
                writes_before = len(posted)
                for route in delayed_releases:
                    fulfill_late(route, release, 201)
                delayed_releases.clear()
                hold_releases = False
                expect(page.locator("#edge-system-content")).to_have_count(0)
                expect(page.locator("#training-selected-heading")).to_contain_text(run_b["name"])
                assert len(posted) == writes_before
                passed("A late publish response after project navigation cannot reopen controls or submit a deployment")
                assert not errors, errors
                assert not unexpected, unexpected
            except Exception:
                page.screenshot(path=str(output / "failure.png"), full_page=True)
                (output / "failure-context.json").write_text(json.dumps({"page_errors": errors, "unexpected_requests": unexpected,
                                                                       "url": page.url, "checks": checks, "posted": posted,
                                                                       "model_name": page.locator("#edge-system-model-name").input_value() if page.locator("#edge-system-model-name").count() else None}, ensure_ascii=False, indent=2))
                raise
            finally:
                for route in delayed_devices + delayed_releases:
                    try:
                        route.abort()
                    except Exception:
                        pass
                context.close()
                browser.close()
    for name in ("failure.png", "failure-context.json"):
        (output / name).unlink(missing_ok=True)
    return {"passed": True, "checks": checks, "page_errors": errors, "unexpected_requests": unexpected,
            "scope": "Real project training UI in isolated uvicorn/temp DB; controlled API fixtures; no production or hardware operations"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output = (args.output_dir or Path(tempfile.mkdtemp(prefix="cvsprout-project-deployment-proof-"))).resolve()
    output.mkdir(parents=True, exist_ok=True)
    print("Artifacts: " + str(output), flush=True)
    result = run(args.chromium, output)
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
