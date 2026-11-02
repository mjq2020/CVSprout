"""Exercise edge devices in real Chromium against an isolated temporary server.

Run with the project's installed Python environment (requires Playwright):
    python scripts/smoke_edge_devices_browser.py --chromium /path/to/chrome
The script uses sys.executable for uvicorn, never contacts a camera/production
server, and writes only screenshots plus a credential-free JSON report to the
output directory. Its temporary project/database/media are removed on exit.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from PIL import Image
from playwright.sync_api import expect, sync_playwright


REPO = Path(__file__).resolve().parents[1]
LAN_HOST = "cvsprout-clipboard.test"


def checked(response, expected=200):
    if response.status_code != expected:
        # A creation response can contain the one-time token. Never print bodies.
        raise AssertionError(f"Fixture HTTP response: expected {expected}, received {response.status_code}")
    return response.json()


def jpeg(color):
    stream = io.BytesIO()
    Image.new("RGB", (160, 120), color).save(stream, "JPEG")
    return stream.getvalue()


def run(chromium, output):
    checks, errors = [], []

    def passed(label):
        checks.append(label)
        print("PASS " + label, flush=True)

    with tempfile.TemporaryDirectory(prefix="cvsprout-edge-browser-data-") as temporary:
        root = Path(temporary)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        secure = f"http://127.0.0.1:{port}"
        lan = f"http://{LAN_HOST}:{port}"
        env = {**os.environ, "MDP_DATA_DIR": str(root / "data"), "MDP_MODELS_DIR": str(root / "models"),
               "MDP_AUTH_MODE": "disabled", "MDP_EMBEDDED_WORKER": "false",
               "MDP_EMBEDDED_PIPELINE_WORKER": "false", "MDP_WORKER_URL": "", "MDP_WORKER_TOKEN": "",
               "MDP_BOOTSTRAP_API_KEY": "", "MDP_PUBLIC_ORIGIN": "", "MDP_TRAINING_ENABLED": "false",
               "MDP_MODEL_DEVICE": "cpu", "MDP_ENABLE_DEMO_MODEL": "false", "CUDA_VISIBLE_DEVICES": "",
               "MDP_RECAMERA_SSH_KEY": "", "MDP_RECAMERA_KNOWN_HOSTS": "", "YOLO_OFFLINE": "true",
               "YOLO_AUTOINSTALL": "false", "YOLO_CONFIG_DIR": str(root / "yolo"),
               "MDP_TRAINING_MODEL_DIRS": str(root / "models/training")}
        with (output / "server.log").open("w") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--fd", str(listener.fileno()),
                 "--no-access-log", "--log-level", "warning"], cwd=REPO, env=env,
                pass_fds=(listener.fileno(),), stdout=log, stderr=subprocess.STDOUT)
            listener.close()
            try:
                with httpx.Client(base_url=secure, trust_env=False, timeout=30,
                                  headers={"X-Actor-ID": "edge-browser-test", "X-MDP-Client": "browser"}) as client:
                    deadline = time.monotonic() + 30
                    while True:
                        try:
                            if client.get("/api/health").status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        if process.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError("Isolated server did not become ready; inspect server.log")
                        time.sleep(.05)
                    project = checked(client.post("/api/projects", json={
                        "name": "浏览器验收独立项目", "task_type": "detect", "labels": ["object"]}), 201)
                    with sync_playwright() as pw:
                        launch = {"headless": True, "args": ["--no-sandbox", "--disable-gpu", "--no-zygote",
                                  "--disable-software-rasterizer", "--use-gl=disabled",
                                  "--no-proxy-server", f"--host-resolver-rules=MAP {LAN_HOST} 127.0.0.1"]}
                        if chromium:
                            launch["executable_path"] = str(chromium)
                        browser = pw.chromium.launch(**launch)
                        context = browser.new_context(viewport={"width": 1440, "height": 1050})
                        context.grant_permissions(["clipboard-read", "clipboard-write"], origin=secure)
                        context.on("page", lambda page: page.on("pageerror", lambda error: errors.append(str(error))))
                        page = context.new_page()
                        reader = context.new_page()
                        page.set_default_timeout(12000)
                        reader.set_default_timeout(12000)
                        try:
                            reader.goto(secure + "/api/health")
                            assert reader.evaluate("isSecureContext && typeof navigator.clipboard.readText === 'function'"), "Secure clipboard reader unavailable"
                            page.bring_to_front()
                            page.goto(lan + "/#/devices", wait_until="domcontentloaded")
                            expect(page.locator(".edge-empty")).to_be_visible()
                            assert page.evaluate("!isSecureContext && !navigator.clipboard"), "LAN fixture must use an actual insecure context"

                            def create_from_ui(target, name, address):
                                target.locator("#add-edge-device").click()
                                target.locator("#edge-device-name").fill(name)
                                target.locator("#edge-server-url").fill(address)
                                target.locator("#modal-submit").click()
                                field = target.locator("#edge-copy-token")
                                expect(field).to_be_visible()
                                value = field.input_value()
                                assert value.startswith("cvsprout_device_"), "Fixture token has unexpected format"
                                expect(field).to_have_attribute("type", "password")
                                return value

                            token = create_from_ui(page, "LAN HTTP 测试相机", lan)
                            page.locator("#edge-copy-token-button").click()
                            expect(page.locator(".toast.success").filter(has_text="设备令牌已复制。")).to_be_visible()
                            expect(page.locator("#edge-copy-token-button")).to_be_focused()
                            reader.bring_to_front()
                            copied = reader.evaluate("navigator.clipboard.readText()")
                            assert copied == token, "Actual LAN clipboard content does not match the complete token"
                            copied = None
                            page.bring_to_front()
                            expect(page.locator("#edge-copy-token")).to_have_attribute("type", "password")
                            expect(page.locator("[data-edge-clipboard]")).to_have_count(0)
                            expect(page.locator("#edge-copy-token-button")).to_be_enabled()
                            passed("LAN HTTP uses real button-triggered clipboard copy; secure readback matches the full masked token")

                            # Exercise denial without relying on OS policy: only this copy
                            # boundary is controlled, and the actual click/UI path remains real.
                            page.evaluate("window.savedExecCommand=document.execCommand; document.execCommand=()=>false")
                            try:
                                page.locator("#edge-copy-token-button").click()
                                expect(page.locator(".toast.warning").filter(has_text="浏览器未能复制令牌")).to_be_visible()
                                expect(page.locator("[data-edge-clipboard]")).to_have_count(0)
                            finally:
                                page.evaluate("document.execCommand=window.savedExecCommand; delete window.savedExecCommand")
                            passed("Denied clipboard copy shows manual-copy guidance and removes the temporary secret control")

                            devices = checked(client.get("/api/edge/devices"))["devices"]
                            device = next(item for item in devices if item["name"] == "LAN HTTP 测试相机")
                            did = device["id"]
                            auth = {"Authorization": "Bearer " + token}
                            checked(client.get("/api/edge/v1/config", headers=auth))
                            expect(page.locator("#edge-setup-connection strong")).to_have_text("已确认设备连接成功")
                            assert page.locator("#edge-copy-token").input_value() == token, "Polling changed the token field"
                            page.locator("#modal-submit").click()

                            # Localhost is a trustworthy browser context, equivalent to HTTPS
                            # for Clipboard API availability; this is the real API path.
                            reader.bring_to_front()
                            reader.goto(secure + "/#/devices", wait_until="domcontentloaded")
                            expect(reader.locator(".edge-device-card")).to_be_visible()
                            secure_token = create_from_ui(reader, "Secure API 测试相机", lan)
                            assert reader.evaluate("isSecureContext && typeof navigator.clipboard.writeText === 'function'")
                            reader.locator("#edge-copy-token-button").click()
                            expect(reader.locator(".toast.success").filter(has_text="设备令牌已复制。")).to_be_visible()
                            expect(reader.locator("#edge-copy-token-button")).to_be_focused()
                            assert reader.evaluate("navigator.clipboard.readText()") == secure_token, "Clipboard API readback mismatch"
                            expect(reader.locator("#edge-copy-token")).to_have_attribute("type", "password")
                            reader.locator("#modal-submit").click()
                            secure_token = None
                            passed("Trustworthy localhost uses Clipboard API; actual readback matches without exposing the token")

                            # Real uploads create 26 duplicate-content events, one controlled
                            # browser HTTP 404, and one asset deleted via the public API.
                            start = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
                            uploads = []
                            for index in range(28):
                                color = (20, 100, 60) if index < 26 else ((200, 40, 80) if index == 26 else (30, 60, 210))
                                capture_id = f"browser-capture-{index:02d}"
                                response = checked(client.post("/api/edge/v1/captures", headers=auth,
                                    data={"capture_id": capture_id,
                                          "captured_at": (start + timedelta(minutes=index)).isoformat(),
                                          "model_release": f"browser-release-{index:02d}"},
                                    files={"file": ("camera.jpg", jpeg(color), "image/jpeg")}), 201)
                                uploads.append(response)
                            broken_id, deleted_id = uploads[26]["asset_id"], uploads[27]["asset_id"]
                            asset = checked(client.get(f"/api/assets/{deleted_id}"))
                            deletion = checked(client.post(f"/api/projects/{project['id']}/assets/delete-batch", json={
                                "assets": [{"asset_id": deleted_id, "expected_revision": asset["revision"]}]}))
                            assert deletion["counts"]["succeeded"] == 1, "Isolated deleted-image fixture failed"
                            checked(client.post("/api/edge/v1/heartbeat", headers=auth, json={
                                "app_version": "1.0.2", "state": "capturing", "frames_received": 200,
                                "captured_images": 28, "uploaded_images": 28, "pending_images": 0}))
                            page.bring_to_front()
                            card = page.locator(f'[data-edge-device="{did}"]')
                            expect(card.locator('[data-edge-value="captures"]')).to_have_text("28")
                            expect(card.locator(".edge-connection")).to_contain_text("在线")
                            page.route(re.compile(rf"/api/assets/{broken_id}/(?:thumbnail|image)(?:\?.*)?$"),
                                       lambda route: route.fulfill(status=404, content_type="text/plain", body="controlled missing image"))
                            card.locator("[data-edge-captures]").click()
                            expect(page.locator(".edge-record-list article")).to_have_count(25)
                            expect(page.locator(".edge-record-pagination")).to_contain_text("共 28 条记录")
                            expect(page.locator(".edge-record-list .unavailable")).to_contain_text("原图已删除")
                            missing = page.locator('[data-edge-capture="browser-capture-26"]')
                            expect(missing.locator("[data-edge-image-status]")).to_have_text("图片暂不可用")
                            expect(missing.locator("img")).to_be_hidden()
                            missing.click()
                            expect(page.locator("#edge-record-preview [data-edge-image-status]")).to_have_text("图片暂不可用")
                            page.keyboard.press("Escape")
                            expect(page.locator("#edge-record-page")).to_be_visible()
                            expect(page.locator(".modal")).to_be_visible()
                            page.screenshot(path=str(output / "records-desktop.png"), full_page=True)
                            passed("Deleted originals retain records; thumbnail/original HTTP 404 show clear placeholders; Escape returns to the list")

                            original = page.locator('[data-edge-capture="browser-capture-25"]')
                            image = original.locator("img")
                            image.scroll_into_view_if_needed()
                            page.wait_for_function("document.querySelector('[data-edge-capture=\"browser-capture-25\"] img').naturalWidth > 0")
                            assert image.evaluate("image => image.complete && image.naturalWidth > 0"), "Thumbnail did not decode"
                            row = original.locator("..")
                            expect(row).to_contain_text("已有相同图片 · 已关联")
                            expect(row).to_contain_text("browser-release-25")
                            captured_text = row.locator(".edge-record-heading > span").inner_text()
                            original.click()
                            preview_image = page.locator("#edge-record-preview img")
                            page.wait_for_function("document.querySelector('#edge-record-preview img').naturalWidth === 160")
                            assert preview_image.evaluate("image => image.naturalWidth === 160 && image.naturalHeight === 120"), "Original preview resolution changed"
                            expect(page.locator("#edge-record-preview > p")).to_contain_text(captured_text)
                            page.keyboard.press("Escape")
                            expect(original).to_be_focused()
                            expect(page.locator("#edge-record-page")).to_be_visible()
                            far_record = page.locator('[data-edge-capture="browser-capture-07"]')
                            far_record.scroll_into_view_if_needed()
                            scroller = page.locator(".modal-body")
                            old_scroll = scroller.evaluate("node=>node.scrollTop")
                            assert old_scroll > 0, "Scrollable record fixture did not reach a lower position"
                            far_record.click()
                            assert scroller.evaluate("node=>node.scrollTop") <= 1, "Preview did not scroll to the top"
                            page.keyboard.press("Escape")
                            assert abs(scroller.evaluate("node=>node.scrollTop") - old_scroll) <= 1, "Preview return lost the old list scroll position"
                            expect(far_record).to_be_focused()
                            passed("Decoded thumbnails open the original image and preserve duplicate capture-specific metadata and trigger focus")

                            page.locator("#edge-record-next").click()
                            expect(page.locator(".edge-record-list article")).to_have_count(3)
                            expect(page.locator("#edge-record-next")).to_be_disabled()
                            assert page.evaluate("!!document.activeElement.closest('.modal')"), "Pagination lost focus outside the modal"
                            assert scroller.evaluate("node=>node.scrollTop") <= 1, "Next page retained the old bottom scroll position"
                            first = page.locator('[data-edge-capture="browser-capture-01"]')
                            expected_time = first.locator("..").locator(".edge-record-heading > span").inner_text()
                            ids_before = page.locator("[data-edge-capture]").evaluate_all("nodes=>nodes.map(node=>node.dataset.edgeCapture)")
                            first.click()
                            expect(page.locator("#edge-record-preview > p")).to_contain_text(expected_time)
                            page.locator("[data-edge-preview-back]").click()
                            assert page.locator("[data-edge-capture]").evaluate_all("nodes=>nodes.map(node=>node.dataset.edgeCapture)") == ids_before, "Preview return reset pagination"
                            expect(first).to_be_focused()
                            expect(page.locator("#edge-record-next")).to_be_disabled()
                            first.click()
                            page.keyboard.press("Escape")
                            expect(page.locator(".edge-record-list article")).to_have_count(3)
                            expect(page.locator("#edge-record-prev")).to_be_enabled()
                            page.locator("#edge-record-prev").click()
                            expect(page.locator(".edge-record-list article")).to_have_count(25)
                            assert page.evaluate("!!document.activeElement.closest('.modal')"), "Previous page lost modal focus"
                            assert scroller.evaluate("node=>node.scrollTop") <= 1, "Previous page did not reset scroll"
                            page.locator("#edge-record-next").click()
                            expect(page.locator(".edge-record-list article")).to_have_count(3)
                            passed("Preview Back and Escape preserve second-page records, pagination controls and capture-specific metadata")

                            for theme in ("light", "dark"):
                                page.set_viewport_size({"width": 390, "height": 844})
                                page.evaluate("theme=>window.CVSproutTheme.setPreference(theme)", theme)
                                assert page.evaluate("document.body.scrollWidth <= innerWidth + 1"), "Mobile page overflows horizontally"
                                assert page.locator(".modal-body").evaluate("node=>node.scrollWidth <= node.clientWidth + 1"), "Mobile records overflow horizontally"
                                page.screenshot(path=str(output / f"records-mobile-{theme}.png"), full_page=True)
                                first.click()
                                page.wait_for_function("document.querySelector('#edge-record-preview img').naturalWidth > 0")
                                assert page.locator(".modal-body").evaluate("node=>node.scrollWidth <= node.clientWidth + 1"), "Mobile preview overflows horizontally"
                                page.screenshot(path=str(output / f"preview-mobile-{theme}.png"), full_page=True)
                                page.keyboard.press("Escape")
                            page.locator("#modal-submit").click()
                            for theme in ("light", "dark"):
                                page.evaluate("theme=>window.CVSproutTheme.setPreference(theme)", theme)
                                assert page.evaluate("document.body.scrollWidth <= innerWidth + 1"), "Mobile devices overflow horizontally"
                                page.screenshot(path=str(output / f"devices-mobile-{theme}.png"), full_page=True)
                            page.set_viewport_size({"width": 1440, "height": 1050})
                            page.screenshot(path=str(output / "devices-desktop.png"), full_page=True)
                            assert not errors, "Browser pageerror events occurred; see result.json"
                            passed("390px light/dark device list, capture records and previews have no horizontal overflow or page errors")
                        except Exception:
                            # Password input stays masked; never store page HTML or traces,
                            # since they could retain the one-time credential value.
                            try:
                                page.screenshot(path=str(output / "failure.png"), full_page=True, timeout=3000)
                            except Exception:
                                pass
                            (output / "result.json").write_text(json.dumps({
                                "passed": False, "checks_completed": checks, "page_errors": errors,
                                "isolation": "Only temporary server/database/media used",
                            }, ensure_ascii=False, indent=2) + "\n")
                            raise
                        finally:
                            try:
                                reader.bring_to_front()
                                reader.evaluate("navigator.clipboard.writeText('')")
                            except Exception:
                                pass
                            browser.close()
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    return {"passed": True, "checks": checks, "page_errors": errors,
            "isolation": "Temporary real HTTP server/database/media; simulated camera via public APIs; no production or device changes",
            "clipboard": "Real LAN-HTTP click + secure-context readback, real Clipboard API click + readback; tokens never printed",
            "captures": 28, "current_assets": 2, "mobile_width": 390}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium", type=Path, help="Existing Chromium executable; defaults to Playwright's installed browser")
    parser.add_argument("--output-dir", type=Path, help="Screenshots and sanitized report directory")
    args = parser.parse_args()
    output = (args.output_dir or Path(tempfile.mkdtemp(prefix="cvsprout-edge-browser-proof-"))).resolve()
    output.mkdir(parents=True, exist_ok=True)
    print("Artifacts: " + str(output), flush=True)
    result = run(args.chromium, output)
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print("PASS edge device browser acceptance", flush=True)


if __name__ == "__main__":
    main()
