from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient


class OpenSourceTest(unittest.TestCase):
    def setUp(self):
        from app.config import settings
        from app.main import app
        self.settings = settings
        self.temp = tempfile.TemporaryDirectory(prefix="visionforge-onboarding-test-")
        root = Path(self.temp.name)
        changes = dict(data_dir=root / "data", models_dir=root / "models", auth_mode="accounts",
                       embedded_worker=False, embedded_pipeline_worker=False, worker_url="",
                       training_base_model=root / "models/training/absent.pt",
                       training_segmentation_model=root / "models/training/absent-seg.pt",
                       training_model_dirs_raw=str(root / "models/training"))
        self.previous = {key: getattr(settings, key) for key in changes}
        for key, value in changes.items():
            object.__setattr__(settings, key, value)
        self.client = TestClient(app, headers={"X-MDP-Client": "browser"}).__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for key, value in self.previous.items():
            object.__setattr__(self.settings, key, value)
        self.temp.cleanup()

    def enroll(self):
        from app.setup import setup_code
        body = dict(username="owner", display_name="Owner", password="a-secure-password-123", code=setup_code())
        response = self.client.post("/api/setup", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        response = self.client.post("/api/login", json={k: body[k] for k in ("username", "password")})
        self.assertEqual(response.status_code, 200, response.text)
        return body

    def test_setup_requires_code_and_closes_after_first_admin(self):
        self.assertTrue(self.client.get("/api/health").json()["setup_required"])
        body = dict(username="attacker", display_name="Attacker", password="a-secure-password-123", code="x" * 32)
        self.assertEqual(self.client.post("/api/setup", json=body).status_code, 403)
        body = self.enroll()
        self.assertFalse(self.client.get("/api/setup").json()["required"])
        self.assertEqual(self.client.post("/api/setup", json=body).status_code, 409)
        self.assertEqual(self.client.get("/api/session").json()["role"], "admin")
        self.assertFalse((self.settings.data_dir / ".setup-code").exists())

    def test_cross_origin_cannot_enroll(self):
        from app.setup import setup_code
        body = dict(username="owner", display_name="Owner", password="a-secure-password-123", code=setup_code())
        response = self.client.post("/api/setup", json=body, headers={"Origin": "https://unrelated.invalid"})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(self.client.get("/api/setup").json()["required"])

    def test_import_rejects_paths_empty_files_and_duplicate_names(self):
        self.assertEqual(self.client.get("/api/model-downloads").status_code, 401)
        self.enroll()
        url = "/api/model-downloads/import/checkpoint"
        self.assertEqual(self.client.post(url, files={"file": ("../../owned.pt", b"x")}).status_code, 400)
        self.assertEqual(self.client.post(url, files={"file": ("my.pt", b"")}).status_code, 400)
        first = self.client.post(url, files={"file": ("my.pt", b"trusted-checkpoint")})
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(self.client.post(url, files={"file": ("my.pt", b"replacement")}).status_code, 409)
        self.assertEqual((self.settings.models_dir / "training/my.pt").read_bytes(), b"trusted-checkpoint")

    def test_download_verifies_before_publishing_and_preserves_custom_models(self):
        from app.model_downloads import download_file
        data = b"verified-model"
        item = {"path": "training/model.pt", "url": "https://example.invalid/model.pt",
                "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        with patch("app.model_downloads.httpx.stream", return_value=nullcontext(httpx.Response(200, content=b"x" * len(data), request=httpx.Request("GET", item["url"])))):
            with self.assertRaises(ValueError):
                download_file(item, lambda amount: None)
        target = self.settings.models_dir / item["path"]
        self.assertFalse(target.exists())
        with patch("app.model_downloads.httpx.stream", return_value=nullcontext(httpx.Response(200, content=data, request=httpx.Request("GET", item["url"])))):
            download_file(item, lambda amount: None)
        self.assertEqual(target.read_bytes(), data)
        target.write_bytes(b"custom-trained-model")
        with self.assertRaises(ValueError):
            download_file(item, lambda amount: None)
        self.assertEqual(target.read_bytes(), b"custom-trained-model")

    def test_worker_auth_and_media_path_are_enforced(self):
        from app.worker_server import app
        from app.worker_client import worker_token
        with TestClient(app) as worker:
            self.assertEqual(worker.post("/internal/call/resources", json={}).status_code, 401)
            headers = {"X-VisionForge-Worker": worker_token()}
            self.assertEqual(worker.post("/internal/call/arbitrary-code", json={}, headers=headers).status_code, 404)
            result = worker.post("/internal/predict", headers=headers, json={
                "model_id": "grounding-dino", "image": "../.worker-token", "width": 1, "height": 1})
            self.assertEqual(result.status_code, 400)

    def test_download_cannot_replace_a_model_imported_during_transfer(self):
        from app.model_downloads import download_file, destination
        data = b"official-model"
        item = {"path": "training/concurrent.pt", "url": "https://example.invalid/model.pt",
                "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        target = destination(item["path"])
        response = httpx.Response(200, content=data, request=httpx.Request("GET", item["url"]))
        with patch("app.model_downloads.httpx.stream", return_value=nullcontext(response)):
            with self.assertRaises(ValueError):
                download_file(item, lambda amount: target.write_bytes(b"my-imported-model"))
        self.assertEqual(target.read_bytes(), b"my-imported-model")

    def test_download_resumes_only_at_the_verified_offset(self):
        from app.model_downloads import download_file, destination
        content = b"resumable-model-content"
        item = {"path": "training/resume.pt", "url": "https://example.invalid/resume.pt",
                "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        target = destination(item["path"])
        target.parent.mkdir(parents=True)
        partial = target.with_name(target.name + ".part")
        partial.write_bytes(content[:5])
        response = httpx.Response(206, content=content[5:], headers={"Content-Range": f"bytes 5-{len(content)-1}/{len(content)}"}, request=httpx.Request("GET", item["url"]))
        with patch("app.model_downloads.httpx.stream", return_value=nullcontext(response)) as stream:
            download_file(item, lambda amount: None)
        self.assertEqual(stream.call_args.kwargs["headers"]["Range"], "bytes=5-")
        self.assertEqual(target.read_bytes(), content)
        self.assertFalse(partial.exists())

    def test_deployment_bundle_checks_source_and_contains_runtime(self):
        self.enroll()
        from app.database import db, now_iso
        artifact = self.settings.training_dir / "run-1/attempt-1/best.pt"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"bundle-fixture")
        checksum = hashlib.sha256(artifact.read_bytes()).hexdigest()
        with db() as conn:
            p = conn.execute("INSERT INTO projects(name,task_type,created_at,updated_at) VALUES ('test','detect',?,?)", (now_iso(), now_iso())).lastrowid
            v = conn.execute("""INSERT INTO dataset_versions(project_id,version_number,name,train_ratio,valid_ratio,test_ratio,content_hash,asset_count,annotation_count,task_type,created_by,created_at)
                VALUES (?,1,'test',0.8,0.2,0,'hash',2,2,'detect','test',?)""", (p, now_iso())).lastrowid
            run = conn.execute("""INSERT INTO training_runs(project_id,version_id,name,model_arch,task,status,total_epochs,settings_json,created_by,created_at,artifact_path,artifact_metadata_json)
                VALUES (?,?,'test','test.pt','detect','completed',1,?,'test',?,?,?)""", (p, v, json.dumps({"image_size": 64}), now_iso(), str(artifact), json.dumps({"sha256": checksum, "size_bytes": artifact.stat().st_size}))).lastrowid
        response = self.client.get(f"/api/training-runs/{run}/deployment")
        self.assertEqual(response.status_code, 200, response.text[:100])
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            manifest = json.loads(archive.read("model/manifest.json"))
            self.assertEqual(manifest["sha256"], checksum)
            self.assertIn("app/runtime/server.py", archive.namelist())
            self.assertIn("start.ps1", archive.namelist())
            self.assertIn("LICENSE", archive.namelist())
            self.assertIn("THIRD_PARTY_NOTICES.md", archive.namelist())
            self.assertEqual(archive.read("model/model.pt"), b"bundle-fixture")
        artifact.write_bytes(b"changed")
        self.assertEqual(self.client.get(f"/api/training-runs/{run}/deployment").status_code, 409)


class ResourceCoordinationTest(unittest.TestCase):
    def test_training_fonts_work_without_network_and_preserve_custom_fonts(self):
        from app.plot_fonts import prepare_training_fonts
        from PIL import ImageFont
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            with patch("ultralytics.utils.USER_CONFIG_DIR", directory), patch("urllib.request.urlopen", side_effect=AssertionError("Training must not download fonts")):
                prepare_training_fonts()
                for name in ("Arial.ttf", "Arial.Unicode.ttf"):
                    self.assertIsNotNone(ImageFont.truetype(str(directory / name), 12))
                (directory / "Arial.ttf").write_bytes(b"custom-font")
                prepare_training_fonts()
                self.assertEqual((directory / "Arial.ttf").read_bytes(), b"custom-font")

    def test_physical_gpu_identity_ignores_local_cuda_ordinal(self):
        from types import SimpleNamespace
        from app.providers.execution import device_lock_name
        fake = SimpleNamespace(cuda=SimpleNamespace(get_device_properties=lambda index: SimpleNamespace(uuid="physical-gpu-id")))
        with patch.dict("sys.modules", {"torch": fake}):
            self.assertEqual(device_lock_name("cuda:0"), device_lock_name("cuda:3"))

    def test_inference_and_training_cannot_hold_same_resource(self):
        from app.config import settings
        from app.training import _try_file_resource_lock, _release_file_resource_lock
        with tempfile.TemporaryDirectory() as root:
            previous = settings.data_dir
            object.__setattr__(settings, "data_dir", Path(root))
            try:
                self.assertIsNotNone(_try_file_resource_lock("cpu", "inference", 1, 0))
                self.assertIsNone(_try_file_resource_lock("cpu", "training", 2, 1))
                _release_file_resource_lock("inference", 1, 0)
                self.assertIsNotNone(_try_file_resource_lock("cpu", "training", 2, 1))
            finally:
                _release_file_resource_lock("inference", 1, 0)
                _release_file_resource_lock("training", 2, 1)
                object.__setattr__(settings, "data_dir", previous)
