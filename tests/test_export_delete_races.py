from __future__ import annotations

import importlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from app.config import settings
from app.database import one

main = importlib.import_module("app.main")
versions = importlib.import_module("app.dataset_versions")


class ExportDeleteRaceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mdp-export-delete-race-")
        values = dict(data_dir=Path(self.temp.name), embedded_worker=False,
                      embedded_pipeline_worker=False, auth_mode="disabled")
        self.previous = {key: getattr(settings, key) for key in values}
        for key, value in values.items():
            object.__setattr__(settings, key, value)
        self.client = TestClient(main.app).__enter__()
        result = self.client.post("/api/projects", json={
            "name": "Deletion race", "task_type": "detect", "labels": ["object"]})
        self.assertEqual(result.status_code, 201, result.text)
        self.project_id = result.json()["id"]
        image = io.BytesIO()
        Image.new("RGB", (64, 64), "blue").save(image, "PNG")
        result = self.client.post(f"/api/projects/{self.project_id}/assets", files={
            "files": ("race.png", image.getvalue(), "image/png")})
        self.assertEqual(result.status_code, 201, result.text)
        self.asset_id = result.json()["asset_ids"][0]
        self.media = settings.media_dir / one("SELECT storage_name FROM assets WHERE id=?", (self.asset_id,))["storage_name"]

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for key, value in self.previous.items():
            object.__setattr__(settings, key, value)
        self.temp.cleanup()

    def test_delete_between_version_file_check_and_copy_returns_conflict_and_cleans_staging(self):
        copyfile = versions.shutil.copyfile

        def delete_then_copy(source, destination):
            Path(source).unlink()
            return copyfile(source, destination)

        with patch.object(versions.shutil, "copyfile", side_effect=delete_then_copy):
            response = self.client.post(f"/api/projects/{self.project_id}/versions", json={"approved_only": False})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("刷新图片列表后重试", response.json()["detail"])
        self.assertEqual(one("SELECT COUNT(*) AS n FROM dataset_versions")["n"], 0)
        self.assertEqual(list(settings.versions_dir.iterdir()), [])

    def test_missing_live_image_never_returns_an_incomplete_archive(self):
        self.media.unlink()
        for export_format in ("coco", "yolo"):
            response = self.client.get(f"/api/projects/{self.project_id}/export/{export_format}")
            self.assertEqual(response.status_code, 409, response.text)
            self.assertIn("race.png", response.json()["detail"])

    def test_delete_between_export_file_check_and_open_returns_conflict(self):
        archive_write = zipfile.ZipFile.write

        def delete_then_write(archive, filename, *args, **kwargs):
            Path(filename).unlink()
            return archive_write(archive, filename, *args, **kwargs)

        with patch.object(zipfile.ZipFile, "write", new=delete_then_write):
            response = self.client.get(f"/api/projects/{self.project_id}/export/coco")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("刷新图片列表后重试", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
