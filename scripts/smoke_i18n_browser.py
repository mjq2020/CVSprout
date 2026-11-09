"""Exercise both languages in Chromium against an isolated, worker-free server.

Usage: .venv/bin/python scripts/smoke_i18n_browser.py [--chromium /path/to/chrome]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

import httpx
from PIL import Image
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
USER_TEXT = "项目不存在"


def seed_populated_states(client, folder, project_id):
    """Use real API records plus inert job history; never start inference/workers."""
    response = client.post(f"/api/projects/{project_id}/work-blocks", json={"block_size": 1})
    response.raise_for_status()
    block_id = response.json()["blocks"][0]["id"]
    response = client.post(f"/api/projects/{project_id}/versions", json={"name": USER_TEXT, "approved_only": False,
                          "train_ratio": 1, "valid_ratio": 0, "test_ratio": 0})
    response.raise_for_status()
    version_id = response.json()["id"]
    response = client.post("/api/edge/devices", json={"name": USER_TEXT, "project_id": project_id})
    response.raise_for_status()
    device_id = response.json()["id"]
    now = "2026-01-02T03:04:05+00:00"
    with sqlite3.connect(folder / "data/mdp.sqlite3") as conn:
        # Chinese user fields deliberately equal a translation key.
        conn.execute("""INSERT INTO model_services(id,name,family,provider_type,description,endpoint,model_name,
            capabilities_json,modes_json,config_json,builtin,created_at,updated_at)
            VALUES ('fixture-custom',?,'Custom','remote_detection',?,'http://invalid.test','fixture',?,'["auto"]','{"badge":"自定义"}',0,?,?)""",
                     (USER_TEXT, USER_TEXT, json.dumps([USER_TEXT]), now, now))
        conn.execute("""INSERT INTO model_services(id,name,family,provider_type,description,endpoint,model_name,
            capabilities_json,modes_json,config_json,builtin,created_at,updated_at)
            VALUES ('fixture-local','fixture（本地 AI）','Ultralytics','local_yolo','使用本地权重预标注；类别名称及关键点顺序需与项目一致。',
            'local://ultralytics','missing.pt','["检测框"]','["auto"]','{"badge":"本地 AI"}',1,?,?)""", (now, now))
        conn.execute("""INSERT INTO jobs(project_id,model_id,mode,prompt,status,total,processed,error,progress_message,created_at)
            VALUES (?,'fixture-local','auto',?,'failed',1,0,?,'已处理 0/1',?)""",
                     (project_id, USER_TEXT, "checkpoint 无法加载：未安装 ultralytics", now))
        run_id = conn.execute("""INSERT INTO training_runs(project_id,version_id,name,model_arch,task,status,total_epochs,
            progress_message,error,created_by,created_at) VALUES (?,?,?,'yolo11n-cls.pt','classify','failed',10,
            'epoch 1/10 完成','checkpoint 无法加载：未安装 ultralytics','i18n-test',?)""", (project_id, version_id, USER_TEXT, now)).lastrowid
        conn.execute("""INSERT INTO training_run_logs(run_id,level,event,message,created_at)
            VALUES (?,'error','failed','checkpoint 无法加载：未安装 ultralytics',?)""", (run_id, now))
        conversion_id = conn.execute("""INSERT INTO model_conversions(training_run_id,format,status,error,created_by,created_at)
            VALUES (?,'onnx','failed','训练产物文件不存在','i18n-test',?)""", (run_id, now)).lastrowid
        conn.execute("""INSERT INTO conversion_logs(conversion_id,level,event,message,created_at)
            VALUES (?,'error','failed','训练产物文件不存在',?)""", (conversion_id, now))
    return block_id, run_id, conversion_id, device_id


def untranslated(page):
    return page.evaluate("""() => {
      const found = [], han = /[\\u3400-\\u9fff]/;
      const inspect = (text, node) => {
        if (!text || !han.test(text) || node.closest('script,style,option[lang]')) return;
        if (han.test(text.replaceAll('项目不存在', '').replaceAll('简体中文', ''))) found.push(text.trim());
      };
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) inspect(walker.currentNode.nodeValue, walker.currentNode.parentElement);
      for (const node of document.querySelectorAll('*')) {
        for (const attribute of ['title','placeholder','aria-label','aria-description','aria-valuetext','alt','data-label']) inspect(node.getAttribute(attribute), node);
      }
      for (const node of document.querySelectorAll('input:not([type=password]),textarea')) inspect(node.value, node);
      return [...new Set(found)];
    }""")


def run(chromium=None, screenshots=None):
    def capture(page, name):
        if screenshots:
            Path(screenshots).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(screenshots) / f"{name}.png"))

    with tempfile.TemporaryDirectory(prefix="cvsprout-i18n-") as temporary:
        folder = Path(temporary)
        listener = socket.socket(); listener.bind(("127.0.0.1", 0)); listener.listen(128)
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        env = {**os.environ, "MDP_DATA_DIR": str(folder / "data"), "MDP_MODELS_DIR": str(folder / "models"),
               "MDP_AUTH_MODE": "disabled", "MDP_EMBEDDED_WORKER": "false", "MDP_EMBEDDED_PIPELINE_WORKER": "false",
               "MDP_TRAINING_ENABLED": "false", "MDP_ENABLE_DEMO_MODEL": "false", "MDP_WORKER_URL": "",
               "MDP_WORKER_TOKEN": "", "MDP_BOOTSTRAP_API_KEY": "", "MDP_PUBLIC_ORIGIN": "", "CUDA_VISIBLE_DEVICES": "",
               "YOLO_OFFLINE": "true", "YOLO_AUTOINSTALL": "false", "YOLO_CONFIG_DIR": str(folder / "yolo")}
        with (folder / "server.log").open("w") as log:
            server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--fd", str(listener.fileno()),
                                       "--no-access-log", "--log-level", "warning"], cwd=ROOT, env=env,
                                      pass_fds=(listener.fileno(),), stdout=log, stderr=subprocess.STDOUT)
            listener.close()
            try:
                with httpx.Client(base_url=base, trust_env=False, headers={"X-MDP-Client": "browser", "X-Actor-ID": "i18n-test"}) as client:
                    deadline = time.monotonic() + 40
                    while True:
                        try:
                            if client.get("/api/health").status_code == 200: break
                        except httpx.TransportError:
                            pass
                        if time.monotonic() > deadline or server.poll() is not None:
                            raise RuntimeError((folder / "server.log").read_text())
                        time.sleep(.1)
                    project = client.post("/api/projects", json={"name": USER_TEXT, "description": USER_TEXT, "task_type": "detect", "labels": [USER_TEXT]}).json()
                    project_id = project["id"]
                    image = io.BytesIO(); Image.new("RGB", (320, 240), "#709975").save(image, "PNG")
                    upload = client.post(f"/api/projects/{project_id}/assets", files={"files": (f"{USER_TEXT}.png", image.getvalue(), "image/png")})
                    upload.raise_for_status()
                    block_id, run_id, conversion_id, device_id = seed_populated_states(client, folder, project_id)
                    for language in ["zh-CN", "en"]:
                        response = client.get("/api/projects/999999", headers={"Accept-Language": language})
                        assert response.headers["content-language"] == language
                        assert response.json()["detail"] == ("项目不存在或无权访问" if language == "zh-CN" else "Project does not exist or access denied")
                    def untranslated_metadata(value, path=""):
                        if isinstance(value, str) and re.search(r"[\u3400-\u9fff]", value.replace(USER_TEXT, "")):
                            return [(path, value)]
                        if isinstance(value, list):
                            return [item for index, child in enumerate(value) for item in untranslated_metadata(child, f"{path}/{index}")]
                        if isinstance(value, dict):
                            return [item for key, child in value.items() if key != "config" for item in untranslated_metadata(child, f"{path}/{key}")]
                        return []
                    for endpoint in ["/api/training/catalog", "/api/model-downloads", "/api/models",
                                     f"/api/projects/{project_id}/work-blocks", f"/api/training-runs/{run_id}/conversion-options",
                                     f"/api/projects/{project_id}/augmentation-options",
                                     f"/api/edge/devices/{device_id}/system-models",
                                     f"/api/training-runs/{run_id}/logs", f"/api/conversions/{conversion_id}/logs"]:
                        response = client.get(endpoint, headers={"Accept-Language": "en"})
                        response.raise_for_status()
                        assert not untranslated_metadata(response.json()), (endpoint, untranslated_metadata(response.json()))
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--no-proxy-server", "--no-zygote", "--disable-gpu", "--disable-software-rasterizer", "--use-gl=disabled"],
                                                 **({"executable_path": chromium} if chromium else {}))
                    errors, residual = [], {}
                    # A Chinese browser selecting English must not leak native captions.
                    context = browser.new_context(locale="zh-CN", viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
                    context.add_init_script("localStorage.setItem('cvsprout-locale', localStorage.getItem('cvsprout-locale') || 'en')")
                    page = context.new_page(); page.set_default_timeout(30000)
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(base); expect(page.locator("#new-project")).to_be_visible()
                    expect(page.locator("html")).to_have_attribute("lang", "en")
                    expect(page.locator(".dataset-project-card")).to_contain_text(USER_TEXT)
                    routes = ["/", "/models", "/users", "/devices", "/account", *[f"/projects/{project_id}/{section}" for section in ["overview", "data", "blocks", "ai", "versions", "train", "annotate"]]]
                    for route in routes:
                        print("Checking", route, flush=True)
                        page.goto(base + "#" + route)
                        try:
                            expect(page.locator("#app main, #canvas-scroll").first).to_be_visible(timeout=30000)
                            expect(page.locator("main[data-workspace-loading=true]")).to_have_count(0, timeout=30000)
                        except Exception:
                            print("DOM", page.evaluate("[...document.querySelectorAll('main')].map(x=>({cls:x.className, busy:x.dataset.workspaceLoading, app:!!x.closest('#app')}))"), "Browser errors", errors, "Body", page.locator("body").inner_text(), "Server", (folder / "server.log").read_text()[-5000:], flush=True)
                            raise
                        page.wait_for_timeout(600)
                        if route.endswith("/blocks"):
                            page.locator('[data-block-filter="all"]').click()
                            expect(page.locator(".work-block-identity").first).to_contain_text("Work block 001")
                        missing = untranslated(page)
                        if missing:
                            residual[route] = missing
                            print("UNTRANSLATED", route, missing, flush=True)
                        page.set_viewport_size({"width": 390, "height": 844})
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"Mobile overflow: {route}"
                        heading = page.locator("main .page-head h1").first
                        if heading.count():
                            assert heading.bounding_box()["height"] < 150, f"English heading squeezed into a narrow column: {route}"
                        page.set_viewport_size({"width": 1440, "height": 1000})

                    def inspect(label):
                        missing = untranslated(page)
                        if missing: residual[label] = missing

                    # Both a current backend and one returning canonical Chinese
                    # must render the same English populated states and dialogs.
                    for backend in ["current", "legacy"]:
                        if backend == "legacy":
                            context.route("**/api/**", lambda route: route.continue_(headers={**route.request.headers, "accept-language": "zh-CN"}))
                        print("Checking populated workflows:", backend, flush=True)
                        page.goto(base + "#/models")
                        expect(page.locator('[data-service-model="fixture-local"]')).to_contain_text("fixture (Local AI)")
                        expect(page.locator('[data-service-model="fixture-custom"]')).to_contain_text("Custom")
                        inspect(backend + "/models")
                        page.get_by_role("button", name="Import training weights", exact=True).click()
                        expect(page.locator("[data-file-selection]")).to_have_text("No file selected")
                        page.locator("#checkpoint-file").set_input_files({"name": USER_TEXT + ".pt", "mimeType": "application/octet-stream", "buffer": b"fixture"})
                        expect(page.locator("[data-file-selection]")).to_have_text(USER_TEXT + ".pt")
                        inspect(backend + "/file-picker")
                        page.locator("#modal-cancel").click()
                        page.locator("#new-model").click()
                        expect(page.locator("#model-provider")).to_be_visible()
                        inspect(backend + "/model-configuration")
                        page.locator("#modal-cancel").click()
                        page.goto(base + "#/devices")
                        expect(page.locator(f'[data-edge-settings="{device_id}"]')).to_be_visible()
                        page.locator(f'[data-edge-settings="{device_id}"]').click()
                        expect(page.locator("#modal-title")).to_be_visible()
                        inspect(backend + "/device-settings")
                        page.locator("#modal-cancel").click()
                        page.locator(f'[data-edge-captures="{device_id}"]').click()
                        expect(page.locator("#edge-record-page")).to_be_visible()
                        inspect(backend + "/device-records")
                        page.locator("#modal-cancel").click()
                        page.goto(base + f"#/projects/{project_id}/blocks")
                        expect(page.locator('[data-block-filter="all"]')).to_be_visible()
                        page.locator('[data-block-filter="all"]').click()
                        expect(page.locator(".work-block-identity")).to_contain_text("Work block 001")
                        inspect(backend + "/blocks")
                        page.goto(base + f"#/projects/{project_id}/annotate?block={block_id}")
                        expect(page.locator(".annotation-block-scope")).to_contain_text("Work block 001")
                        inspect(backend + "/block-annotation")
                        page.goto(base + f"#/projects/{project_id}/ai?tab=history")
                        expect(page.locator("main[data-workspace-loading=true]")).to_have_count(0)
                        page.wait_for_timeout(500)
                        inspect(backend + "/job-history")
                        page.goto(base + f"#/projects/{project_id}/train?run={run_id}&tab=details")
                        expect(page.locator('[data-section-tab="train:details"]')).to_be_visible()
                        page.locator('[data-section-tab="train:details"]').click()
                        expect(page.locator('[data-training-log-detail]')).to_be_visible()
                        page.locator('[data-training-log-detail] > summary').click()
                        expect(page.locator(".log-line code").first).to_contain_text("Ultralytics not installed")
                        inspect(backend + "/training-logs")
                        page.locator('[data-section-tab="train:conversions"]').click()
                        expect(page.locator(f'[data-conversion-logs="{conversion_id}"]')).to_be_visible()
                        page.locator(f'[data-conversion-logs="{conversion_id}"]').click()
                        expect(page.locator("#conversion-log-modal .log-line")).to_be_visible()
                        inspect(backend + "/conversion-logs")
                        page.locator("#modal-cancel").click()
                    context.unroute("**/api/**")

                    # Legacy administrator accounts store the default name in
                    # Chinese. Check the initial session (which bypasses api()),
                    # profile and member forms without changing persisted names.
                    for language, name in [("en", "Administrator"), ("zh-CN", "管理员")]:
                        identity = browser.new_context(locale="zh-CN", reduced_motion="reduce")
                        account = {"id": 1, "user_id": 1, "username": "admin-fixture", "display_name": "管理员",
                                   "role": "admin", "enabled": True, "must_change_password": False, "created_at": "2026-01-01T00:00:00Z"}
                        saved_names = []

                        def profile_response(route):
                            if route.request.method == "PATCH":
                                saved_names.append(route.request.post_data_json["display_name"])
                            route.fulfill(json=account)

                        identity.route("**/api/health", lambda route: route.fulfill(json={"auth_mode": "accounts", "setup_required": False}))
                        identity.route("**/api/session", lambda route: route.fulfill(json={**account, "actor": "user:admin-fixture"}))
                        identity.route("**/api/account", profile_response)
                        identity.route("**/api/account/sessions", lambda route: route.fulfill(json={"sessions": []}))
                        identity.route("**/api/account/events?*", lambda route: route.fulfill(json={"events": []}))
                        identity.route("**/api/users?*", lambda route: route.fulfill(json={"items": [account], "total": 1, "enabled_admin_count": 1}))
                        profile = identity.new_page()
                        profile.on("pageerror", lambda error: errors.append(str(error)))
                        profile.goto(base + f"?lang={language}#/account")
                        expect(profile.locator(".navigation-identity strong")).to_have_text(name)
                        expect(profile.locator("#profile-name")).to_have_value(name)
                        profile.locator('#profile-form button[type="submit"]').click()
                        expect(profile.locator('#profile-form button[type="submit"]')).to_be_enabled()
                        assert saved_names == ["管理员"], saved_names
                        profile.goto(base + f"?lang={language}#/users")
                        expect(profile.locator(".member-table tbody tr strong").first).to_have_text(name)
                        profile.locator('[data-edit-user="1"]').click()
                        expect(profile.locator("#modal-title")).to_contain_text(name)
                        expect(profile.locator("#user-display")).to_have_value(name)
                        profile.locator("#modal-submit").click()
                        expect(profile.locator("#user-display")).to_have_count(0)
                        if language == "en":
                            assert not untranslated(profile), untranslated(profile)
                        identity.close()

                    page.goto(base); expect(page.locator("#new-project")).to_be_visible()
                    page.locator("#new-project").click()
                    for task in ["detect", "segment", "classify", "pose"]:
                        page.locator("#project-task").select_option(task)
                        missing = untranslated(page)
                        if missing: residual[f"new-project/{task}"] = missing
                    page.locator("#project-name").fill(USER_TEXT)
                    # Switch requests must preserve drafts and keep the language.
                    page.evaluate("document.querySelector('[data-language-select]').value='zh-CN'; document.querySelector('[data-language-select]').dispatchEvent(new Event('change',{bubbles:true}))")
                    expect(page.locator("#project-name")).to_have_value(USER_TEXT)
                    expect(page.locator("html")).to_have_attribute("lang", "en")
                    page.locator("#modal-cancel").click()
                    page.goto(base + f"#/projects/{project_id}/data")
                    expect(page.locator("#data-search")).to_be_visible()
                    language = page.get_by_role("combobox", name="Language", exact=True)
                    expect(language).to_be_visible()
                    assert language.evaluate("el => !el.closest('details')")
                    language.focus(); expect(language).to_be_focused()
                    capture(page, "workspace-language")
                    # The icon remains usable when the desktop sidebar is collapsed.
                    page.locator('.navigation-collapse').click()
                    expect(page.locator('.workspace-navigation')).to_have_class(re.compile("is-collapsed"))
                    language.focus(); expect(language).to_be_focused()
                    capture(page, "workspace-collapsed")
                    language.select_option('zh-CN')
                    expect(page.locator("html")).to_have_attribute("lang", "zh-CN")
                    expect(page.locator("#data-search")).to_have_attribute("placeholder", "按文件名搜索")
                    assert page.url.endswith(f"#/projects/{project_id}/data")
                    page.reload(); expect(page.locator("#data-search")).to_have_attribute("placeholder", "按文件名搜索")
                    assert page.evaluate("localStorage.getItem('cvsprout-locale')") == "zh-CN"
                    # Mobile restores the full selector, even with a saved compact sidebar.
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.locator('.navigation-toggle').click()
                    expect(page.locator('#navigation-language-select')).to_be_visible()
                    assert page.locator('#navigation-language-select').evaluate("el => getComputedStyle(el).opacity") == "1"
                    capture(page, "workspace-mobile")
                    page.locator('#navigation-language-select').select_option('en')
                    expect(page.locator("html")).to_have_attribute("lang", "en")
                    expect(page.locator("#data-search")).to_have_attribute("placeholder", "Search by filename")
                    page.set_viewport_size({"width": 1440, "height": 1000})

                    # Authentication and first-run setup are available before a session.
                    for mode in ["login", "setup"]:
                        auth = browser.new_page(locale="en-US", reduced_motion="reduce")
                        auth.on("pageerror", lambda error: errors.append(str(error)))
                        health = {"auth_mode": "accounts", "setup_required": mode == "setup"}
                        auth.route("**/api/health", lambda route: route.fulfill(json=health))
                        auth.route("**/api/session", lambda route: route.fulfill(status=401, json={"detail": "Login required"}))
                        auth.goto(base)
                        expect(auth.locator("#setup-form" if mode == "setup" else "#login-form")).to_be_visible()
                        missing = untranslated(auth)
                        if missing: residual[mode] = missing
                        if mode == "login":
                            language = auth.get_by_role("combobox", name="Language", exact=True)
                            expect(language).to_be_visible()
                            assert language.evaluate("el => !el.closest('details')")
                            capture(auth, "login-desktop")
                            for width in [320, 390, 800]:
                                auth.set_viewport_size({"width": width, "height": 844})
                                brand = auth.locator('.auth-brand').bounding_box()
                                utilities = auth.locator('.auth-utilities').bounding_box()
                                assert brand['y'] + brand['height'] <= utilities['y']
                                assert auth.evaluate("document.documentElement.scrollWidth <= innerWidth")
                                auth.locator('.auth-appearance > summary').click()
                                popover = auth.locator('.auth-appearance .studio-popover').bounding_box()
                                assert popover['x'] >= 0 and popover['x'] + popover['width'] <= width
                                expect(auth.locator('.auth-appearance [data-language-select]')).to_have_count(0)
                                auth.keyboard.press('Escape')
                                capture(auth, f"login-mobile-{width}")
                            # Switching works before login and keeps appearance independent.
                            language.select_option('zh-CN')
                            expect(auth.locator("html")).to_have_attribute("lang", "zh-CN")
                            auth.locator('#auth-language-select').select_option('en')
                            expect(auth.locator("html")).to_have_attribute("lang", "en")
                            auth.set_viewport_size({"width": 1280, "height": 900})
                            auth.route("**/api/login", lambda route: route.fulfill(status=401, json={"detail": "账号或密码不正确，或账号已停用"}))
                            auth.locator("#login-username").fill("i18n-user")
                            auth.locator("#login-password").fill("ExamplePassword123")
                            auth.locator(".auth-submit").click()
                            expect(auth.locator("#login-feedback")).to_be_visible()
                            missing = untranslated(auth)
                            if missing: residual["legacy-login-error"] = missing
                            auth.locator('[data-auth-mode="register"]').click()
                            expect(auth.locator("#register-form")).to_be_visible()
                            missing = untranslated(auth)
                            if missing: residual["registration"] = missing
                            auth.route("**/api/register", lambda route: route.fulfill(status=201, json={"username": "i18n-user", "status": "pending"}))
                            auth.locator(".auth-note-details > summary").click()
                            for field, value in {"username": "i18n-user", "display": USER_TEXT, "password": "ExamplePassword123", "confirm": "ExamplePassword123", "note": USER_TEXT}.items():
                                auth.locator(f'#register-{field}').fill(value)
                            auth.locator(".auth-submit").click()
                            expect(auth.locator(".auth-receipt")).to_be_visible()
                            missing = untranslated(auth)
                            if missing: residual["registration-receipt"] = missing
                        else:
                            auth.route("**/api/setup", lambda route: route.fulfill(status=403, json={"detail": "初始化口令不正确，请查看启动终端。"}))
                            for field, value in {"code": "i18n-test-code-123", "username": "i18n-user", "password": "ExamplePassword123", "confirm": "ExamplePassword123"}.items():
                                auth.locator(f'[name="{field}"]').fill(value)
                            auth.locator("#setup-form button").click()
                            expect(auth.locator('#setup-form [role="status"]')).to_contain_text(re.compile("incorrect", re.I))
                            missing = untranslated(auth)
                            if missing: residual["legacy-setup-error"] = missing
                        auth.close()
                    for width in [390, 1440]:
                        page.goto(base + "?lang=en")
                        expect(page.locator("#new-project")).to_be_visible()
                        page.set_viewport_size({"width": width, "height": 1000})
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"Overflow at {width}"
                    browser.close()
                    assert not errors, errors
                    assert not residual, residual
                    print("PASS English coverage across 12 populated routes, model and file dialogs, work-block annotation, job history, training/conversion logs with current and legacy responses, login, registration and setup; Chinese switching, persistence, draft protection and user data preservation.")
            finally:
                server.terminate()
                try: server.wait(timeout=10)
                except subprocess.TimeoutExpired: server.kill(); server.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--chromium"); parser.add_argument("--screenshots")
    args = parser.parse_args()
    run(args.chromium, args.screenshots)
