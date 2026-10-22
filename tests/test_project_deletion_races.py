from __future__ import annotations

import asyncio
import hashlib
import importlib
import io
import json
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image


class ProjectDeletionRaceTest(unittest.TestCase):
    def setUp(self):
        global settings, db, one, main, versions, training
        from app.config import settings
        from app.database import db, one
        main = importlib.import_module("app.main")
        versions = importlib.import_module("app.dataset_versions")
        training = importlib.import_module("app.training")
        self.temp = tempfile.TemporaryDirectory(prefix="cvsprout-project-delete-races-")
        values = dict(data_dir=Path(self.temp.name), embedded_worker=False,
                      embedded_pipeline_worker=False, auth_mode="disabled", enable_demo_model=True)
        self.previous = {key: getattr(settings, key) for key in values}
        for key, value in values.items():
            object.__setattr__(settings, key, value)
        self.client = TestClient(main.app, headers={"X-MDP-Client": "browser"}).__enter__()
        response = self.client.post("/api/projects", json={"name": "删除并发测试", "labels": ["object"]})
        self.assertEqual(response.status_code, 201, response.text)
        self.project = response.json()
        self.url = f"/api/projects/{self.project['id']}"
        label = self.client.get(self.url).json()["labels"][0]["id"]
        self.images = []
        for index in range(2):
            stream = io.BytesIO()
            Image.new("RGB", (64, 64), (40 + index, 80, 100)).save(stream, "PNG")
            self.image = stream.getvalue()
            response = self.client.post(self.url + "/assets", files={"files": (f"{index}.png", self.image, "image/png")})
            self.assertEqual(response.status_code, 201, response.text)
            asset_id = response.json()["asset_ids"][0]
            self.images.append(asset_id)
            response = self.client.post(f"/api/assets/{asset_id}/annotations", json={
                "label_id": label, "shape_type": "rectangle", "points": [4, 4, 40, 40]})
            self.assertEqual(response.status_code, 201, response.text)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for key, value in self.previous.items():
            object.__setattr__(settings, key, value)
        self.temp.cleanup()

    def delete_project(self):
        response = self.client.request("DELETE", self.url, json={"confirmation_name": self.project["name"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["deleted"])
        return response

    def version(self):
        response = self.client.post(self.url + "/versions", json={
            "approved_only": False, "train_ratio": .5, "valid_ratio": .5, "test_ratio": 0})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def completed_run(self):
        version = self.version()
        with db() as conn:
            run_id = conn.execute("""INSERT INTO training_runs
                (project_id,version_id,name,model_arch,status,total_epochs,created_by,created_at)
                VALUES (?,?,'finished','test.pt','completed',1,'test','2026-10-22')""",
                (self.project["id"], version["id"])).lastrowid
            artifact = settings.training_dir / f"run-{run_id}" / "weights" / "best.pt"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"fixture checkpoint")
            metadata = {"size_bytes": artifact.stat().st_size, "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
            conn.execute("UPDATE training_runs SET artifact_path=?,artifact_metadata_json=? WHERE id=?",
                         (str(artifact), json.dumps(metadata), run_id))
        return run_id, artifact

    def test_upload_rechecks_deleted_project_and_removes_staged_files(self):
        unpack = main.unpack_upload

        async def unpack_then_delete(upload):
            result = await unpack(upload)
            await asyncio.to_thread(self.delete_project)
            return result

        with patch.object(main, "unpack_upload", side_effect=unpack_then_delete):
            response = self.client.post(self.url + "/assets", files={"files": ("new.png", self.image, "image/png")})
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(list(settings.media_dir.iterdir()), [])
        self.assertEqual(one("SELECT COUNT(*) AS n FROM assets")["n"], 0)

    def test_version_rechecks_project_after_copy_and_cleans_staging(self):
        statistics = versions._snapshot_stats

        def stats_then_delete(*args, **kwargs):
            result = statistics(*args, **kwargs)
            self.delete_project()
            return result

        with patch.object(versions, "_snapshot_stats", side_effect=stats_then_delete):
            response = self.client.post(self.url + "/versions", json={"approved_only": False})
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(list(settings.versions_dir.iterdir()), [])
        self.assertEqual(one("SELECT COUNT(*) AS n FROM dataset_versions")["n"], 0)

    def test_training_create_rechecks_version_before_enqueue(self):
        version = self.version()
        canonical_hash = training._canonical_hash

        def hash_then_delete(*args, **kwargs):
            result = canonical_hash(*args, **kwargs)
            self.delete_project()
            return result

        with patch.object(training, "training_availability", return_value={"available": True, "backend": "test", "backend_version": "1"}), \
             patch.object(training, "_resolve_base_model", return_value=(Path("test.pt"), {"sha256": "test", "size_bytes": 1})), \
             patch.object(training, "training_resources", return_value={"devices": [], "cuda_available": False}), \
             patch.object(training, "_canonical_hash", side_effect=hash_then_delete):
            response = self.client.post(self.url + "/training-runs", json={"version_id": version["id"], "device": "cpu"})
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(one("SELECT COUNT(*) AS n FROM training_runs")["n"], 0)

    def test_conversion_create_rechecks_run_before_enqueue(self):
        run_id, _ = self.completed_run()

        def validate_then_delete(*args):
            self.delete_project()
            return {}

        with patch.object(training, "_validate_conversion_config", side_effect=validate_then_delete):
            response = self.client.post(f"/api/training-runs/{run_id}/conversions", json={"format": "onnx"})
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(one("SELECT COUNT(*) AS n FROM model_conversions")["n"], 0)

    def test_version_metadata_reads_stay_in_one_snapshot_during_delete(self):
        version = self.version()
        original_db = versions.db
        deleted = False

        class Connection:
            def __init__(inner, conn):
                inner.conn = conn

            def execute(inner, sql, *args):
                nonlocal deleted
                if "FROM dataset_version_labels" in sql and not deleted:
                    deleted = True
                    self.delete_project()
                return inner.conn.execute(sql, *args)

        @contextmanager
        def snapshot_db():
            with original_db() as conn:
                yield Connection(conn)

        with patch.object(versions, "db", snapshot_db):
            frozen, labels, assets, annotations = versions._load_snapshot_rows(version["id"])
        self.assertTrue(deleted)
        self.assertEqual(frozen["id"], version["id"])
        self.assertEqual((len(labels), len(assets), len(annotations)), (1, 2, 2))

    def test_frozen_export_during_project_delete_returns_conflict(self):
        version = self.version()
        archive_write = zipfile.ZipFile.write

        def delete_then_write(archive, filename, *args, **kwargs):
            self.delete_project()
            return archive_write(archive, filename, *args, **kwargs)

        with patch.object(zipfile.ZipFile, "write", new=delete_then_write):
            response = self.client.get(f"/api/versions/{version['id']}/export/coco")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse(response.headers.get("content-type", "").startswith("application/zip"))

    def test_deployment_export_during_delete_returns_conflict_and_cleans_zip(self):
        run_id, artifact = self.completed_run()
        archive_write, temporary_file = zipfile.ZipFile.write, tempfile.NamedTemporaryFile
        destinations = []

        def delete_then_write(archive, filename, *args, **kwargs):
            if Path(filename) == artifact:
                self.delete_project()
            return archive_write(archive, filename, *args, **kwargs)

        def record_temp(*args, **kwargs):
            handle = temporary_file(*args, **kwargs)
            destinations.append(Path(handle.name))
            return handle

        with patch.object(zipfile.ZipFile, "write", new=delete_then_write), \
             patch.object(tempfile, "NamedTemporaryFile", side_effect=record_temp):
            response = self.client.get(f"/api/training-runs/{run_id}/deployment")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertTrue(destinations)
        self.assertTrue(all(not path.exists() for path in destinations))

    def test_training_download_finishes_when_project_is_deleted_before_response(self):
        run_id, artifact = self.completed_run()
        expected = artifact.read_bytes()
        pin = training.pinned_file_response

        def pin_then_delete(*args, **kwargs):
            response = pin(*args, **kwargs)
            self.delete_project()
            return response

        with patch.object(training, "pinned_file_response", side_effect=pin_then_delete):
            response = self.client.get(f"/api/training-runs/{run_id}/artifact")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, expected)
        self.assertFalse(artifact.exists())
        self.assertEqual(list((settings.training_dir / ".downloads").iterdir()), [])

    def test_conversion_range_download_finishes_when_project_is_deleted(self):
        run_id, _ = self.completed_run()
        with db() as conn:
            conversion_id = conn.execute("""INSERT INTO model_conversions
                (training_run_id,format,status,created_by,created_at)
                VALUES (?,'onnx','completed','test','2026-10-22')""", (run_id,)).lastrowid
            artifact = settings.training_dir / f"conversion-{conversion_id}" / "model.onnx"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"converted-model")
            conn.execute("UPDATE model_conversions SET artifact_path=? WHERE id=?", (str(artifact), conversion_id))
        pin = training.pinned_file_response

        def pin_then_delete(*args, **kwargs):
            response = pin(*args, **kwargs)
            self.delete_project()
            return response

        with patch.object(training, "pinned_file_response", side_effect=pin_then_delete):
            response = self.client.get(f"/api/conversions/{conversion_id}/artifact", headers={"Range": "bytes=0-8"})
        self.assertEqual(response.status_code, 206, response.text)
        self.assertEqual(response.content, b"converted")
        self.assertFalse(artifact.exists())
        self.assertEqual(list((settings.training_dir / ".downloads").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
