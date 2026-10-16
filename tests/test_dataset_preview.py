from __future__ import annotations

import importlib
import io
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image


class DatasetPreviewTest(unittest.TestCase):
    def setUp(self):
        from app.auth import Actor, get_actor
        from app.config import settings
        from app.database import db, one
        self.settings, self.db, self.one = settings, db, one
        self.Actor = Actor
        main = importlib.import_module("app.main")
        self.temp = tempfile.TemporaryDirectory(prefix="mdp-version-preview-")
        values = {"data_dir": Path(self.temp.name), "embedded_worker": False,
                  "embedded_pipeline_worker": False, "auth_mode": "disabled"}
        self.previous = {key: getattr(settings, key) for key in values}
        for key, value in values.items():
            object.__setattr__(settings, key, value)
        app = FastAPI(lifespan=main.lifespan)
        app.include_router(main.app.router)
        self.actor = Actor("version-preview-test", "admin")
        app.dependency_overrides[get_actor] = lambda: self.actor
        self.client = TestClient(app).__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for key, value in self.previous.items():
            object.__setattr__(self.settings, key, value)
        self.temp.cleanup()

    def fixture(self, count=3, ratios=(0.7, 0.2, 0.1)):
        created = self.client.post("/api/projects", json={"name": "Frozen preview", "task_type": "detect", "labels": ["object"]})
        self.assertEqual(created.status_code, 201, created.text)
        project = self.client.get(f"/api/projects/{created.json()['id']}").json()
        assets = []
        for index in range(count):
            image = io.BytesIO()
            Image.new("RGB", (128, 80), (index + 20, 70, 100)).save(image, "PNG")
            uploaded = self.client.post(f"/api/projects/{project['id']}/assets", files={"files": (f"frame-{index}.png", image.getvalue(), "image/png")})
            self.assertEqual(uploaded.status_code, 201, uploaded.text)
            asset_id = uploaded.json()["asset_ids"][0]
            annotation = self.client.post(f"/api/assets/{asset_id}/annotations", json={"label_id": project["labels"][0]["id"], "shape_type": "rectangle", "points": [10, 10, 60, 60]})
            self.assertEqual(annotation.status_code, 201, annotation.text)
            assets.append(asset_id)
        response = self.client.post(f"/api/projects/{project['id']}/versions", json={"name": "Preview version", "approved_only": False, "train_ratio": ratios[0], "valid_ratio": ratios[1], "test_ratio": ratios[2]})
        self.assertEqual(response.status_code, 201, response.text)
        return project, assets, response.json()

    def test_frozen_images_and_annotations_survive_original_deletion(self):
        project, original_ids, version = self.fixture()
        before = self.client.get(f"/api/versions/{version['id']}/assets").json()
        sample = before["items"][0]
        full_image = self.client.get(sample["image_url"])
        thumb = self.client.get(sample["thumbnail_url"] + "?size=128")
        self.assertEqual((full_image.status_code, thumb.status_code), (200, 200))
        self.assertEqual(thumb.headers["content-type"], "image/webp")
        targets = [{"asset_id": asset_id, "expected_revision": self.one("SELECT revision FROM assets WHERE id=?", (asset_id,))["revision"]} for asset_id in original_ids]
        deleted = self.client.post(f"/api/projects/{project['id']}/assets/delete-batch", json={"assets": targets})
        self.assertEqual(deleted.json()["counts"]["succeeded"], len(original_ids), deleted.text)
        self.assertEqual(self.client.get(f"/api/projects/{project['id']}/assets").json(), [])
        # A fresh cache miss after deletion proves the immutable copy is read.
        for cached in self.settings.thumbnail_dir.glob("version-*.webp"):
            cached.unlink()
        self.assertEqual(self.client.get(sample["image_url"]).content, full_image.content)
        self.assertEqual(self.client.get(sample["thumbnail_url"] + "?size=128").content, thumb.content)
        self.assertEqual(self.client.get(f"/api/versions/{version['id']}/assets").json(), before)
        self.assertTrue(all(item["annotation_count"] == 1 for item in before["items"]))

    def test_split_pagination_and_representative_samples(self):
        project, _, version = self.fixture(11, (0.5, 0.3, 0.2))
        expected_counts = {"train": 6, "valid": 3, "test": 2}
        all_ids = []
        for offset in (0, 4, 8):
            page = self.client.get(f"/api/versions/{version['id']}/assets?limit=4&offset={offset}").json()
            self.assertEqual(page["total"], 11)
            self.assertEqual(page["has_more"], offset < 8)
            all_ids.extend(item["id"] for item in page["items"])
            self.assertFalse(any("storage_name" in item or "source_created_at" in item for item in page["items"]))
        self.assertEqual(len(set(all_ids)), 11)
        self.assertEqual(all_ids, sorted(all_ids))
        for split, count in expected_counts.items():
            page = self.client.get(f"/api/versions/{version['id']}/assets?split={split}").json()
            self.assertEqual(page["total"], count)
            self.assertTrue(all(item["split"] == split for item in page["items"]))
        listed = self.client.get(f"/api/projects/{project['id']}/versions").json()[0]
        self.assertEqual(len(listed["preview_assets"]), 6)
        self.assertEqual([item["split"] for item in listed["preview_assets"]], ["train", "train", "valid", "valid", "test", "test"])
        self.assertTrue(all(item["thumbnail_url"].startswith(f"/api/versions/{version['id']}/assets/") for item in listed["preview_assets"]))

    def test_version_membership_missing_media_and_path_escape_are_rejected(self):
        _, _, first = self.fixture(1)
        _, _, second = self.fixture(1)
        sample = first["preview_assets"][0]
        self.assertEqual(self.client.get(f"/api/versions/{second['id']}/assets/{sample['id']}/image").status_code, 404)
        self.assertEqual(self.client.get(f"/api/versions/{second['id']}/assets/{sample['id']}/thumbnail").status_code, 404)
        row = self.one("SELECT * FROM dataset_version_assets WHERE id=?", (sample["id"],))
        snapshot = self.settings.versions_dir / f"version-{first['id']}" / row["storage_name"]
        snapshot.unlink()
        for suffix in ("image", "thumbnail"):
            result = self.client.get(f"/api/versions/{first['id']}/assets/{sample['id']}/{suffix}")
            self.assertEqual(result.status_code, 404)
            self.assertNotIn(self.temp.name, result.text)
        outside = self.settings.versions_dir / "outside.png"
        Image.new("RGB", (40, 40), "red").save(outside)
        with self.db() as conn:
            conn.execute("UPDATE dataset_version_assets SET storage_name='../outside.png' WHERE id=?", (sample["id"],))
        self.assertEqual(self.client.get(sample["image_url"]).status_code, 404)
        self.assertEqual(self.client.get(sample["thumbnail_url"]).status_code, 404)

    def test_readonly_role_and_query_validation(self):
        _, _, version = self.fixture(2, (1, 0, 0))
        self.actor = self.Actor("annotator", "annotator")
        base = f"/api/versions/{version['id']}/assets"
        empty = self.client.get(base + "?split=test").json()
        self.assertEqual((empty["total"], empty["items"], empty["has_more"]), (0, [], False))
        self.assertEqual(self.client.get(version["preview_assets"][0]["thumbnail_url"]).status_code, 200)
        for query in ("split=unknown", "offset=-1", "limit=0", "limit=97"):
            self.assertEqual(self.client.get(base + "?" + query).status_code, 422)
        for size in (0, 63, 769):
            self.assertEqual(self.client.get(version["preview_assets"][0]["thumbnail_url"] + f"?size={size}").status_code, 422)
        self.assertEqual(self.client.get("/api/versions/999999/assets").status_code, 404)


if __name__ == "__main__":
    unittest.main()
