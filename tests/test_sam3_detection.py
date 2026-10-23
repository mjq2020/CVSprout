from __future__ import annotations

import asyncio
import importlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image


@unittest.skipUnless(importlib.util.find_spec("torch") and importlib.util.find_spec("cv2"),
                     "requires CPU model dependencies")
class Sam3DetectionApiTest(unittest.TestCase):
    """Exercise the real registry/provider/preview/job chain using tensor outputs.

    Only the SAM checkpoint loader, processor and dependency status are fakes;
    predict(), task hints, geometry conversion, filtering and DB writes are real.
    """

    def setUp(self):
        import torch
        from app.auth import Actor, get_actor
        from app.config import settings
        from app.database import all_rows, db, one
        from app.providers import local, registry

        self.main = importlib.import_module("app.main")
        self.temp = tempfile.TemporaryDirectory(prefix="cvsprout-sam3-detection-")
        self.root = Path(self.temp.name)
        self.settings, self.db, self.one, self.all_rows = settings, db, one, all_rows
        self.local = local
        values = dict(data_dir=self.root / "data", models_dir=self.root / "models",
                      auth_mode="disabled", worker_url="", model_device="cpu",
                      embedded_worker=False, embedded_pipeline_worker=False,
                      qwen_base_url="", enable_demo_model=False,
                      sam2_checkpoint=self.root / "models/sam2/sam2.1_t.pt",
                      sam3_checkpoint=self.root / "models/sam3/sam3.pt",
                      sam3_source_dir=self.root / "missing-sdk",
                      training_base_model=self.root / "models/training/missing.pt",
                      training_segmentation_model=self.root / "models/training/missing-seg.pt",
                      training_model_dirs_raw=str(self.root / "models/training"))
        self.previous = {key: getattr(settings, key) for key in values}
        for key, value in values.items():
            object.__setattr__(settings, key, value)

        masks = torch.zeros((3, 1, 80, 128), dtype=torch.bool)
        masks[0, 0, 20:50, 25:55] = True
        masks[0, 0, 5:10, 80:90] = True  # disconnected part of the same instance
        masks[1, 0, 10:30, 60:100] = True
        # The last empty mask exercises the valid native detection-box fallback.
        self.processor = Mock()
        self.processor.set_image.return_value = {"fixture": True}
        self.processor.set_text_prompt.return_value = {
            "masks": masks,
            "boxes": torch.tensor([[8, 10, 70, 60], [20, 5, 90, 65], [5, 5, 15, 15]], dtype=torch.float32),
            "scores": torch.tensor([0.9, 0.2, 0.02], dtype=torch.float32),
        }
        self.patches = [
            patch.object(local, "local_sam3_runtime_status", return_value=(True, "")),
            patch.object(registry, "local_sam3_runtime_status", return_value=(True, "")),
            patch.object(local, "_load_sam3", return_value=(self.processor, None, "cpu")),
        ]
        self.status, self.registry_status, self.loader = [item.start() for item in self.patches]
        app = FastAPI(lifespan=self.main.lifespan)
        app.include_router(self.main.app.router)
        app.dependency_overrides[get_actor] = lambda: Actor("sam3-detection-test", "admin")
        self.client = TestClient(app).__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for mocked in reversed(self.patches):
            mocked.stop()
        for key, value in self.previous.items():
            object.__setattr__(self.settings, key, value)
        self.temp.cleanup()

    def fixture(self, task="detect", count=1):
        payload = {"name": f"SAM3 {task}", "task_type": task, "labels": ["object", "other"]}
        if task == "pose":
            payload["keypoint_schema"] = {"names": ["head", "tail"]}
        response = self.client.post("/api/projects", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        project = self.client.get(f"/api/projects/{response.json()['id']}").json()
        assets = []
        for index in range(count):
            image = io.BytesIO()
            Image.new("RGB", (128, 80), (30 + index, 50, 80)).save(image, "PNG")
            response = self.client.post(f"/api/projects/{project['id']}/assets", files={
                "files": (f"frame-{index}.png", image.getvalue(), "image/png")})
            self.assertEqual(response.status_code, 201, response.text)
            assets.extend(response.json()["asset_ids"])
        return project, assets

    def request(self, project, assets, endpoint="annotation-preview", **options):
        return self.client.post(f"/api/projects/{project['id']}/{endpoint}", json={
            "model_id": "sam3", "prompt": "object", "target_labels": ["object"],
            "asset_ids": assets, "confidence_threshold": 0.4, **options})

    def snapshot(self):
        with self.db() as connection:
            return list(connection.iterdump())

    def run_job(self, project, assets, **options):
        from app import jobs

        response = self.request(project, assets, endpoint="jobs", **options)
        self.assertEqual(response.status_code, 202, response.text)
        job_id = response.json()["id"]
        claimed = jobs.claim_next_job("sam3-cpu-fixture")
        self.assertEqual(claimed, job_id)
        # jobs.get_provider is deliberately real: worker instantiates SAM3 from
        # the registry and invokes the same native provider as the HTTP preview.
        asyncio.run(jobs.run_annotation_job(job_id, "sam3-cpu-fixture"))
        job = self.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        self.assertEqual(job["status"], "completed", job)
        rows = self.all_rows("""SELECT n.* FROM annotations n JOIN assets a ON a.id=n.asset_id
            WHERE a.project_id=? AND n.deleted_at IS NULL ORDER BY n.asset_id,n.id""", (project["id"],))
        return job, rows

    def assert_polygon(self, annotation):
        self.assertEqual(annotation["shape_type"], "polygon")
        points = annotation.get("points")
        if points is None:
            points = json.loads(annotation["points_json"])
        self.assertGreaterEqual(len(points), 6)
        self.assertEqual((min(points[0::2]), max(points[0::2]), min(points[1::2]), max(points[1::2])),
                         (25, 54, 20, 49))

    def test_registry_exposes_only_detection_and_segmentation_and_builds_real_provider(self):
        from app.providers import get_provider

        response = self.client.get("/api/models")
        self.assertEqual(response.status_code, 200, response.text)
        model = next(item for item in response.json() if item["id"] == "sam3")
        self.assertEqual(model["tasks"], ["detect", "segment"])
        self.assertTrue(model["configured"])
        self.assertIsInstance(get_provider("sam3"), self.local.LocalSam3Provider)
        self.loader.assert_not_called()

    def test_detection_preview_returns_full_mask_bounding_rectangle_without_database_writes(self):
        project, assets = self.fixture()
        before = self.snapshot()
        response = self.request(project, assets)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["preview_only"])
        self.assertEqual(body["task_type"], "detect")
        self.assertEqual(body["totals"]["predicted"], 3)
        self.assertEqual(body["totals"]["accepted"], 1)
        self.assertEqual(body["totals"]["filtered_confidence"], 2)
        self.assertEqual(body["totals"]["filtered_geometry"], 0)
        annotation = body["items"][0]["annotations"][0]
        self.assertEqual(annotation["shape_type"], "rectangle")
        self.assertEqual(annotation["points"], [25, 5, 90, 50])
        self.assertEqual(annotation["label_id"], project["labels"][0]["id"])
        self.assertEqual(self.snapshot(), before)
        self.loader.assert_called_once_with(str(self.settings.sam3_checkpoint), str(self.settings.sam3_source_dir), "cpu")

    def test_detection_project_overrides_client_task_type_in_preview_and_worker(self):
        project, assets = self.fixture()
        before = self.snapshot()
        for forged in ("segment", "classify", "pose", "mixed"):
            with self.subTest(forged=forged):
                response = self.request(project, assets, hints={"task_type": forged})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["items"][0]["annotations"][0]["shape_type"], "rectangle")
        self.assertEqual(self.snapshot(), before)
        job, rows = self.run_job(project, assets, hints={"task_type": "segment"})
        self.assertEqual(job["created_count"], 1)
        self.assertEqual(rows[0]["shape_type"], "rectangle")
        self.assertEqual(json.loads(rows[0]["points_json"]), [25, 5, 90, 50])

    def test_batch_worker_persists_rectangles_for_every_selected_image_with_threshold(self):
        project, assets = self.fixture(count=2)
        job, rows = self.run_job(project, assets)
        self.assertEqual((job["processed"], job["created_count"]), (2, 2))
        self.assertEqual([row["asset_id"] for row in rows], assets)
        self.assertEqual(self.loader.call_count, 2)
        for row in rows:
            self.assertEqual((row["model_id"], row["source"], row["shape_type"]), ("sam3", "auto", "rectangle"))
            self.assertEqual(json.loads(row["points_json"]), [25, 5, 90, 50])
            self.assertGreaterEqual(row["confidence"], 0.4)

    def test_segmentation_keeps_polygons_in_preview_and_batch_despite_forged_detection_hint(self):
        project, assets = self.fixture(task="segment")
        before = self.snapshot()
        response = self.request(project, assets, hints={"task_type": "detect"})
        self.assertEqual(response.status_code, 200, response.text)
        annotation = response.json()["items"][0]["annotations"][0]
        self.assert_polygon(annotation)
        self.assertEqual(self.snapshot(), before)
        job, rows = self.run_job(project, assets, hints={"task_type": "detect"})
        self.assertEqual(job["created_count"], 1)
        self.assert_polygon(rows[0])
        self.assertEqual(json.loads(rows[0]["points_json"]), annotation["points"])

    def test_mixed_project_preserves_polygon_behavior_and_ignores_forged_task(self):
        project, assets = self.fixture(task="mixed")
        before = self.snapshot()
        for hints in (None, {"task_type": "detect"}):
            response = self.request(project, assets, hints=hints)
            self.assertEqual(response.status_code, 200, response.text)
            self.assert_polygon(response.json()["items"][0]["annotations"][0])
        self.assertEqual(self.snapshot(), before)

    def test_classification_and_pose_projects_are_rejected_before_model_inference(self):
        for task in ("classify", "pose"):
            with self.subTest(task=task):
                project, assets = self.fixture(task=task)
                before = self.snapshot()
                for endpoint in ("annotation-preview", "jobs"):
                    response = self.request(project, assets, endpoint=endpoint, hints={"task_type": "detect"})
                    self.assertEqual(response.status_code, 409, response.text)
                    self.assertIn("不支持项目任务类型", response.json()["detail"])
                self.assertEqual(self.snapshot(), before)
        self.loader.assert_not_called()

    def test_low_threshold_exposes_faint_box_fallback_and_high_threshold_filters_everything(self):
        project, assets = self.fixture()
        before = self.snapshot()
        low = self.request(project, assets, confidence_threshold=0.01)
        self.assertEqual(low.status_code, 200, low.text)
        self.assertEqual(low.json()["totals"]["accepted"], 3)
        self.assertEqual(low.json()["items"][0]["annotations"][-1]["points"], [5, 5, 15, 15])
        self.processor.set_confidence_threshold.assert_called_with(0.01)
        high = self.request(project, assets, confidence_threshold=0.95)
        self.assertEqual(high.status_code, 200, high.text)
        self.assertEqual(high.json()["totals"]["accepted"], 0)
        self.assertEqual(high.json()["totals"]["filtered_confidence"], 3)
        self.processor.set_confidence_threshold.assert_called_with(0.05)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
