from __future__ import annotations

import fcntl
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


class AnnotationPreparationTest(unittest.TestCase):
    def setUp(self):
        from app.config import settings
        from app.main import app
        from app import model_downloads
        self.settings, self.app, self.downloads = settings, app, model_downloads
        self.temp = tempfile.TemporaryDirectory(prefix="cvsprout-annotation-prepare-")
        self.root = Path(self.temp.name)
        changes = dict(data_dir=self.root / "data", models_dir=self.root / "models", auth_mode="disabled",
                       embedded_worker=False, embedded_pipeline_worker=False, worker_url="", qwen_base_url="",
                       sam2_checkpoint=self.root / "models/sam2/sam2.1_t.pt", sam3_checkpoint=self.root / "models/sam3/sam3.pt",
                       training_base_model=self.root / "models/training/missing.pt",
                       training_segmentation_model=self.root / "models/training/missing-seg.pt",
                       training_model_dirs_raw=str(self.root / "models/training"))
        self.previous = {key: getattr(settings, key) for key in changes}
        for key, value in changes.items():
            object.__setattr__(settings, key, value)
        self.client = TestClient(app, headers={"X-MDP-Client": "browser"}).__enter__()

    def tearDown(self):
        self.app.dependency_overrides.clear()
        self.client.__exit__(None, None, None)
        for key, value in self.previous.items():
            object.__setattr__(self.settings, key, value)
        self.temp.cleanup()

    def model(self, name="qwen3.5-0.8b"):
        response = self.client.get(f"/api/models/{name}/preparation")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_listing_missing_qwen_is_offline_and_has_a_download_plan(self):
        with patch.object(self.downloads, "queue_download", side_effect=AssertionError("GET must not download")), \
                patch.object(self.downloads.httpx, "stream", side_effect=AssertionError("GET must not fetch")):
            model = self.model()
            self.assertFalse(model["configured"])
            self.assertEqual(model["provider_type"], "local_qwen")
            self.assertTrue(model["preparation"]["downloadable"])
            self.assertFalse(model["preparation"]["installed"])
            self.assertIn("model.safetensors.index.json", model["preparation"]["files"])
            self.assertIn("0.8b", model["preparation"]["directory"])
            self.assertEqual(self.client.get("/api/models").status_code, 200)

    def test_annotator_can_prepare_only_a_known_enabled_download(self):
        from app.auth import Actor, get_actor
        self.app.dependency_overrides[get_actor] = lambda: Actor("annotator", "annotator")
        with patch.object(self.downloads, "queue_download") as start:
            response = self.client.post("/api/models/qwen3.5-0.8b/prepare")
            self.assertEqual(response.status_code, 202, response.text)
            start.assert_called_once_with("qwen3.5-0.8b", idempotent=True)
        self.assertEqual(self.client.post("/api/model-downloads/qwen3.5-0.8b").status_code, 403)
        self.assertEqual(self.client.post("/api/models/unknown/prepare").status_code, 404)

    def test_disabled_and_manual_models_cannot_start_a_download(self):
        from app.database import db
        with db() as connection:
            connection.execute("UPDATE model_services SET enabled=0 WHERE id='qwen3.5-0.8b'")
        with patch.object(self.downloads, "queue_download", side_effect=AssertionError("download forbidden")):
            for name in ("qwen3.5-0.8b", "sam2-assist", "sam3"):
                self.assertEqual(self.client.post(f"/api/models/{name}/prepare").status_code, 409)
        with patch.object(self.downloads.threading, "Thread", side_effect=AssertionError("SAM must remain manual")):
            for name in ("sam2.1-hiera-tiny", "sam3"):
                self.assertEqual(self.client.post(f"/api/model-downloads/{name}").status_code, 409)

    def test_prepare_rejects_unprivileged_and_password_change_actors(self):
        from app.auth import Actor, get_actor
        for actor in (Actor("readonly", "viewer"), Actor("temporary", "annotator", must_change_password=True)):
            self.app.dependency_overrides[get_actor] = lambda actor=actor: actor
            with patch.object(self.downloads, "queue_download", side_effect=AssertionError("unauthorized download")):
                self.assertEqual(self.client.post("/api/models/qwen3.5-0.8b/prepare").status_code, 403)

    def test_empty_remote_placeholder_migrates_but_configured_remote_is_preserved(self):
        from app.database import db, one
        from app.providers import seed_builtin_models
        with db() as connection:
            connection.execute("UPDATE model_services SET provider_type='openai_vision',endpoint='',model_name='Qwen/Qwen3.5-35B-A3B',enabled=0 WHERE id='qwen3.5-35b-a3b'")
        seed_builtin_models()
        row = one("SELECT * FROM model_services WHERE id='qwen3.5-35b-a3b'")
        self.assertEqual(row["provider_type"], "local_qwen")
        self.assertFalse(row["enabled"])
        with db() as connection:
            connection.execute("UPDATE model_services SET provider_type='openai_vision',endpoint='https://example.invalid/v1',model_name='custom-qwen',enabled=1 WHERE id='qwen3.5-35b-a3b'")
        seed_builtin_models()
        model = self.model("qwen3.5-35b-a3b")
        self.assertEqual(model["endpoint"], "https://example.invalid/v1")
        self.assertEqual(model["model_name"], "custom-qwen")
        self.assertTrue(model["configured"])
        self.assertIsNone(model["preparation"])
        self.assertEqual(self.client.post("/api/models/qwen3.5-35b-a3b/prepare").status_code, 409)

    def test_manual_sam_files_refresh_without_restart_and_show_missing_runtime(self):
        initial = self.model("sam2-assist")
        self.assertFalse(initial["preparation"]["installed"])
        path = self.settings.sam2_checkpoint
        path.parent.mkdir(parents=True)
        path.write_bytes(b"test fixture")
        with patch("app.providers.registry.local_sam2_runtime_status", return_value=(False, "缺少测试推理依赖")):
            found = self.model("sam2-assist")
        self.assertTrue(found["preparation"]["installed"])
        self.assertFalse(found["configured"])
        self.assertIn("测试推理依赖", found["availability_error"])
        with patch("app.providers.registry.local_sam2_runtime_status", return_value=(True, "")):
            self.assertTrue(self.model("sam2-assist")["configured"])
        path.unlink()
        self.assertFalse(self.model("sam2-assist")["preparation"]["installed"])

    def test_alternate_sam2_filename_and_legacy_directory_are_detected(self):
        from app.annotation_models import sam2_model_path
        path = self.settings.sam2_checkpoint.with_name("sam2.1_hiera_tiny.pt")
        path.parent.mkdir(parents=True)
        path.write_bytes(b"test fixture")
        self.assertEqual(sam2_model_path(), path)
        self.assertEqual(self.model("sam2-assist")["preparation"]["files"], [path.name])
        path.unlink()
        legacy = self.settings.models_dir / "sam2.1-hiera-tiny"
        legacy.mkdir()
        for name in ("config.json", "model.safetensors", "preprocessor_config.json", "processor_config.json"):
            (legacy / name).write_bytes(b"test fixture")
        self.assertEqual(sam2_model_path(), legacy)
        self.assertTrue(self.model("sam2-assist")["preparation"]["installed"])

    def test_manual_catalog_rows_are_not_installed_when_their_files_are_absent(self):
        rows = self.client.get("/api/model-downloads").json()
        manual = [row for row in rows if row.get("download_policy") == "manual"]
        self.assertEqual({row["id"] for row in manual}, {"sam2.1-hiera-tiny", "sam3"})
        for row in manual:
            self.assertFalse(row["installed"])
            self.assertFalse(row["preparation"]["downloadable"])
            self.assertTrue(row["preparation"]["missing_files"])

    def test_concurrent_preparation_reuses_the_download_lock_and_detects_interruption(self):
        entry = next(item for item in self.downloads.catalog() if item["id"] == "qwen3.5-0.8b")
        lock_path = self.downloads.state_dir() / "qwen3.5-0.8b.lock"
        self.downloads.write_state(entry["id"], status="downloading", downloaded=12, total=34, error="")
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(self.downloads.threading, "Thread", side_effect=AssertionError("duplicate download")):
                self.assertEqual(self.downloads.queue_download(entry["id"], idempotent=True)["status"], "downloading")
            self.assertEqual(self.model()["preparation"]["state"]["status"], "downloading")
        self.assertEqual(self.model()["preparation"]["state"]["status"], "interrupted")

    def test_complete_files_do_not_redownload_and_disabled_health_is_not_ready(self):
        plan = self.model()["preparation"]
        directory = Path(plan["directory"])
        directory.mkdir(parents=True)
        for name in plan["files"]:
            (directory / name).write_bytes(b"fixture")
        with patch.object(self.downloads, "queue_download", side_effect=AssertionError("unexpected redownload")):
            response = self.client.post("/api/models/qwen3.5-0.8b/prepare")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(response.json()["preparation"]["installed"])
        self.assertFalse(response.json()["configured"])


if __name__ == "__main__":
    unittest.main()
