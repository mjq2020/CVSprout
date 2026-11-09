"""Exercise studio interactions against an isolated database with real Chromium.

Usage: .venv/bin/python scripts/smoke_studio_browser.py --chromium /path/to/chrome
No production projects, accounts or model workers are used.
"""
from __future__ import annotations

import argparse
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


def run(chromium: str | None, output: Path):
    errors = []
    with tempfile.TemporaryDirectory(prefix="cvsprout-studio-") as temporary:
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
                with httpx.Client(base_url=base, trust_env=False, headers={"X-MDP-Client": "browser", "X-Actor-ID": "studio-test"}) as client:
                    deadline = time.monotonic() + 30
                    while True:
                        try:
                            if client.get("/api/health").status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        if process.poll() is not None or time.monotonic() > deadline:
                            raise RuntimeError("Isolated server did not start")
                        time.sleep(.05)
                    for name, task in [("工件 <A> & 检测", "detect"), ("超长项目名称用于验证布局" * 7, "detect"), ("植株分割", "segment")]:
                        response = client.post("/api/projects", json={"name": name, "task_type": task, "labels": ["object"]})
                        response.raise_for_status()
                with sync_playwright() as pw:
                    flags = ["--no-sandbox", "--no-proxy-server", "--no-zygote"]
                    flags += ["--disable-gpu", "--disable-software-rasterizer", "--use-gl=disabled"]
                    browser = pw.chromium.launch(headless=True, args=flags, **({"executable_path": chromium} if chromium else {}))
                    page = browser.new_page(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
                    page.set_default_timeout(10000)
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    page.goto(base)
                    cards = page.locator(".dataset-project-card")
                    expect(cards).to_have_count(3)
                    expect(page.locator(".project-options[open]")).to_have_count(0)
                    expect(page.locator(".project-delete-action:visible")).to_have_count(0)
                    page.locator(".library-filter > summary").click()
                    page.locator('[data-task-filter="detect"]').click()
                    expect(cards).to_have_count(2)
                    expect(page.locator("#project-filter-status")).to_be_visible()
                    page.keyboard.press("Escape")
                    expect(page.locator(".library-filter > summary")).to_be_focused()
                    page.locator("#clear-project-filters").click()
                    expect(cards).to_have_count(3)
                    expect(page.locator("#project-search")).to_be_focused()
                    page.locator("#project-search").fill("不存在的项目")
                    expect(cards).to_have_count(0)
                    page.locator("#reset-project-filters").click()
                    expect(cards).to_have_count(3)
                    print("PASS search, task filters, reset, Escape and keyboard focus", flush=True)

                    card = cards.filter(has_text="工件 <A> & 检测")
                    card.locator("summary").click()
                    card.locator("[data-delete-project]").click()
                    expect(page.locator("#modal-submit")).to_be_disabled()
                    page.locator("#modal-cancel").click()
                    expect(card.locator("summary")).to_be_focused()
                    expect(card.locator("details")).not_to_have_attribute("open", "")
                    card.locator("summary").press("Enter")
                    card.locator("[data-archive-project]").click()
                    expect(cards).to_have_count(2)
                    page.locator(".library-filter > summary").click()
                    page.locator("#show-archived-projects").check()
                    expect(cards).to_have_count(1)
                    expect(page.locator("h1")).to_have_text("归档项目")
                    cards.first.locator("summary").click()
                    cards.first.locator("[data-archive-project]").click()
                    expect(cards).to_have_count(0)
                    page.locator(".library-filter > summary").click()
                    page.locator("#show-archived-projects").uncheck()
                    expect(cards).to_have_count(3)
                    print("PASS escaped titles, action menu, modal return focus, archive and restore", flush=True)

                    for width in [320, 390, 900, 1024, 1440]:
                        page.set_viewport_size({"width": width, "height": 1000})
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"Document overflow at {width}"
                        assert page.locator("main").evaluate("el => el.scrollWidth <= el.clientWidth + 1"), f"Workspace overflow at {width}"
                        page.locator(".library-filter > summary").click()
                        box = page.locator(".library-filter .studio-popover").bounding_box()
                        assert box and box["x"] >= 0 and box["x"] + box["width"] <= width, f"Filter overflow at {width}"
                        page.keyboard.press("Escape")
                    for theme in ["light", "dark"]:
                        page.evaluate("theme => window.CVSproutTheme.setPreference(theme)", theme)
                        page.screenshot(path=str(output / f"projects-{theme}.png"))
                    page.evaluate("window.CVSproutTheme.setPreference('light')")
                    cards.filter(has_text="工件 <A> & 检测").locator(".project-title-row a").click()
                    expect(page.locator(".workflow-surface")).to_be_visible()
                    expect(page.locator(".overview-training")).to_be_hidden()
                    expect(page.locator(".workflow-funnel")).to_have_count(0)
                    page.screenshot(path=str(output / "empty-project.png"))
                    print("PASS responsive layout, long names, themes and empty overview", flush=True)

                    auth = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1.5)
                    auth.set_default_timeout(10000)
                    auth.on("pageerror", lambda e: errors.append(str(e)))
                    auth.add_init_script("""localStorage.setItem('cvsprout-login-appearance', JSON.stringify({style: 'flow', paused: true}));
                        window.visionPaints = 0;
                        const clear = CanvasRenderingContext2D.prototype.clearRect;
                        CanvasRenderingContext2D.prototype.clearRect = function(...args) {
                            window.visionPaints++; return clear.apply(this, args);
                        };""")
                    auth.route("**/api/health", lambda r: r.fulfill(json={"auth_mode": "accounts", "setup_required": False}))
                    auth.route("**/api/session", lambda r: r.fulfill(status=401, json={"detail": "login required"}))
                    auth_requests = []
                    auth.on("request", lambda request: auth_requests.append(request.url))
                    auth.goto(base)
                    expect(auth.locator("#login-form")).to_be_visible()
                    expect(auth.locator(".vision-ready")).to_have_count(1)
                    assert len([url for url in auth_requests if "/static/app.js?" in url]) == 1, "Only one application entry may load"
                    assert len([url for url in auth_requests if url.endswith("/api/session")]) == 1, "Login must initialize exactly once"
                    expect(auth.locator(".vision-scene")).to_have_attribute("data-motion", "running")
                    expect(auth.locator("[data-vision-style-choice], [data-vision-pause]")).to_have_count(0)
                    before = auth.locator(".vision-scene").screenshot()
                    auth.wait_for_timeout(250)
                    assert before != auth.locator(".vision-scene").screenshot(), "Scene must animate without interaction or saved preferences"
                    auth.mouse.move(500, 550)
                    auth.reload()
                    expect(auth.locator(".vision-scene")).to_have_attribute("data-motion", "running")
                    auth.locator("#login-username").focus()
                    expect(auth.locator(".vision-scene")).to_have_attribute("data-motion", "running")
                    paints = auth.evaluate("window.visionPaints")
                    auth.wait_for_timeout(180)
                    assert auth.evaluate("window.visionPaints") > paints, "Focused or autofilled fields must not freeze motion"
                    auth.locator("#login-username").fill("studio-user")
                    expect(auth.locator(".vision-scene")).to_have_attribute("data-motion", "editing")
                    paints = auth.evaluate("window.visionPaints")
                    auth.wait_for_timeout(180)
                    assert auth.evaluate("window.visionPaints") == paints, "Typing must not compete with scene painting"
                    expect(auth.locator(".vision-scene")).to_have_attribute("data-motion", "running")
                    expect(auth.locator("#login-username")).to_be_focused()
                    auth.locator("#login-password").fill("example-password")
                    auth.locator("#login-form").evaluate("form => form.dataset.retained = 'yes'")
                    backgrounds = set()
                    for style in ["forest", "ocean", "sand"]:
                        auth.locator(".auth-appearance > summary").click()
                        auth.locator("[data-style-select]").select_option(style)
                        expect(auth.locator("html")).to_have_attribute("data-style", style)
                        expect(auth.locator("#login-form")).to_have_attribute("data-retained", "yes")
                        expect(auth.locator("#login-username")).to_have_value("studio-user")
                        expect(auth.locator("#login-password")).to_have_value("example-password")
                        for theme in ["light", "dark"]:
                            auth.locator("[data-theme-select]").select_option(theme)
                            expect(auth.locator("html")).to_have_attribute("data-theme", theme)
                            auth.screenshot(path=str(output / f"login-{style}-{theme}.png"))
                        auth.keyboard.press("Escape")
                        expect(auth.locator(".auth-appearance > summary")).to_be_focused()
                        backgrounds.add(auth.locator(".auth-intro").evaluate("el => getComputedStyle(el).backgroundColor"))
                        expect(auth.locator(".vision-scene")).to_have_count(1)
                    assert len(backgrounds) == 3, "Each global style must have a distinct palette"
                    auth.reload()
                    expect(auth.locator("html")).to_have_attribute("data-style", "sand")
                    expect(auth.locator("html")).to_have_attribute("data-theme", "dark")
                    expect(auth.locator(".vision-scene")).to_have_attribute("data-motion", "running")
                    auth.locator(".auth-appearance > summary").click()
                    expect(auth.locator("[data-style-select]")).to_have_value("sand")
                    auth.locator("[data-theme-select]").select_option("light")
                    auth.locator("[data-style-select]").select_option("forest")
                    auth.keyboard.press("Escape")
                    auth.emulate_media(reduced_motion="reduce")
                    expect(auth.locator(".vision-scene")).to_have_attribute("data-motion", "reduced")
                    auth.wait_for_timeout(100)
                    still = auth.locator(".vision-scene").screenshot()
                    auth.mouse.move(300, 650)
                    assert still == auth.locator(".vision-scene").screenshot(), "Reduced motion must ignore pointer rotation"
                    auth.screenshot(path=str(output / "login.png"))
                    print("PASS single animation, focused-input autoplay, typing recovery, three global styles in light/dark, persistence and form retention", flush=True)
                    auth.locator(".auth-submit").click()
                    expect(auth.locator("#login-username")).to_have_attribute("aria-invalid", "true")
                    auth.locator("#login-username").fill("studio-user")
                    auth.locator("#login-password").fill("example-password")
                    auth.locator("[data-password-toggle]").click()
                    expect(auth.locator("#login-password")).to_have_attribute("type", "text")
                    auth.locator("#auth-register-tab").click()
                    expect(auth.locator("#register-username")).to_have_value("studio-user")
                    auth.locator("#auth-register-tab").press("ArrowLeft")
                    expect(auth.locator("#auth-login-tab")).to_be_focused()
                    auth.set_viewport_size({"width": 390, "height": 844})
                    expect(auth.locator(".vision-scene")).to_be_hidden()
                    expect(auth.locator(".vision-scene")).to_have_attribute("data-motion", "hidden")
                    expect(auth.locator("[data-vision-style-choice], [data-vision-pause]")).to_have_count(0)
                    assert auth.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    auth.locator(".auth-appearance > summary").click()
                    auth.locator("[data-style-select]").select_option("ocean")
                    box = auth.locator(".auth-appearance .studio-popover").bounding_box()
                    assert box and box["x"] >= 0 and box["x"] + box["width"] <= 390
                    auth.keyboard.press("Escape")
                    auth.screenshot(path=str(output / "login-mobile.png"))
                    auth.set_viewport_size({"width": 1440, "height": 1000})
                    auth.emulate_media(reduced_motion="no-preference")
                    expect(auth.locator(".vision-scene")).to_have_attribute("data-motion", "running")
                    for _ in range(3):
                        auth.locator("#auth-register-tab").click()
                        auth.locator("#auth-login-tab").click()
                    auth.locator("#auth-login-tab").focus()
                    expect(auth.locator(".vision-scene")).to_have_attribute("data-motion", "running")
                    paints = auth.evaluate("window.visionPaints")
                    auth.wait_for_timeout(400)
                    assert 2 <= auth.evaluate("window.visionPaints") - paints <= 16, "Only one capped scene loop may remain"
                    auth.evaluate("Object.defineProperty(document, 'hidden', {configurable: true, value: true}); document.dispatchEvent(new Event('visibilitychange'))")
                    expect(auth.locator(".vision-scene")).to_have_attribute("data-motion", "hidden")
                    paints = auth.evaluate("window.visionPaints")
                    auth.wait_for_timeout(180)
                    assert auth.evaluate("window.visionPaints") == paints
                    auth.evaluate("delete document.hidden; document.dispatchEvent(new Event('visibilitychange'))")
                    expect(auth.locator(".vision-scene")).to_have_attribute("data-motion", "running")
                    print("PASS mobile-to-desktop resize, hidden-page pause and repeated scene cleanup", flush=True)
                    # The same preference continues into real workspace routes, independent of auth.
                    auth.unroute("**/api/health")
                    auth.unroute("**/api/session")
                    auth.reload()
                    expect(auth.locator(".dataset-project-card")).to_have_count(3)
                    expect(auth.locator("html")).to_have_attribute("data-style", "ocean")
                    for style in ["forest", "ocean", "sand"]:
                        auth.locator('[data-navigation-menu="theme"] > summary').click()
                        auth.locator("[data-style-select]").select_option(style)
                        auth.keyboard.press("Escape")
                        expect(auth.locator("html")).to_have_attribute("data-style", style)
                        auth.screenshot(path=str(output / f"workspace-{style}.png"))
                    auth.locator(".project-title-row a").first.click()
                    expect(auth.locator(".workflow-surface")).to_be_visible()
                    expect(auth.locator("html")).to_have_attribute("data-style", "sand")
                    print("PASS appearance shared between login, workspace and project routes", flush=True)
                    assert not errors, errors
                    fallback = browser.new_page(viewport={"width": 1440, "height": 1000})
                    fallback.on("pageerror", lambda e: errors.append(str(e)))
                    fallback.add_init_script("HTMLCanvasElement.prototype.getContext = () => null")
                    fallback.route("**/api/health", lambda r: r.fulfill(json={"auth_mode": "accounts", "setup_required": False}))
                    fallback.route("**/api/session", lambda r: r.fulfill(status=401, json={"detail": "login required"}))
                    fallback.goto(base)
                    expect(fallback.locator("#login-form")).to_be_visible()
                    expect(fallback.locator(".vision-fallback")).to_be_visible()
                    expect(fallback.locator(".vision-scene")).to_have_attribute("data-motion", "unavailable")
                    fallback.locator(".auth-submit").click()
                    expect(fallback.locator("#login-username")).to_have_attribute("aria-invalid", "true")
                    storage_blocked = browser.new_page(viewport={"width": 1440, "height": 1000})
                    storage_blocked.on("pageerror", lambda e: errors.append(str(e)))
                    storage_blocked.add_init_script("Object.defineProperty(window, 'localStorage', {get() { throw new Error('storage disabled'); }})")
                    storage_blocked.route("**/api/health", lambda r: r.fulfill(json={"auth_mode": "accounts", "setup_required": False}))
                    storage_blocked.route("**/api/session", lambda r: r.fulfill(status=401, json={"detail": "login required"}))
                    storage_blocked.goto(base)
                    expect(storage_blocked.locator("#login-form")).to_be_visible()
                    expect(storage_blocked.locator(".vision-scene")).to_have_attribute("data-motion", "running")
                    storage_blocked.locator(".auth-appearance > summary").click()
                    storage_blocked.locator("[data-style-select]").select_option("ocean")
                    storage_blocked.keyboard.press("Escape")
                    expect(storage_blocked.locator("html")).to_have_attribute("data-style", "ocean")
                    storage_blocked.locator("#auth-register-tab").click()
                    expect(storage_blocked.locator("#register-form")).to_be_visible()
                    assert not errors, errors
                    print("PASS login validation, password toggle, auth tabs, mobile, 3D interaction, reduced motion and no-canvas fallback", flush=True)
                    browser.close()
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium")
    parser.add_argument("--output", type=Path, default=Path(tempfile.mkdtemp(prefix="cvsprout-studio-results-")))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    run(args.chromium, args.output)
    print(f"Screenshots: {args.output}")
