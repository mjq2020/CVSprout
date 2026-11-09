import asyncio
import json
import unittest

from app.i18n import LocaleMiddleware, localize_payload, negotiate_language, translate, validation_message


class LocaleTest(unittest.TestCase):
    def test_language_negotiation(self):
        for header, expected in [
            ("", "zh-CN"), ("fr-FR", "zh-CN"), ("en-US,en;q=0.9", "en"),
            ("zh-TW", "zh-CN"), ("zh;q=0.2,en-GB;q=0.8", "en"),
            ("en;q=0,zh;q=0.5", "zh-CN"), ("en;q=bad", "zh-CN"),
            ("en;q=NaN,zh", "zh-CN"), ("en;q=2,zh", "zh-CN"),
        ]:
            self.assertEqual(negotiate_language(header), expected)

    def test_complete_messages_interpolate_without_translating_user_values(self):
        self.assertEqual(translate("项目不存在", "zh-CN"), "项目不存在")
        self.assertEqual(translate("项目不存在"), "Project does not exist")
        self.assertEqual(translate("缺少必填属性：项目不存在"), "Missing required attribute: 项目不存在")
        self.assertEqual(translate("Unrecognized external diagnostic"), "Unrecognized external diagnostic")
        self.assertEqual(translate("未收录的第三方诊断。"), "未收录的第三方诊断。")

    def test_api_content_remains_canonical_except_system_messages(self):
        payload = {
            "name": "项目不存在", "description": "项目不存在", "display_name": "项目不存在",
            "note": "项目不存在", "reason": "项目不存在", "filename": "项目不存在.png",
            "labels": [{"name": "项目不存在", "attributes": {"message": "项目不存在"}}],
            "settings": {"message": "项目不存在"}, "error": "项目不存在",
            "availability": {"reason": "项目不存在", "available": False},
        }
        result = localize_payload(payload, "/api/projects/1", 200, "en")
        for key in set(payload) - {"error", "availability"}:
            self.assertEqual(result[key], payload[key], key)
        self.assertEqual(result["error"], "Project does not exist")
        self.assertEqual(result["availability"]["reason"], "Project does not exist")
        self.assertEqual(payload["error"], "项目不存在", "Do not mutate shared state")

    def test_registration_rejection_preserves_administrator_feedback(self):
        payload = {"detail": {"code": "registration_rejected", "message": "项目不存在",
                              "registration": {"reason": "项目不存在", "display_name": "项目不存在"}}}
        result = localize_payload(payload, "/api/login", 403, "en")
        self.assertEqual(result["detail"]["code"], "registration_rejected")
        self.assertEqual(result["detail"]["message"], "Project does not exist")
        self.assertEqual(result["detail"]["registration"], payload["detail"]["registration"])

    def test_builtin_metadata_and_job_progress(self):
        result = localize_payload({"builtin": True, "name": "目标检测", "description": "项目不存在",
                                   "warnings": ["项目不存在"], "capabilities": ["目标检测"],
                                   "progress_message": "训练完成", "status": "completed"}, "/api/models", 200, "en")
        self.assertEqual(result["name"], "Object detection")
        self.assertEqual(result["description"], "Project does not exist")
        self.assertEqual(result["progress_message"], "Training completed")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["warnings"], ["Project does not exist"])
        self.assertEqual(result["capabilities"], ["Object detection"])

    def test_concatenated_preparation_instructions(self):
        message = "请先在官方页面申请并获得访问权限，自行下载 sam3.pt，放到此目录后点击重新检查。 本项目不会自动下载 SAM 权重；识别文件后还会检查推理依赖。"
        self.assertNotRegex(translate(message), r"[\u3400-\u9fff]")

    def test_persisted_generated_names_and_custom_model_badges(self):
        block = {"id": 1, "name": "任务块 001", "owner_name": "项目不存在", "events": [{"reason": "项目不存在"}]}
        localized = localize_payload(block, "/api/projects/1/work-blocks/1?detail=true", 200, "en")
        self.assertEqual(localized["name"], "Work block 001")
        self.assertEqual(localized["owner_name"], block["owner_name"])
        self.assertEqual(localized["events"], block["events"])
        self.assertEqual(localize_payload(block, "/api/projects/1", 200, "en"), block)
        models = [{"builtin": True, "provider_type": "local_yolo", "name": "yolov8n（本地 AI）"},
                  {"builtin": False, "provider_type": "remote_detection", "name": "项目不存在", "badge": "自定义"}]
        result = localize_payload(models, "/api/models", 200, "en")
        self.assertEqual(result[0]["name"], "yolov8n (Local AI)")
        self.assertEqual(result[1]["badge"], "Custom")
        self.assertEqual(result[1]["name"], "项目不存在")

    def test_nested_diagnostics_and_conversion_reasons(self):
        from app.training import _system_model_options
        for task in ["classify", "segment", "pose", "detect"]:
            with self.subTest(task=task):
                options = _system_model_options({"task": task, "model_arch": "custom.pt"})
                result = localize_payload({"system_model": options}, "/api/training-runs/1/conversion-options", 200, "en")
                self.assertNotRegex(result["system_model"]["reason"], r"[\u3400-\u9fff]")
        self.assertEqual(translate("checkpoint 无法加载：未安装 ultralytics"), "Checkpoint loading failed: Ultralytics not installed")
        self.assertEqual(translate("下载失败，可重试续传：ValueError: 项目不存在"), "Download failed, retry or resume: ValueError: Project does not exist")
        # A filename/label that happens to be a dictionary key must stay intact.
        message = translate("图片“项目不存在”无法增强：项目不存在")
        self.assertIn("项目不存在", message)
        self.assertIn("Project does not exist", message)
        blocked = localize_payload({"blocked_reason": "设备已撤销或项目已归档"}, "/api/edge/devices/1/system-models", 200, "en")
        self.assertNotRegex(blocked["blocked_reason"], r"[\u3400-\u9fff]")

    def test_unknown_long_diagnostic_does_not_match_ui_fragments(self):
        message = "未收录的第三方诊断" * 1000
        self.assertEqual(translate(message), message)

    def test_format_descriptions_and_augmentation_operation_names(self):
        payload = {"formats": {"onnx": {"description": "通用交换格式，可交给 ONNX Runtime 或其他推理引擎。"}}}
        result = localize_payload(payload, "/api/training-runs/1/conversion-options", 200, "en")
        self.assertNotRegex(result["formats"]["onnx"]["description"], r"[\u3400-\u9fff]")
        payload = {"operations": [{"type": "horizontal_flip", "name": "水平翻转", "available": True}]}
        result = localize_payload(payload, "/api/projects/1/augmentation-options", 200, "en")
        self.assertEqual(result["operations"][0]["name"], "Horizontal flip")

    def test_standard_and_custom_validation(self):
        error = {"type": "string_too_short", "msg": "String should have at least 12 characters", "ctx": {"min_length": 12}}
        self.assertEqual(validation_message(error, "zh-CN"), "请至少输入 12 个字符。")
        self.assertEqual(validation_message(error, "en"), error["msg"])
        self.assertEqual(validation_message({"type": "value_error", "msg": "Value error, 项目不存在"}, "en"), "Project does not exist")

    def test_standalone_runtime_assets_and_errors(self):
        from fastapi.testclient import TestClient
        from unittest.mock import patch
        from app.runtime import server

        # No lifespan: never load model weights or start inference during this test.
        with patch.object(server, "_key", "k" * 32):
            client = TestClient(server.app)
            self.assertEqual(client.get("/").status_code, 200)
            self.assertIn("languageControlsMarkup", client.get("/").text)
            self.assertEqual(client.get("/static/i18n.js").status_code, 200)
            self.assertEqual(client.get("/static/locales/en.js").status_code, 200)
            for locale in ["en", "zh-CN"]:
                response = client.post("/predict", headers={"Accept-Language": locale, "Authorization": "Bearer " + "k" * 32})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.headers["content-language"], locale)
                self.assertEqual(response.json()["detail"][0]["msg"], "Field required" if locale == "en" else "此字段为必填项。")
                self.assertNotIn("input", response.json()["detail"][0])
            client.close()

    def test_streamed_json_headers_and_binary_passthrough(self):
        async def run(language, content_type, content):
            sent = []

            async def app(scope, receive, send):
                await send({"type": "http.response.start", "status": 409, "headers": [
                    (b"content-type", content_type), (b"content-length", str(len(content)).encode()),
                    (b"cache-control", b"no-store"), (b"www-authenticate", b"Bearer"),
                ]})
                for index, chunk in enumerate([content[:10], content[10:]]):
                    await send({"type": "http.response.body", "body": chunk, "more_body": index == 0})

            async def send(message):
                sent.append(message)

            await LocaleMiddleware(app)({"type": "http", "path": "/api/example",
                                         "headers": [(b"accept-language", language)]}, None, send)
            return sent

        original = json.dumps({"detail": "项目不存在"}, ensure_ascii=False).encode()
        en = asyncio.run(run(b"en", b"application/json", original))
        headers = dict(en[0]["headers"])
        self.assertEqual(json.loads(en[1]["body"])["detail"], "Project does not exist")
        self.assertEqual(int(headers[b"content-length"]), len(en[1]["body"]))
        self.assertEqual(headers[b"content-language"], b"en")
        self.assertEqual(headers[b"cache-control"], b"no-store")
        self.assertEqual(headers[b"www-authenticate"], b"Bearer")
        zh = asyncio.run(run(b"zh-CN", b"application/json", original))
        self.assertEqual(b"".join(item["body"] for item in zh[1:]), original)
        binary = asyncio.run(run(b"en", b"application/zip", original))
        self.assertEqual(b"".join(item["body"] for item in binary[1:]), original)
        self.assertNotIn(b"content-language", dict(binary[0]["headers"]))
