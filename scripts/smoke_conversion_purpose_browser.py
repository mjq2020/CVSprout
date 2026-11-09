"""Check real conversion controls in Chromium using an isolated static fixture.

No backend, database, production service or device is accessed. The actual
conversion-ui.js and styles are served on a temporary loopback port. Screenshots
and a JSON report go to --output-dir (a temporary directory by default).
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

REPO = Path(__file__).resolve().parents[1]
HTML = """<!doctype html><html lang="zh-CN" data-theme="light"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Conversion purpose browser fixture</title>
<link rel="stylesheet" href="/static/styles.css"><link rel="stylesheet" href="/static/training-guidance.css">
<link rel="stylesheet" href="/static/themes.css">
<style>html,body{height:auto;min-height:100%;overflow:visible}.fixture{max-width:1000px;margin:24px auto;padding:24px;background:var(--panel);border:1px solid var(--line);border-radius:12px}.fixture-footer{padding-top:20px}h1{font-size:20px}</style>
</head><body><main class="fixture"><h1>转换并导出模型 · 隔离浏览器验收</h1>
<div id="fixture-host"></div><div class="fixture-footer"><button class="btn primary" id="fixture-submit">开始转换</button></div></main>
<script type="module">
import { mountConversionForm } from '/static/conversion-ui.js';
const esc = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
window.mountFixture = fixture => {
  const root = document.createElement('div');
  document.querySelector('#fixture-host').replaceChildren(root);
  const submit = document.querySelector('#fixture-submit');
  window.submissions = [];
  window.conversionForm = mountConversionForm(root, {...fixture, escapeHtml:esc,
    onValidityChange:valid => { submit.disabled = !valid; }});
  submit.onclick = () => window.submissions.push(window.conversionForm.payload());
};
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            content, content_type = HTML.encode(), "text/html; charset=utf-8"
        elif path in {"/static/conversion-ui.js", "/static/styles.css", "/static/training-guidance.css", "/static/themes.css"}:
            content = (REPO / path.lstrip("/")).read_bytes()
            content_type = "text/javascript" if path.endswith(".js") else "text/css"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *_args):
        pass


def fixture(architecture, capability):
    options = {"task": "detect", "model_backend": "yolo", "defaults": {"image_size": 640},
               "formats": {"onnx": {"available": True}, "rknn": {"available": True}},
               "calibration": {"available_count": 12, "default_count": 12}}
    if capability is not None:
        options["system_model"] = {"source_model_arch": architecture, **capability}
    return {"run": {"id": 1, "name": "Controlled fixture", "task": "detect", "model_arch": architecture},
            "options": options, "resources": {"devices": [{"id": "cpu", "available": True, "type": "cpu"}]}}


def run(chromium, output):
    checks, errors, rejected_requests = [], [], []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        with sync_playwright() as pw:
            launch = {"headless": True, "args": ["--no-sandbox", "--disable-gpu", "--no-zygote",
                      "--disable-software-rasterizer", "--use-gl=disabled", "--no-proxy-server"]}
            if chromium:
                launch["executable_path"] = str(chromium)
            browser = pw.chromium.launch(**launch)
            context = browser.new_context(viewport={"width": 1280, "height": 1050})
            page = context.new_page()
            page.set_default_timeout(10000)
            page.on("pageerror", lambda error: errors.append(str(error)))

            def local_only(route):
                if not route.request.url.startswith(origin + "/"):
                    rejected_requests.append(route.request.url)
                    route.abort()
                else:
                    route.continue_()

            context.route("**/*", local_only)
            try:
                page.goto(origin)
                page.wait_for_function("typeof mountFixture === 'function'")
                profile = page.locator("#convert-rknn-profile")
                submit = page.locator("#fixture-submit")

                def mount(architecture, capability):
                    page.evaluate("value=>mountFixture(value)", fixture(architecture, capability))
                    page.locator('input[name="convert-format"][value="rknn"]').check()
                    expect(submit).to_be_enabled()
                    expect(profile).to_have_attribute("aria-describedby", "convert-rknn-profile-help")

                def system_settings():
                    for field, value in (("target", "rv1126b"), ("quantized", "true"),
                                         ("mean", "0, 0, 0"), ("std", "255, 255, 255")):
                        expect(page.locator(f"#convert-rknn-{field}")).to_have_value(value)
                        expect(page.locator(f"#convert-rknn-{field}")).to_be_disabled()

                reason = "当前模型为 yolo26n.pt；YOLO26 输出与设备系统后处理不兼容，请使用 YOLOv8 或 YOLO11。"
                mount("yolo26n.pt", {"available": False, "reason": reason})
                expect(profile.locator('option[value="recamera_system"]')).to_be_enabled()
                expect(profile.locator('option[value="recamera_system"]')).to_contain_text("当前模型不兼容")
                page.locator("#convert-rknn-target").select_option("rk3576")
                page.locator(".conversion-advanced summary").click()
                page.locator("#convert-rknn-mean").fill("1, 2, 3")
                page.locator("#convert-rknn-std").fill("100, 110, 120")
                profile.select_option("recamera_system")
                expect(profile).to_have_value("recamera_system")
                expect(page.locator("#convert-rknn-profile-help")).to_contain_text(reason)
                expect(page.locator("#convert-rknn-profile-help")).to_contain_text("当前不可转换")
                expect(page.locator("#conversion-validation")).to_contain_text(reason)
                system_settings()
                expect(submit).to_be_disabled()
                page.evaluate("document.querySelector('#fixture-submit').click()")
                assert page.evaluate("submissions.length") == 0
                assert page.evaluate("() => {try {conversionForm.payload();return false;} catch (_) {return true;}}")
                page.screenshot(path=str(output / "yolo26-system-purpose-blocked.png"), full_page=True)
                checks.append("YOLO26 native option can be selected, shows exact model/reason and blocks both submit and payload")

                profile.select_option("generic")
                expect(submit).to_be_enabled()
                submit.click()
                restored = page.evaluate("submissions.at(-1).rknn")
                assert restored["deployment_profile"] == "generic" and restored["target_platform"] == "rk3576"
                assert restored["quantized"] is False and restored["mean_values"] == [1, 2, 3] and restored["std_values"] == [100, 110, 120]
                checks.append("Returning to generic restores custom chip, precision and normalization, and permits its valid payload")

                for architecture in ("yolov8n.pt", "yolo11n.pt"):
                    mount(architecture, {"available": True, "reason": "当前模型支持系统转换。"})
                    profile.select_option("recamera_system")
                    system_settings()
                    expect(page.locator("#convert-rknn-profile-help")).to_contain_text(architecture)
                    expect(submit).to_be_enabled()
                    submit.click()
                    payload = page.evaluate("submissions.at(-1)")
                    assert payload["batch"] == 1 and payload["device"] == "cpu" and payload["half"] is False and payload["dynamic"] is False
                    assert payload["rknn"]["deployment_profile"] == "recamera_system" and payload["rknn"]["target_platform"] == "rv1126b"
                    assert payload["rknn"]["quantized"] is True and payload["rknn"]["mean_values"] == [0, 0, 0] and payload["rknn"]["std_values"] == [255, 255, 255]
                    checks.append(architecture + " submits the fixed system-model payload through real controls")
                page.screenshot(path=str(output / "yolo11-system-purpose-ready.png"), full_page=True)

                for capability in (None, {"available": None}, {"available": "true"}):
                    mount("unreported", capability)
                    profile.select_option("recamera_system")
                    expect(submit).to_be_disabled()
                    expect(page.locator("#convert-rknn-profile-help")).to_contain_text("服务未提供系统模型兼容性结论")
                    assert page.evaluate("() => {try {conversionForm.payload();return false;} catch (_) {return true;}}")
                page.screenshot(path=str(output / "unknown-system-purpose-blocked.png"), full_page=True)
                checks.append("Missing, null and nonboolean capability fail closed while the profile remains inspectable")
                assert not errors, errors
                assert not rejected_requests, "Fixture attempted unexpected external requests"
            except Exception:
                page.screenshot(path=str(output / "failure.png"), full_page=True)
                raise
            finally:
                context.close()
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return {"passed": True, "checks": checks, "page_errors": errors,
            "scope": "Real Chromium, repository conversion renderer and styles; isolated static fixture, no backend/database/device"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chromium", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output = (args.output_dir or Path(tempfile.mkdtemp(prefix="cvsprout-conversion-purpose-"))).resolve()
    output.mkdir(parents=True, exist_ok=True)
    print("Artifacts: " + str(output), flush=True)
    result = run(args.chromium, output)
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    for check in result["checks"]:
        print("PASS " + check, flush=True)
