from __future__ import annotations

import asyncio
import io
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.testclient import TestClient

from app.http_limits import RequestBodyLimit


class BodyLimitTest(unittest.TestCase):
    def test_chunked_multipart_overflow_closes_temporary_files(self):
        files = []
        def temporary(*args, **kwargs):
            handle = tempfile.SpooledTemporaryFile(*args, **kwargs)
            files.append(handle)
            return handle
        async def budget(scope):
            return 2 * 1024 * 1024
        app = FastAPI()
        app.add_middleware(RequestBodyLimit, policy=budget)
        @app.post("/upload")
        async def upload(file: UploadFile = File(...)):
            self.fail("An oversized request must not reach the endpoint")
        chunks = iter([
            b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="image.png"\r\n\r\n',
            b"x" * (1024 * 1024 + 1), b"x" * (1024 * 1024),
            b"\r\n--boundary--\r\n",
        ])
        messages = []
        async def receive():
            return {"type": "http.request", "body": next(chunks), "more_body": True}
        async def send(message):
            messages.append(message)
        scope = {"type": "http", "method": "POST", "path": "/upload", "raw_path": b"/upload",
                 "root_path": "", "query_string": b"", "scheme": "http", "http_version": "1.1",
                 "headers": [(b"content-type", b"multipart/form-data; boundary=boundary")],
                 "server": ("testserver", 80), "client": ("127.0.0.1", 1234)}
        with patch("starlette.formparsers.SpooledTemporaryFile", side_effect=temporary):
            asyncio.run(app(scope, receive, send))
        self.assertEqual(messages[0]["status"], 413)
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].closed)

    def test_content_length_rejection_does_not_read_body(self):
        async def budget(scope):
            return 8
        async def endpoint(scope, receive, send):
            self.fail("Oversized or malformed request reached the app")
        app = RequestBodyLimit(endpoint, budget)
        for value, status in ((b"9", 413), (b"-1", 400), (b"invalid", 400)):
            messages = []
            async def send(message):
                messages.append(message)
            receive = AsyncMock(side_effect=AssertionError("body read"))
            asyncio.run(app({"type": "http", "headers": [(b"content-length", value)]}, receive, send))
            self.assertEqual(messages[0]["status"], status)
            receive.assert_not_called()


class ProductionApiTest(unittest.TestCase):
    def setUp(self):
        from app.config import settings
        from app.main import app
        self.settings = settings
        temporary = tempfile.TemporaryDirectory(prefix="cvsprout-production-api-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        changes = dict(data_dir=root / "data", models_dir=root / "models", auth_mode="accounts",
                       worker_url="", embedded_worker=False, embedded_pipeline_worker=False,
                       max_request_mb=1, max_upload_mb=1, public_origin="",
                       training_base_model=root / "models/training/absent.pt",
                       training_segmentation_model=root / "models/training/absent-seg.pt",
                       training_model_dirs_raw=str(root / "models/training"))
        previous = {key: getattr(settings, key) for key in changes}
        def restore():
            for key, value in previous.items():
                object.__setattr__(settings, key, value)
        self.addCleanup(restore)
        for key, value in changes.items():
            object.__setattr__(settings, key, value)
        self.client = TestClient(app, headers={"X-MDP-Client": "browser"}).__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def login(self):
        from app.setup import setup_code
        body = dict(username="owner", display_name="Owner", password="secure-password-1234", code=setup_code())
        self.assertEqual(self.client.post("/api/setup", json=body).status_code, 201)
        self.assertEqual(self.client.post("/api/login", json=body).status_code, 200)

    def test_anonymous_upload_rejected_before_multipart_parsing(self):
        for path in ("/api/projects/1/assets", "/api/projects/invalid/assets", "/api/model-downloads/import/checkpoint"):
            with patch("starlette.requests.Request.form", side_effect=AssertionError("parsed anonymous upload")):
                response = self.client.post(path, content=b"not-a-multipart-body",
                    headers={"Content-Type": "multipart/form-data; boundary=test"})
            self.assertEqual(response.status_code, 401)

    def test_non_admin_checkpoint_upload_rejected_before_parsing(self):
        self.login()
        from app.database import db
        with db() as conn:
            conn.execute("UPDATE users SET role='annotator'")
        with patch("starlette.requests.Request.form", side_effect=AssertionError("parsed non-admin checkpoint")):
            response = self.client.post("/api/model-downloads/import/checkpoint", content=b"x",
                headers={"Content-Type": "multipart/form-data; boundary=test"})
        self.assertEqual(response.status_code, 403)

    def test_large_json_is_rejected_and_health_still_works(self):
        response = self.client.post("/api/login", content=b"x" * (1024 * 1024 + 1),
                                    headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.client.get("/api/health").status_code, 200)

    def test_oversized_image_does_not_leave_files(self):
        self.login()
        project = self.client.post("/api/projects", json={"name": "limit"}).json()
        response = self.client.post(f"/api/projects/{project['id']}/assets",
                                   files={"files": ("too-big.png", b"x" * (1024 * 1024 + 1))})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(list(self.settings.media_dir.iterdir()), [])

    def test_non_ascii_credentials_are_rejected_without_server_error(self):
        response = self.client.post("/api/setup", json={"username": "owner", "display_name": "Owner",
            "password": "secure-password-1234", "code": "错误" * 16})
        self.assertEqual(response.status_code, 403)
        import time
        from app.auth import actor_from_session_identity
        with self.assertRaises(HTTPException) as error:
            actor_from_session_identity(f"{'a' * 32}.{int(time.time())}.无效签名")
        self.assertEqual(error.exception.status_code, 401)

    def test_ready_reports_database_and_worker_failure(self):
        self.assertEqual(self.client.get("/api/ready").status_code, 200)
        with patch("app.main.one", side_effect=OSError("private filesystem path")):
            response = self.client.get("/api/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["component"], "database")
        self.assertNotIn("private", response.text)
        object.__setattr__(self.settings, "worker_url", "http://worker:8001")
        import httpx
        for response in (httpx.Response(503), httpx.Response(200, json={"status": "failed"})):
            with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=response)):
                self.assertEqual(self.client.get("/api/ready").status_code, 503)
        with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=httpx.Response(200, json={"status": "ok"}))):
            self.assertEqual(self.client.get("/api/ready").status_code, 200)

    def test_dead_background_task_fails_worker_health(self):
        from app.worker_server import app, health
        with patch.object(app.state, "worker_tasks", [SimpleNamespace(done=lambda: True)], create=True):
            with self.assertRaises(HTTPException) as error:
                health()
        self.assertEqual(error.exception.status_code, 503)


class RuntimeAdmissionTest(unittest.IsolatedAsyncioTestCase):
    async def test_busy_prediction_is_rejected_before_file_read(self):
        from app.runtime import server
        file = SimpleNamespace(read=AsyncMock(side_effect=AssertionError("read while busy")))
        with patch.object(server, "_request_slot", threading.Lock()):
            server._request_slot.acquire()
            with self.assertRaises(HTTPException) as error:
                await server.predict(file)
            self.assertEqual(error.exception.status_code, 429)
            file.read.assert_not_called()

    async def test_invalid_image_releases_prediction_slot(self):
        from app.runtime import server
        with patch.object(server, "_request_slot", threading.Lock()):
            for _ in range(2):
                with self.assertRaises(HTTPException) as error:
                    await server.predict(UploadFile(io.BytesIO(b"invalid"), filename="image.png"))
                self.assertEqual(error.exception.status_code, 400)
                self.assertFalse(server._request_slot.locked())

    async def test_cancelled_request_holds_slot_until_thread_finishes(self):
        from app.runtime import server
        started, finish = threading.Event(), threading.Event()
        def inference(data):
            started.set()
            finish.wait(5)
            return {"ok": True}
        lock = threading.Lock()
        with patch.object(server, "_request_slot", lock), patch.object(server, "_decode_and_predict", side_effect=inference):
            task = asyncio.create_task(server.predict(UploadFile(io.BytesIO(b"fixture"))))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 2))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(lock.locked())
            finally:
                finish.set()
                for _ in range(100):
                    if not lock.locked():
                        break
                    await asyncio.sleep(0.01)
            self.assertFalse(lock.locked())


if __name__ == "__main__":
    unittest.main()
