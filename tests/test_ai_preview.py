from __future__ import annotations

import asyncio
import importlib
import io
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image


class FakeProvider:
    def __init__(self, predictions=()):
        from app.providers.base import ModelInfo
        self.info = ModelInfo("preview-test", "测试模型", "Test", "", [], ["auto", "assist"], True)
        self.predictions = list(predictions)
        self.calls = []
        self.error = None
        self.started = None
        self.release = None

    async def predict(self, image_path, width, height, prompt, labels, hints=None):
        self.calls.append({"filename": image_path.name, "prompt": prompt, "labels": labels, "hints": hints})
        if self.started:
            self.started.set()
        if self.release:
            await asyncio.to_thread(self.release.wait, 3)
        if self.error:
            raise self.error
        return self.predictions


class AiPreviewTest(unittest.TestCase):
    def setUp(self):
        from app.auth import Actor, get_actor
        from app.config import settings
        from app.database import db, one, all_rows
        from app.providers.base import Prediction
        self.main = importlib.import_module("app.main")
        self.preview_module = importlib.import_module("app.annotation_preview")
        self.Actor, self.Prediction = Actor, Prediction
        self.settings, self.db, self.one, self.all_rows = settings, db, one, all_rows
        self.temp = tempfile.TemporaryDirectory(prefix="mdp-ai-preview-test-")
        values = {"data_dir": Path(self.temp.name), "embedded_worker": False,
                  "embedded_pipeline_worker": False, "auth_mode": "disabled"}
        self.previous = {key: getattr(settings, key) for key in values}
        for key, value in values.items():
            object.__setattr__(settings, key, value)
        app = FastAPI(lifespan=self.main.lifespan)
        app.include_router(self.main.app.router)
        self.actor = Actor("ai-preview-test", "admin")
        app.dependency_overrides[get_actor] = lambda: self.actor
        self.client = TestClient(app).__enter__()
        self.provider = FakeProvider([Prediction("object", "rectangle", [10, 10, 60, 60], 0.9)])
        self.model = {"id": "preview-test", "provider_type": "openai_vision", "timeout_seconds": 30,
                      "tasks": ["detect", "segment", "classify", "pose"]}
        self.patches = [patch.object(self.main, "get_provider", return_value=self.provider),
                        patch.object(self.main, "get_model", return_value=self.model)]
        for mocked in self.patches:
            mocked.start()

    def tearDown(self):
        if self.provider.release:
            self.provider.release.set()
        self.client.__exit__(None, None, None)
        for mocked in self.patches:
            mocked.stop()
        for key, value in self.previous.items():
            object.__setattr__(self.settings, key, value)
        self.temp.cleanup()

    def fixture(self, count=1, task="detect"):
        payload = {"name": "Preview test", "task_type": task, "labels": ["object", "other"]}
        if task == "pose":
            payload["keypoint_schema"] = {"names": ["head", "tail"], "skeleton": [[0, 1]]}
        result = self.client.post("/api/projects", json=payload)
        self.assertEqual(result.status_code, 201, result.text)
        project = self.client.get(f"/api/projects/{result.json()['id']}").json()
        assets = []
        for index in range(count):
            image = io.BytesIO()
            Image.new("RGB", (128, 80), (20 + index, 30, 50)).save(image, "PNG")
            result = self.client.post(f"/api/projects/{project['id']}/assets",
                                      files={"files": (f"frame-{index}.png", image.getvalue(), "image/png")})
            self.assertEqual(result.status_code, 201, result.text)
            assets.extend(result.json()["asset_ids"])
        return project, assets

    def request(self, project, assets, **overrides):
        return self.client.post(f"/api/projects/{project['id']}/annotation-preview", json={
            "model_id": "preview-test", "prompt": "Find objects", "target_labels": ["object"],
            "asset_ids": assets, **overrides})

    def snapshot(self):
        with self.db() as conn:
            return list(conn.iterdump())

    def test_preview_is_read_only_and_filters_selected_labels_confidence_geometry(self):
        project, assets = self.fixture()
        self.client.post(f"/api/assets/{assets[0]}/lock")
        p = self.Prediction
        self.provider.predictions = [
            p("OBJECT", "rectangle", [10, 10, 60, 60], 0.9),
            p("object", "rectangle", [20, 20, 40, 40], None),
            p("object", "rectangle", [10, 10, 30, 30], 0.2),
            p("other", "rectangle", [10, 10, 30, 30], 0.99),
            p("unknown", "rectangle", [10, 10, 30, 30], 0.99),
            p("object", "rectangle", [60, 10, 20, 30], 0.99),
            p("object", "polygon", [1, 1, 20, 1, 10, 20], 0.99),
            p("object", "rectangle", [10, 10, 30, 30], float("nan")),
        ]
        before = self.snapshot()
        result = self.request(project, assets, confidence_threshold=0.4)
        self.assertEqual(result.status_code, 200, result.text)
        body = result.json()
        self.assertTrue(body["preview_only"])
        self.assertEqual(body["totals"], {"predicted": 8, "accepted": 2, "filtered_unknown_label": 1,
                          "filtered_target_label": 1, "filtered_confidence": 1, "filtered_geometry": 3,
                          "filtered_classification": 0})
        self.assertEqual(body["items"][0]["annotations"][0]["label_id"], project["labels"][0]["id"])
        self.assertTrue(body["items"][0]["warnings"])
        self.assertEqual(body["items"][0]["image_url"], f"/api/assets/{assets[0]}/image")
        self.assertEqual(self.provider.calls[0]["hints"]["confidence_threshold"], 0.4)
        self.assertEqual(self.snapshot(), before)

    def test_preview_and_job_share_prompt_attributes_threshold_targets_and_outputs(self):
        from app import jobs
        project, assets = self.fixture()
        schema = {"type": "object", "properties": {"visible": {"type": "boolean"}}, "required": ["visible"]}
        with self.db() as conn:
            conn.execute("UPDATE labels SET attributes_schema_json=? WHERE id=?", (json.dumps(schema), project["labels"][0]["id"]))
        p = self.Prediction
        self.provider.predictions = [p("object", "rectangle", [10, 10, 60, 60], 0.7, {"visible": True}),
                                     p("other", "rectangle", [10, 10, 60, 60], 0.9),
                                     p("object", "rectangle", [10, 10, 60, 60], 0.1, {"visible": True}),
                                     p("object", "rectangle", [10, 10, 60, 60], 0.9)]
        preview = self.request(project, assets, confidence_threshold=0.6).json()
        result = self.client.post(f"/api/projects/{project['id']}/jobs", json={"model_id": "preview-test",
               "prompt": "Find objects", "target_labels": ["object"], "confidence_threshold": 0.6, "asset_ids": assets})
        self.assertEqual(result.status_code, 202, result.text)
        job = jobs.claim_next_job("preview-parity")
        with patch.object(jobs, "get_provider", return_value=self.provider):
            asyncio.run(jobs.run_annotation_job(job, "preview-parity"))
        self.assertEqual(self.one("SELECT status FROM jobs WHERE id=?", (job,))["status"], "completed")
        stored = self.all_rows("SELECT * FROM annotations WHERE asset_id=? AND deleted_at IS NULL", (assets[0],))
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["label_id"], preview["items"][0]["annotations"][0]["label_id"])
        self.assertEqual(json.loads(stored[0]["points_json"]), preview["items"][0]["annotations"][0]["points"])
        self.assertEqual(self.provider.calls[0], self.provider.calls[1])
        self.assertIn("visible", self.provider.calls[0]["prompt"])

    def test_concept_new_labels_are_virtual_until_batch_is_created(self):
        project, assets = self.fixture()
        self.model["provider_type"] = "local_grounding_dino"
        self.provider.predictions = [self.Prediction("new-object", "rectangle", [10, 10, 60, 60], 0.9)]
        before = self.snapshot()
        result = self.request(project, assets, prompt="new-object", target_labels=["new-object"])
        self.assertEqual(result.status_code, 200, result.text)
        body = result.json()
        self.assertEqual(body["pending_labels"], ["new-object"])
        self.assertIsNone(body["items"][0]["annotations"][0]["label_id"])
        self.assertTrue(body["warnings"])
        self.assertEqual(self.provider.calls[0]["prompt"], "new-object")
        self.assertEqual(self.snapshot(), before)
        self.actor = self.Actor("annotator", "annotator")
        result = self.request(project, assets, prompt="new-object", target_labels=["new-object"])
        self.assertEqual(result.status_code, 403)
        self.assertEqual(self.snapshot(), before)

    def test_classification_segmentation_and_keypoints_share_task_contract(self):
        p = self.Prediction
        predictions = {"classify": [p("object", "classification", [], 0.6), p("other", "classification", [], 0.95)],
                       "segment": [p("object", "polygon", [1, 1, 50, 1, 20, 50], 0.8)],
                       "pose": [p("object", "keypoints", [1, 1, 60, 60], 0.8, keypoints=[
                           {"x": 10, "y": 10, "visibility": 2}, {"x": 0, "y": 0, "visibility": 0}]) ]}
        for task in predictions:
            with self.subTest(task=task):
                project, assets = self.fixture(task=task)
                self.provider.predictions = predictions[task]
                before = self.snapshot()
                result = self.request(project, assets, target_labels=["object", "other"])
                self.assertEqual(result.status_code, 200, result.text)
                body = result.json()
                self.assertEqual(body["totals"]["accepted"], 1)
                self.assertEqual(body["task_type"], task)
                self.assertEqual(self.provider.calls[-1]["hints"]["task_type"], task)
                if task == "pose":
                    self.assertEqual(body["keypoint_schema"]["names"], ["head", "tail"])
                if task == "classify":
                    self.assertEqual(body["items"][0]["annotations"][0]["label_name"], "other")
                    self.assertEqual(body["totals"]["filtered_classification"], 1)
                self.assertEqual(self.snapshot(), before)

    def test_assist_hint_annotation_and_points_are_read_only_and_single_image(self):
        project, assets = self.fixture(count=2, task="segment")
        result = self.client.post(f"/api/assets/{assets[0]}/annotations", json={"label_id": project["labels"][0]["id"],
                      "shape_type": "polygon", "points": [1, 1, 60, 1, 30, 60]})
        self.assertEqual(result.status_code, 201, result.text)
        annotation = result.json()
        self.model["provider_type"] = "interactive_segmentation"
        self.provider.predictions = [self.Prediction("object", "polygon", [1, 1, 60, 1, 30, 60], 0.8)]
        before = self.snapshot()
        result = self.request(project, assets[:1], mode="assist", hint_annotation_id=annotation["id"])
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.provider.calls[0]["hints"]["points"], annotation["points"])
        self.assertEqual(self.provider.calls[0]["hints"]["label"], "object")
        self.assertEqual(self.request(project, assets, mode="assist", hint_annotation_id=annotation["id"]).status_code, 400)
        self.assertEqual(self.request(project, assets[1:], mode="assist", hint_annotation_id=annotation["id"]).status_code, 400)
        hints = {"points": [{"x": 20, "y": 20, "label": 1}]}
        self.assertEqual(self.request(project, assets[:1], mode="assist", hints=hints).status_code, 200)
        self.assertEqual(self.provider.calls[-1]["hints"]["points"], hints["points"])
        self.assertEqual(self.snapshot(), before)

    def test_asset_selection_validation_and_project_boundary_never_infer(self):
        project, assets = self.fixture(4)
        other, other_assets = self.fixture()
        before = self.snapshot()
        for ids in (None, [], assets, [assets[0], assets[0]], [True], ["1"], [0], [2**64]):
            with self.subTest(ids=ids):
                self.assertEqual(self.request(project, ids).status_code, 422)
        self.assertEqual(self.request(project, other_assets).status_code, 400)
        self.assertEqual(self.request(project, [999999]).status_code, 400)
        self.assertEqual(self.request({"id": 999999}, assets[:1]).status_code, 404)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.snapshot(), before)

    def test_review_roles_model_compatibility_and_password_rules(self):
        project, assets = self.fixture()
        with self.db() as conn:
            conn.execute("UPDATE assets SET review_status='approved' WHERE id=?", (assets[0],))
        before = self.snapshot()
        self.assertEqual(self.request(project, assets).status_code, 409)
        self.actor = self.Actor("annotator", "annotator")
        self.assertEqual(self.request(project, assets, include_reviewed=True).status_code, 403)
        for role in ("reviewer", "admin"):
            self.actor = self.Actor(role, role)
            self.assertEqual(self.request(project, assets, include_reviewed=True).status_code, 200)
        self.actor = self.Actor("unknown", "unknown")
        self.assertEqual(self.request(project, assets, include_reviewed=True).status_code, 403)
        self.actor = self.Actor("initial", "admin", must_change_password=True)
        self.assertEqual(self.request(project, assets, include_reviewed=True).status_code, 403)
        self.actor = self.Actor("admin", "admin")
        self.model["tasks"] = ["segment"]
        self.assertEqual(self.request(project, assets, include_reviewed=True).status_code, 409)
        self.model["tasks"] = ["detect"]
        self.provider.info.configured = False
        self.assertEqual(self.request(project, assets, include_reviewed=True).status_code, 409)
        self.assertEqual(self.snapshot(), before)

    def test_preview_orders_selected_images_and_missing_media_has_clear_error(self):
        project, assets = self.fixture(3)
        result = self.request(project, list(reversed(assets)))
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual([item["asset_id"] for item in result.json()["items"]], list(reversed(assets)))
        path = self.settings.media_dir / self.one("SELECT storage_name FROM assets WHERE id=?", (assets[0],))["storage_name"]
        path.unlink()
        before = self.snapshot()
        result = self.request(project, assets[:1])
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(self.snapshot(), before)

    def test_provider_failure_hides_internal_details_and_changes_nothing(self):
        project, assets = self.fixture()
        self.provider.error = RuntimeError("secret-key at /private/model")
        before = self.snapshot()
        response = self.request(project, assets)
        self.assertEqual(response.status_code, 502, response.text)
        self.assertNotIn("secret", response.text)
        self.assertEqual(self.snapshot(), before)
        self.provider.error = None
        self.assertEqual(self.request(project, assets).status_code, 200)

    def test_timeout_keeps_capacity_busy_until_native_work_finishes(self):
        project, assets = self.fixture(3)
        before = self.snapshot()
        self.provider.started, self.provider.release = threading.Event(), threading.Event()
        with patch.object(self.preview_module, "MAX_PREVIEW_SECONDS", 0.02):
            result = self.request(project, assets)
        self.assertEqual(result.status_code, 504, result.text)
        self.assertTrue(self.provider.started.is_set())
        self.assertEqual(self.request(project, assets).status_code, 429)
        self.assertEqual(len(self.provider.calls), 1)
        self.provider.release.set()
        for _ in range(100):
            if not self.preview_module._PREVIEW_SLOT.locked():
                break
            time.sleep(0.01)
        self.assertFalse(self.preview_module._PREVIEW_SLOT.locked())
        self.assertEqual(len(self.provider.calls), 1)  # Timed-out samples must never start.
        self.assertEqual(self.request(project, assets).status_code, 200)
        self.assertEqual(self.snapshot(), before)

    def test_simultaneous_previews_are_bounded_and_local_batch_is_respected(self):
        from app.providers.execution import _LOCAL_INFERENCE_LOCK
        project, assets = self.fixture()
        self.provider.started, self.provider.release = threading.Event(), threading.Event()
        before = self.snapshot()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.request, project, assets)
            self.assertTrue(self.provider.started.wait(2))
            second = self.request(project, assets)
            self.assertEqual(second.status_code, 429, second.text)
            self.provider.release.set()
            self.assertEqual(first.result(timeout=2).status_code, 200)
        self.model["provider_type"] = "local_grounding_dino"
        with _LOCAL_INFERENCE_LOCK:
            self.assertEqual(self.request(project, assets).status_code, 429)
        self.assertEqual(self.snapshot(), before)

    def test_cancelled_request_keeps_slot_until_provider_finishes(self):
        project, assets = self.fixture()
        before = self.snapshot()
        prepared = self.main._prepare_annotation_request(project["id"], self.main.AnnotationPreviewCreate(
                     model_id="preview-test", prompt="objects", asset_ids=assets), self.actor)
        self.provider.started, self.provider.release = threading.Event(), threading.Event()

        async def scenario():
            request = asyncio.create_task(self.preview_module.run_preview(*prepared[:3], prepared[3], prepared[4], "objects", prepared[5]))
            while not self.provider.started.is_set():
                await asyncio.sleep(0.001)
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            self.assertTrue(self.preview_module._PREVIEW_SLOT.locked())
            self.provider.release.set()
            for _ in range(100):
                if not self.preview_module._PREVIEW_SLOT.locked():
                    break
                await asyncio.sleep(0.01)
            self.assertFalse(self.preview_module._PREVIEW_SLOT.locked())

        asyncio.run(scenario())
        self.assertEqual(self.snapshot(), before)


class LocalPreviewExecutionTest(unittest.TestCase):
    def test_native_local_calls_hold_lock_even_when_await_is_cancelled(self):
        from app.providers.execution import serialized_local_inference, local_inference_busy
        entered, release, second_entered = threading.Event(), threading.Event(), threading.Event()

        @serialized_local_inference
        def first():
            entered.set()
            release.wait(3)

        @serialized_local_inference
        def second():
            second_entered.set()

        async def scenario():
            task = asyncio.create_task(asyncio.to_thread(first))
            await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(local_inference_busy())
            next_task = asyncio.create_task(asyncio.to_thread(second))
            await asyncio.sleep(0.02)
            self.assertFalse(second_entered.is_set())
            release.set()
            await next_task
            self.assertTrue(second_entered.is_set())
        try:
            asyncio.run(scenario())
        finally:
            release.set()

    def test_dino_and_sam3_receive_low_user_threshold_without_real_inference(self):
        from app.providers import local
        from app.providers.base import ModelInfo
        info = ModelInfo("fake", "Fake", "Test", "", [], ["auto"], True)
        fake_tensor = SimpleNamespace(detach=lambda: SimpleNamespace(cpu=lambda: SimpleNamespace(tolist=lambda: [])))
        dino_calls, sam_calls = [], []

        class DinoProcessor:
            def __call__(self, **kwargs):
                return {"input_ids": []}
            def post_process_grounded_object_detection(self, *args, **kwargs):
                dino_calls.append(kwargs)
                return [{"boxes": fake_tensor, "scores": fake_tensor, "text_labels": []}]

        class SamProcessor:
            def set_confidence_threshold(self, value):
                sam_calls.append(value)
            def set_image(self, image):
                return {}
            def set_text_prompt(self, **kwargs):
                return {"masks": [], "boxes": [], "scores": []}

        fake_torch = SimpleNamespace(inference_mode=nullcontext)
        with tempfile.TemporaryDirectory(prefix="mdp-provider-threshold-") as temp:
            image = Path(temp) / "input.png"
            Image.new("RGB", (40, 40)).save(image)
            with patch.dict("sys.modules", {"torch": fake_torch}), \
                 patch.object(local, "local_runtime_status", return_value=(True, "")), \
                 patch.object(local, "local_sam3_runtime_status", return_value=(True, "")), \
                 patch.object(local, "_load_grounding_dino", return_value=(DinoProcessor(), lambda **kwargs: {}, "cpu")), \
                 patch.object(local, "_load_sam3", return_value=(SamProcessor(), None, "cpu")):
                dino = local.LocalGroundingDinoProvider(info, "fake")
                sam = local.LocalSam3Provider(info, "fake", "fake")
                for provider in (dino, sam):
                    self.assertEqual(asyncio.run(provider.predict(image, 40, 40, "object", ["object"],
                                                             {"confidence_threshold": 0.01})), [])
        self.assertEqual(dino_calls[0]["threshold"], 0.01)
        self.assertEqual(dino_calls[0]["text_threshold"], 0.01)
        self.assertEqual(sam_calls, [0.01])

    def test_yolo_receives_low_user_threshold_without_real_inference(self):
        from app.providers import yolo
        from app.providers.base import ModelInfo
        calls = []

        class FakeYolo:
            task = "detect"
            def __init__(self, *args, **kwargs):
                pass
            def predict(self, **kwargs):
                calls.append(kwargs)
                return [SimpleNamespace(boxes=[])]

        info = ModelInfo("fake", "Fake", "Test", "", [], ["auto"], True)
        with tempfile.TemporaryDirectory(prefix="mdp-yolo-threshold-") as temp:
            path = Path(temp) / "fake.pt"
            path.write_bytes(b"fake")
            with patch.dict("sys.modules", {"ultralytics": SimpleNamespace(YOLO=FakeYolo)}), \
                 patch.object(yolo, "_cache", {}), patch.object(yolo, "_device", return_value="cpu"):
                provider = yolo.LocalYoloProvider(info, str(path), "detect", "cpu")
                result = asyncio.run(provider.predict(path, 40, 40, "object", ["object"], {"confidence_threshold": 0.01}))
        self.assertEqual(result, [])
        self.assertEqual(calls[0]["conf"], 0.01)


if __name__ == "__main__":
    unittest.main()
