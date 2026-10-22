from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from PIL import Image


TEST_DATA = tempfile.TemporaryDirectory(prefix="visionforge-test-")
os.environ["MDP_DATA_DIR"] = TEST_DATA.name
os.environ["MDP_AUTH_MODE"] = "disabled"
os.environ["MDP_ENABLE_DEMO_MODEL"] = "true"  # test-only deterministic provider
os.environ["MDP_EMBEDDED_WORKER"] = "true"
os.environ["MDP_WORKER_POLL_SECONDS"] = "0.02"
os.environ["MDP_EMBEDDED_PIPELINE_WORKER"] = "false"

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.providers.base import normalize_predictions  # noqa: E402
from app.providers import seed_builtin_models  # noqa: E402
from app.providers.demo import DemoProvider  # noqa: E402
from app.providers.local import (  # noqa: E402
    _sam3_mask_prediction,
    grounding_labels_from_prompt,
)
from app.database import db, now_iso, one  # noqa: E402
from app.config import settings  # noqa: E402
from app import training as training_module  # noqa: E402

if settings.data_dir.resolve() != Path(TEST_DATA.name).resolve():
    raise RuntimeError(
        "Tests must be isolated from application data; run: "
        "python -m unittest discover -s tests -v"
    )


def png_bytes(width: int = 320, height: int = 240, color: str = "#27364a") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), color).save(output, "PNG")
    return output.getvalue()


class InferenceHandler(BaseHTTPRequestHandler):
    last_payload: dict = {}

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        InferenceHandler.last_payload = payload
        label = payload["labels"][0]
        hint_points = payload.get("hints", {}).get("points", [])
        if hint_points:
            x, y = hint_points[0]["x"], hint_points[0]["y"]
            item = {
                "label": label,
                "polygon": [x - 8, y - 8, x + 8, y - 8, x + 8, y + 8, x - 8, y + 8],
                "confidence": 0.97,
                "attributes": {},
            }
        else:
            item = {"label": label, "bbox": [10, 12, 60, 70], "confidence": 0.92}
        body = json.dumps({"coordinate_space": "absolute", "objects": [item]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args) -> None:
        return


class ApiFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.inference_server = ThreadingHTTPServer(("127.0.0.1", 0), InferenceHandler)
        cls.inference_thread = threading.Thread(
            target=cls.inference_server.serve_forever, daemon=True
        )
        cls.inference_thread.start()
        cls.inference_endpoint = (
            f"http://127.0.0.1:{cls.inference_server.server_port}/predict"
        )
        cls.client_context = TestClient(app, headers={"X-MDP-Client": "browser"})
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client_context.__exit__(None, None, None)
        cls.inference_server.shutdown()
        cls.inference_server.server_close()
        cls.inference_thread.join(timeout=2)
        TEST_DATA.cleanup()

    def setUp(self) -> None:
        # Each test is independent even if the unittest loader recreates the app lifespan.
        seed_builtin_models()
        now = now_iso()
        with db() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO model_services
                   (id, name, family, provider_type, description, endpoint, model_name,
                    api_key_env, capabilities_json, modes_json, config_json, timeout_seconds,
                    enabled, builtin, created_at, updated_at)
                   VALUES ('demo', '开发测试模型', 'Test', 'demo', '仅用于自动化测试',
                           'internal://demo', 'demo', '', ?, ?, ?, 30, 1, 1, ?, ?)""",
                (json.dumps(["检测框"]), json.dumps(["auto", "assist"]),
                 json.dumps({"badge": "仅测试"}), now, now),
            )

    def create_project_with_image(self, name: str, color: str = "#27364a") -> tuple[int, int]:
        project = self.client.post("/api/projects", json={"name": name})
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["id"]
        uploaded = self.client.post(
            f"/api/projects/{project_id}/assets",
            files=[("files", ("frame.png", png_bytes(color=color), "image/png"))],
            headers={"X-Actor-ID": "alice"},
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        return project_id, uploaded.json()["asset_ids"][0]

    def create_training_version_fixture(
        self,
        name: str,
        *,
        rectangles: int = 2,
        polygons: int = 0,
    ) -> tuple[int, int, dict]:
        project = self.client.post("/api/projects", json={"name": name})
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["id"]
        annotation_count = rectangles + polygons
        stats = {
            "task_type": "segment" if polygons else "detect",
            "shape_types": {"rectangle": rectangles, "polygon": polygons},
            "splits": {
                "train": {"asset_count": 1, "annotation_count": max(1, annotation_count - 1)},
                "valid": {"asset_count": 1, "annotation_count": 1},
                "test": {"asset_count": 0, "annotation_count": 0},
            },
        }
        content_hash = f"{project_id:064x}"[-64:]
        with db() as conn:
            cursor = conn.execute(
                """INSERT INTO dataset_versions
                   (project_id, version_number, name, description, approved_only, split_seed,
                    train_ratio, valid_ratio, test_ratio, content_hash, asset_count,
                    annotation_count, stats_json, created_by, created_at)
                   VALUES (?, 1, 'fixture', '', 1, 42, 0.5, 0.5, 0, ?, 2, ?, ?, 'test', ?)""",
                (
                    project_id,
                    content_hash,
                    annotation_count,
                    json.dumps(stats),
                    now_iso(),
                ),
            )
            version_id = int(cursor.lastrowid)
        return project_id, version_id, {"content_hash": content_hash, "stats": stats}

    def wait_for_job(self, job_id: int, timeout: float = 5) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in {"completed", "failed", "cancelled"}:
                return job
            time.sleep(0.03)
        self.fail(f"job {job_id} did not finish before timeout")

    def test_concurrent_annotation_review_and_approved_export(self) -> None:
        project_id, asset_id = self.create_project_with_image("工业审核流程", "#182c40")
        label = self.client.post(
            f"/api/projects/{project_id}/labels",
            json={
                "name": "helmet",
                "color": "#38bdf8",
                "shortcut": "2",
                "attributes_schema": {
                    "type": "object",
                    "properties": {"occluded": {"type": "boolean"}},
                    "required": ["occluded"],
                    "additionalProperties": False,
                },
            },
            headers={"X-Actor-ID": "alice"},
        )
        self.assertEqual(label.status_code, 201, label.text)
        label_id = label.json()["id"]

        lock = self.client.post(
            f"/api/assets/{asset_id}/lock", headers={"X-Actor-ID": "alice"}
        )
        self.assertEqual(lock.status_code, 200, lock.text)
        revision = lock.json()["revision"]

        blocked = self.client.post(
            f"/api/assets/{asset_id}/annotations",
            json={
                "label_id": label_id,
                "shape_type": "rectangle",
                "points": [20, 30, 120, 180],
                "attributes": {"occluded": False},
                "expected_asset_revision": revision,
            },
            headers={"X-Actor-ID": "bob"},
        )
        self.assertEqual(blocked.status_code, 423)
        self.assertEqual(blocked.json()["detail"], "图片正由其他成员编辑，请稍后重试")
        self.assertNotIn("alice", blocked.text)

        invalid_attributes = self.client.post(
            f"/api/assets/{asset_id}/annotations",
            json={
                "label_id": label_id,
                "shape_type": "rectangle",
                "points": [20, 30, 120, 180],
                "attributes": {},
                "expected_asset_revision": revision,
            },
            headers={"X-Actor-ID": "alice"},
        )
        self.assertEqual(invalid_attributes.status_code, 422)

        invalid_geometry = self.client.post(
            f"/api/assets/{asset_id}/annotations",
            json={
                "label_id": label_id,
                "shape_type": "rectangle",
                "points": [120, 180, 20, 30],
                "attributes": {"occluded": False},
                "expected_asset_revision": revision,
            },
            headers={"X-Actor-ID": "alice"},
        )
        self.assertEqual(invalid_geometry.status_code, 422)

        created = self.client.post(
            f"/api/assets/{asset_id}/annotations",
            json={
                "label_id": label_id,
                "shape_type": "rectangle",
                "points": [20, 30, 120, 180],
                "attributes": {"occluded": False},
                "expected_asset_revision": revision,
            },
            headers={"X-Actor-ID": "alice"},
        )
        self.assertEqual(created.status_code, 201, created.text)
        annotation = created.json()
        revision = annotation["asset_revision"]

        stale = self.client.post(
            f"/api/assets/{asset_id}/annotations",
            json={
                "label_id": label_id,
                "shape_type": "rectangle",
                "points": [1, 1, 10, 10],
                "attributes": {"occluded": True},
                "expected_asset_revision": 0,
            },
            headers={"X-Actor-ID": "alice"},
        )
        self.assertEqual(stale.status_code, 409)

        updated = self.client.patch(
            f"/api/annotations/{annotation['id']}",
            json={
                "expected_version": annotation["version"],
                "points": [25, 35, 130, 190],
                "attributes": {"occluded": True},
            },
            headers={"X-Actor-ID": "alice"},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        annotation = updated.json()
        revision = annotation["asset_revision"]
        self.assertEqual(annotation["version"], 2)
        self.assertEqual(annotation["points"], [25.0, 35.0, 130.0, 190.0])

        label_delete = self.client.delete(
            f"/api/labels/{label_id}", headers={"X-Actor-ID": "alice"}
        )
        self.assertEqual(label_delete.status_code, 409)

        submitted = self.client.post(
            f"/api/assets/{asset_id}/workflow",
            json={"action": "submit", "expected_revision": revision, "reason": "初审"},
            headers={"X-Actor-ID": "alice"},
        )
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertEqual(submitted.json()["review_status"], "submitted")
        revision = submitted.json()["revision"]

        rejected_without_reason = self.client.post(
            f"/api/assets/{asset_id}/workflow",
            json={"action": "reject", "expected_revision": revision, "reason": ""},
            headers={"X-Actor-ID": "reviewer"},
        )
        self.assertEqual(rejected_without_reason.status_code, 200, rejected_without_reason.text)
        self.assertEqual(rejected_without_reason.json()["review_status"], "rejected")
        self.assertEqual(rejected_without_reason.json()["review_reason"], "")
        revision = rejected_without_reason.json()["revision"]

        resubmitted = self.client.post(
            f"/api/assets/{asset_id}/workflow",
            json={"action": "submit", "expected_revision": revision},
            headers={"X-Actor-ID": "alice"},
        )
        self.assertEqual(resubmitted.status_code, 200, resubmitted.text)
        self.assertEqual(resubmitted.json()["review_status"], "submitted")
        revision = resubmitted.json()["revision"]

        approved = self.client.post(
            f"/api/assets/{asset_id}/workflow",
            json={"action": "approve", "expected_revision": revision, "reason": "符合规范"},
            headers={"X-Actor-ID": "reviewer"},
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["review_status"], "approved")

        export = self.client.get(f"/api/projects/{project_id}/export/coco?approved_only=true")
        self.assertEqual(export.status_code, 200, export.text)
        with zipfile.ZipFile(io.BytesIO(export.content)) as archive:
            self.assertIn("manifest.json", archive.namelist())
            self.assertIn("annotations/instances.json", archive.namelist())
            coco = json.loads(archive.read("annotations/instances.json"))
            self.assertTrue(coco["images"][0]["file_name"].startswith("images/"))
            helmet_item = next(item for item in coco["annotations"] if item["category_id"] == label_id)
            self.assertTrue(helmet_item["attributes"]["occluded"])
            self.assertEqual(helmet_item["visionforge"]["source"], "manual")

        events = self.client.get(f"/api/assets/{asset_id}/review-events").json()
        self.assertEqual([event["to_status"] for event in events[:4]], ["approved", "submitted", "rejected", "submitted"])
        self.assertEqual(events[0]["reason"], "符合规范")
        self.assertEqual(events[2]["reason"], "")
        audit = self.client.get(f"/api/projects/{project_id}/audit-logs").json()
        self.assertTrue(any(row["action"] == "approve" for row in audit))

        stale_delete = self.client.delete(
            f"/api/annotations/{annotation['id']}?expected_version=1",
            headers={"X-Actor-ID": "alice"},
        )
        self.assertEqual(stale_delete.status_code, 409)

    def test_persistent_auto_job_and_soft_delete_count(self) -> None:
        project_id, asset_id = self.create_project_with_image("持久任务", "#24331c")
        health = self.client.get("/api/health").json()
        self.assertEqual(health["annotation_workers"]["status"], "online")
        self.assertGreaterEqual(health["annotation_workers"]["count"], 1)
        available = {model["id"]: model for model in self.client.get("/api/models").json()}
        self.assertIn("demo", available, available)
        self.assertTrue(available["demo"]["enabled"], available["demo"])
        response = self.client.post(
            f"/api/projects/{project_id}/jobs",
            json={
                "model_id": "demo",
                "prompt": "标注所有 object",
                "mode": "auto",
                "asset_ids": [asset_id],
                "confidence_threshold": 0.35,
            },
            headers={"X-Actor-ID": "alice"},
        )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["total"], 1)
        job = self.wait_for_job(response.json()["id"])
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(job["processed"], job["total"])
        self.assertEqual(job["created_count"], 1)
        self.assertEqual(job["progress_message"], "完成：新增 1 个标注")
        annotation = self.client.get(f"/api/assets/{asset_id}/annotations").json()[0]
        self.assertEqual(annotation["source"], "auto")

        deleted = self.client.delete(
            f"/api/annotations/{annotation['id']}?expected_version={annotation['version']}",
            headers={"X-Actor-ID": "alice"},
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        asset = self.client.get(f"/api/assets/{asset_id}").json()
        self.assertEqual(asset["annotation_count"], 0)
        listed = self.client.get(f"/api/projects/{project_id}/assets").json()[0]
        self.assertEqual(listed["annotation_count"], 0)
        self.assertEqual(listed["status"], "unlabeled")

        filtered_response = self.client.post(
            f"/api/projects/{project_id}/jobs",
            json={
                "model_id": "demo",
                "prompt": "高置信度过滤",
                "mode": "auto",
                "asset_ids": [asset_id],
                "confidence_threshold": 0.99,
            },
        )
        self.assertEqual(filtered_response.status_code, 202, filtered_response.text)
        filtered_job = self.wait_for_job(filtered_response.json()["id"])
        self.assertEqual(filtered_job["created_count"], 0)
        self.assertIn("模型返回 1 个候选", filtered_job["progress_message"])

    def test_grounding_dino_custom_prompt_controls_target_label(self) -> None:
        self.assertEqual(grounding_labels_from_prompt("cup, bottle", ["object"]), ["cup", "bottle"])
        self.assertEqual(
            grounding_labels_from_prompt(
                "标注图像中所有属于以下类别的目标：cup。尽量完整。", ["object"]
            ),
            ["cup"],
        )
        self.assertEqual(grounding_labels_from_prompt("标注所有 object", ["object"]), ["object"])

        project_id, asset_id = self.create_project_with_image("自定义 Grounding 类别")
        class RecordingProvider(DemoProvider):
            received: tuple[str, list[str]] | None = None

            async def predict(self, image_path, width, height, prompt, labels, hints=None):
                RecordingProvider.received = (prompt, labels)
                return await super().predict(image_path, width, height, prompt, labels, hints)

        provider = RecordingProvider()
        with (
            patch("app.main.get_provider", return_value=provider),
            patch("app.jobs.get_provider", return_value=provider),
        ):
            response = self.client.post(
                f"/api/projects/{project_id}/jobs",
                json={
                    "model_id": "grounding-dino",
                    "prompt": "cup",
                    "mode": "auto",
                    "asset_ids": [asset_id],
                },
            )
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(response.json()["settings"]["target_labels"], ["cup"])
            self.assertEqual(response.json()["settings"]["prompt_kind"], "grounding_labels")
            job = self.wait_for_job(response.json()["id"])
        self.assertEqual(job["created_count"], 1, job)
        self.assertEqual(RecordingProvider.received, ("cup", ["cup"]))
        annotations = self.client.get(f"/api/assets/{asset_id}/annotations").json()
        self.assertEqual(annotations[0]["label_name"], "cup")
        project = self.client.get(f"/api/projects/{project_id}").json()
        self.assertEqual([label["name"] for label in project["labels"]], ["object", "cup"])

    def test_sam3_concept_prompt_and_mask_polygon(self) -> None:
        import torch

        mask = torch.zeros((1, 20, 30), dtype=torch.bool)
        mask[:, 4:16, 7:24] = True
        prediction = _sam3_mask_prediction(
            mask,
            torch.tensor([7.0, 4.0, 24.0, 16.0]),
            torch.tensor(0.91),
            "person",
            30,
            20,
        )
        self.assertIsNotNone(prediction)
        self.assertEqual(prediction.shape_type, "polygon")
        self.assertGreaterEqual(len(prediction.points), 6)

        project_id, asset_id = self.create_project_with_image("SAM3 概念分割")

        class RecordingProvider(DemoProvider):
            received: tuple[str, list[str]] | None = None

            async def predict(self, image_path, width, height, prompt, labels, hints=None):
                RecordingProvider.received = (prompt, labels)
                return await super().predict(image_path, width, height, prompt, labels, hints)

        provider = RecordingProvider()
        with (
            patch("app.main.get_provider", return_value=provider),
            patch("app.jobs.get_provider", return_value=provider),
        ):
            response = self.client.post(
                f"/api/projects/{project_id}/jobs",
                json={
                    "model_id": "sam3",
                    "prompt": "person, helmet",
                    "target_labels": ["person", "helmet"],
                    "mode": "auto",
                    "asset_ids": [asset_id],
                },
            )
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(response.json()["settings"]["target_labels"], ["person", "helmet"])
            self.assertEqual(response.json()["settings"]["prompt_kind"], "grounding_labels")
            job = self.wait_for_job(response.json()["id"])
        self.assertEqual(job["created_count"], 1, job)
        self.assertEqual(RecordingProvider.received, ("person, helmet", ["person", "helmet"]))
        project = self.client.get(f"/api/projects/{project_id}").json()
        self.assertEqual(
            [label["name"] for label in project["labels"]],
            ["object", "person", "helmet"],
        )

    def test_upload_dedup_zip_and_runtime_model_registry(self) -> None:
        project_id = self.client.post("/api/projects", json={"name": "模型注册"}).json()["id"]
        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w") as archive:
            archive.writestr("frames/a.png", png_bytes(64, 64, "#111111"))
            archive.writestr("frames/a-copy.png", png_bytes(64, 64, "#111111"))
            archive.writestr("frames/readme.txt", "ignored")
        response = self.client.post(
            f"/api/projects/{project_id}/assets",
            files=[("files", ("dataset.zip", archive_bytes.getvalue(), "application/zip"))],
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["count"], 1)
        self.assertEqual(response.json()["duplicate_count"], 1)

        models = self.client.get("/api/models").json()
        expected = {"qwen3.5-35b-a3b", "grounding-dino", "sam3", "sam2-assist", "demo"}
        self.assertTrue(expected.issubset({model["id"] for model in models}))

        custom = {
            "id": "factory-detector",
            "name": "Factory Detector",
            "family": "开放词汇检测",
            "provider_type": "remote_detection",
            "description": "测试运行时注册",
            "endpoint": "http://inference.internal/predict",
            "model_name": "detector-v1",
            "api_key_env": "FACTORY_MODEL_TOKEN",
            "capabilities": ["检测框"],
            "modes": ["auto"],
            "timeout_seconds": 30,
            "enabled": True,
            "config": {"badge": "工厂模型"},
        }
        created = self.client.post("/api/models", json=custom)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertTrue(created.json()["configured"])
        custom["enabled"] = False
        updated = self.client.put("/api/models/factory-detector", json=custom)
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertFalse(updated.json()["configured"])
        deleted = self.client.delete("/api/models/factory-detector")
        self.assertEqual(deleted.status_code, 200, deleted.text)

    def test_normalized_model_coordinates(self) -> None:
        predictions = normalize_predictions(
            {"objects": [{"label": "person", "bbox": [100, 200, 900, 800]}]},
            640,
            480,
            coordinate_space="normalized_1000",
        )
        self.assertEqual(predictions[0].points, [64.0, 96.0, 576.0, 384.0])

    def test_project_initial_labels_and_label_removal(self) -> None:
        response = self.client.post(
            "/api/projects",
            json={
                "name": "类别初始化",
                "labels": ["person", "helmet", "Person", "  helmet  ", ""],
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        project_id = response.json()["id"]
        project = self.client.get(f"/api/projects/{project_id}").json()
        self.assertEqual([label["name"] for label in project["labels"]], ["person", "helmet"])
        deleted = self.client.delete(f"/api/labels/{project['labels'][0]['id']}")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        last = self.client.delete(f"/api/labels/{project['labels'][1]['id']}")
        self.assertEqual(last.status_code, 409, last.text)

    def test_failed_batch_upload_cleans_staged_files(self) -> None:
        project_id = self.client.post("/api/projects", json={"name": "失败上传清理"}).json()["id"]
        before = {path.name for path in settings.media_dir.iterdir()}
        response = self.client.post(
            f"/api/projects/{project_id}/assets",
            files=[
                ("files", ("valid.png", png_bytes(40, 40, "#123456"), "image/png")),
                ("files", ("damaged.png", b"not-an-image", "image/png")),
            ],
        )
        self.assertEqual(response.status_code, 415, response.text)
        after = {path.name for path in settings.media_dir.iterdir()}
        self.assertEqual(after, before)
        self.assertEqual(self.client.get(f"/api/projects/{project_id}/assets").json(), [])

    def test_interactive_remote_model_with_positive_negative_points(self) -> None:
        project_id, asset_id = self.create_project_with_image("交互式分割", "#222244")
        model = {
            "id": "test-interactive-segmentation",
            "name": "Test Interactive Segmentation",
            "family": "交互式分割",
            "provider_type": "interactive_segmentation",
            "description": "测试真实 HTTP 推理协议",
            "endpoint": self.inference_endpoint,
            "model_name": "segmenter-v1",
            "api_key_env": "",
            "capabilities": ["多边形", "正负点提示"],
            "modes": ["assist"],
            "timeout_seconds": 30,
            "enabled": True,
            "config": {"badge": "测试"},
        }
        created_model = self.client.post("/api/models", json=model)
        self.assertEqual(created_model.status_code, 201, created_model.text)
        health = self.client.post(f"/api/models/{model['id']}/health")
        self.assertEqual(health.status_code, 200, health.text)
        self.assertEqual(health.json()["status"], "healthy")

        invalid = self.client.post(
            f"/api/projects/{project_id}/jobs",
            json={
                "model_id": model["id"], "prompt": "分割 object", "mode": "assist",
                "asset_ids": [asset_id], "hints": {"points": [{"x": 999, "y": 30, "label": 1}]},
            },
        )
        self.assertEqual(invalid.status_code, 422, invalid.text)

        submitted = self.client.post(
            f"/api/projects/{project_id}/jobs",
            json={
                "model_id": model["id"], "prompt": "分割 object", "mode": "assist",
                "asset_ids": [asset_id],
                "hints": {"points": [{"x": 50, "y": 50, "label": 1}, {"x": 90, "y": 50, "label": 0}]},
            },
        )
        self.assertEqual(submitted.status_code, 202, submitted.text)
        job = self.wait_for_job(submitted.json()["id"])
        self.assertEqual(job["status"], "completed", job)
        annotations = self.client.get(f"/api/assets/{asset_id}/annotations").json()
        self.assertEqual(len(annotations), 1)
        self.assertEqual(annotations[0]["source"], "assist")
        self.assertEqual(annotations[0]["shape_type"], "polygon")
        self.assertEqual(InferenceHandler.last_payload["hints"]["points"][1]["label"], 0)

    def test_token_authentication_protects_reads_and_images(self) -> None:
        project_id, asset_id = self.create_project_with_image("认证读取", "#442222")
        old_mode, old_key = settings.auth_mode, settings.bootstrap_api_key
        object.__setattr__(settings, "auth_mode", "token")
        object.__setattr__(settings, "bootstrap_api_key", "industrial-test-token-32-characters")
        try:
            anonymous = self.client.get("/api/projects")
            self.assertEqual(anonymous.status_code, 401)
            anonymous_image = self.client.get(f"/api/assets/{asset_id}/image")
            self.assertEqual(anonymous_image.status_code, 401)
            invalid = self.client.post(
                "/api/session", headers={"Authorization": "Bearer incorrect"}
            )
            self.assertEqual(invalid.status_code, 401)
            logged_in = self.client.post(
                "/api/session",
                headers={"Authorization": "Bearer industrial-test-token-32-characters"},
            )
            self.assertEqual(logged_in.status_code, 200, logged_in.text)
            self.assertIn("mdp_session", self.client.cookies)
            self.assertNotIn("industrial-test-token", self.client.cookies["mdp_session"])
            browser_session = self.client.get("/api/session")
            self.assertEqual(browser_session.status_code, 200, browser_session.text)
            self.assertTrue(browser_session.json()["actor"].startswith("bootstrap-admin:"))
            self.assertEqual(self.client.get("/api/projects").status_code, 200)
            self.assertEqual(self.client.get(f"/api/assets/{asset_id}/image").status_code, 200)
            self.client.delete("/api/session")
        finally:
            object.__setattr__(settings, "auth_mode", old_mode)
            object.__setattr__(settings, "bootstrap_api_key", old_key)

    def test_immutable_dataset_versions_split_exports_and_analytics(self) -> None:
        project = self.client.post(
            "/api/projects", json={"name": "不可变版本", "labels": ["part"]}
        )
        self.assertEqual(project.status_code, 201, project.text)
        project_id = project.json()["id"]
        uploads = [
            (
                "files",
                (f"frame-{index}.png", png_bytes(color=f"#{index + 1:06x}"), "image/png"),
            )
            for index in range(6)
        ]
        uploaded = self.client.post(f"/api/projects/{project_id}/assets", files=uploads)
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        asset_ids = uploaded.json()["asset_ids"]
        label_id = self.client.get(f"/api/projects/{project_id}").json()["labels"][0]["id"]
        annotation_ids: list[int] = []
        approved_revisions: dict[int, int] = {}
        for index, asset_id in enumerate(asset_ids):
            annotation = self.client.post(
                f"/api/assets/{asset_id}/annotations",
                json={
                    "label_id": label_id,
                    "shape_type": "rectangle",
                    "points": [10 + index, 10, 100 + index, 100],
                    "expected_asset_revision": 0,
                },
            )
            self.assertEqual(annotation.status_code, 201, annotation.text)
            annotation_ids.append(annotation.json()["id"])
            submitted = self.client.post(
                f"/api/assets/{asset_id}/workflow",
                json={
                    "action": "submit",
                    "expected_revision": annotation.json()["asset_revision"],
                },
            )
            self.assertEqual(submitted.status_code, 200, submitted.text)
            approved = self.client.post(
                f"/api/assets/{asset_id}/workflow",
                json={"action": "approve", "expected_revision": submitted.json()["revision"]},
            )
            self.assertEqual(approved.status_code, 200, approved.text)
            approved_revisions[asset_id] = approved.json()["revision"]

        version_payload = {
            "name": "Release A",
            "approved_only": True,
            "split_seed": 2026,
            "train_ratio": 0.5,
            "valid_ratio": 0.333333,
            "test_ratio": 0.166667,
        }
        created = self.client.post(
            f"/api/projects/{project_id}/versions", json=version_payload
        )
        self.assertEqual(created.status_code, 201, created.text)
        version = created.json()
        self.assertEqual(version["asset_count"], 6)
        self.assertEqual(version["annotation_count"], 6)
        self.assertEqual(
            {key: value["asset_count"] for key, value in version["splits"].items()},
            {"train": 3, "valid": 2, "test": 1},
        )
        self.assertEqual(version["labels"][0]["object_count"], 6)
        self.assertEqual(len(version["content_hash"]), 64)

        same_snapshot = self.client.post(
            f"/api/projects/{project_id}/versions",
            json={**version_payload, "name": "Release A duplicate"},
        )
        self.assertEqual(same_snapshot.status_code, 201, same_snapshot.text)
        self.assertEqual(same_snapshot.json()["content_hash"], version["content_hash"])
        self.assertEqual(same_snapshot.json()["splits"], version["splits"])

        reopened = self.client.post(
            f"/api/assets/{asset_ids[0]}/workflow",
            json={"action": "reopen", "expected_revision": approved_revisions[asset_ids[0]]},
        )
        self.assertEqual(reopened.status_code, 200, reopened.text)
        changed = self.client.patch(
            f"/api/annotations/{annotation_ids[0]}",
            json={"expected_version": 1, "points": [20, 20, 80, 80]},
        )
        self.assertEqual(changed.status_code, 200, changed.text)

        coco_response = self.client.get(f"/api/versions/{version['id']}/export/coco")
        self.assertEqual(coco_response.status_code, 200, coco_response.text)
        with zipfile.ZipFile(io.BytesIO(coco_response.content)) as archive:
            names = archive.namelist()
            for split in ("train", "valid", "test"):
                self.assertIn(f"annotations/instances_{split}.json", names)
                self.assertTrue(any(name.startswith(f"images/{split}/") for name in names))
            snapshots = [
                json.loads(archive.read(f"annotations/instances_{split}.json"))
                for split in ("train", "valid", "test")
            ]
            frozen_item = next(
                item
                for snapshot in snapshots
                for item in snapshot["annotations"]
                if item["visionforge"]["original_annotation_id"] == annotation_ids[0]
            )
            self.assertEqual(frozen_item["bbox"], [10, 10, 90, 90])

        yolo_response = self.client.get(f"/api/versions/{version['id']}/export/yolo")
        self.assertEqual(yolo_response.status_code, 200, yolo_response.text)
        with zipfile.ZipFile(io.BytesIO(yolo_response.content)) as archive:
            names = archive.namelist()
            self.assertIn("data.yaml", names)
            yaml = archive.read("data.yaml").decode()
            self.assertIn("train: images/train", yaml)
            self.assertIn("val: images/val", yaml)
            self.assertIn("test: images/test", yaml)
            image_sets = {
                split: {
                    Path(name).name
                    for name in names
                    if name.startswith(f"images/{split}/")
                }
                for split in ("train", "val", "test")
            }
            self.assertFalse(image_sets["train"] & image_sets["val"])
            self.assertFalse(image_sets["train"] & image_sets["test"])
            self.assertFalse(image_sets["val"] & image_sets["test"])

        analytics = self.client.get(f"/api/projects/{project_id}/analytics")
        self.assertEqual(analytics.status_code, 200, analytics.text)
        result = analytics.json()
        self.assertEqual(result["summary"]["asset_count"], 6)
        self.assertEqual(result["summary"]["annotation_count"], 6)
        self.assertEqual(result["labels"][0]["object_count"], 6)
        self.assertEqual(result["versions"]["count"], 2)
        self.assertEqual(result["quality"]["missing_file_count"], 0)

        training = self.client.get(f"/api/projects/{project_id}/training-runs")
        self.assertEqual(training.status_code, 200, training.text)
        self.assertIn("available", training.json()["availability"])
        self.assertIn("conversion_formats", training.json()["availability"])

    def test_training_backend_never_queues_when_unavailable(self) -> None:
        project_id = self.client.post(
            "/api/projects", json={"name": "训练可用性"}
        ).json()["id"]
        unavailable = {
            "available": False,
            "reason": "missing local dependency",
            "backend": None,
            "backend_version": None,
            "cuda_available": False,
            "base_models": {},
            "conversion_formats": {
                name: {
                    "available": False,
                    "reason": "missing local dependency",
                    "requires_gpu": name == "engine",
                }
                for name in ("torchscript", "onnx", "engine")
            },
        }
        with patch("app.training.training_availability", return_value=unavailable):
            listed = self.client.get(f"/api/projects/{project_id}/training-runs")
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertFalse(listed.json()["availability"]["available"])
            created = self.client.post(
                f"/api/projects/{project_id}/training-runs", json={"version_id": 99999}
            )
            self.assertEqual(created.status_code, 503, created.text)
            self.assertEqual(created.json()["detail"]["availability"]["reason"], unavailable["reason"])
        count = one("SELECT COUNT(*) AS count FROM training_runs WHERE project_id = ?", (project_id,))
        self.assertEqual(count["count"], 0)

    def test_training_catalog_and_version_task_compatibility(self) -> None:
        catalog_dir = Path(TEST_DATA.name) / "catalog-fixtures"
        catalog_dir.mkdir(exist_ok=True)
        checkpoints = {
            "yolo-test.pt": "detect",
            "yolo-test-seg.pt": "segment",
            "yolo-test-cls.pt": "classify",
        }
        paths = []
        for filename in checkpoints:
            path = catalog_dir / filename
            path.write_bytes(filename.encode())
            paths.append(path)

        def checkpoint_probe(path: Path) -> dict:
            task = checkpoints[path.name]
            return {
                "loadable": True,
                "task": task,
                "parameters": 5_000_000,
                "trainable_parameters": 5_000_000,
                "error": "",
            }

        with (
            patch.object(training_module, "_model_candidates", return_value=paths),
            patch.object(
                training_module,
                "_backend_info",
                return_value={"name": "ultralytics", "version": "test", "installed": True},
            ),
            patch.object(training_module, "_checkpoint_probe", side_effect=checkpoint_probe),
            patch.object(training_module, "_file_sha256", side_effect=lambda path: path.name * 2),
        ):
            models = training_module.model_catalog()

        by_id = {model["id"]: model for model in models}
        self.assertTrue(by_id["yolo-test.pt"]["available"])
        self.assertTrue(by_id["yolo-test-seg.pt"]["available"])
        self.assertTrue(by_id["yolo-test-cls.pt"]["available"])
        self.assertEqual(by_id["yolo-test-cls.pt"]["compatibility"]["annotation_shapes"], ["classification"])

        project_id, version_id, _ = self.create_training_version_fixture(
            "训练任务兼容性", rectangles=2, polygons=1
        )
        del project_id
        with patch.object(training_module, "model_catalog", return_value=models):
            compatibility = self.client.get(
                f"/api/versions/{version_id}/training-compatibility"
            )
        self.assertEqual(compatibility.status_code, 200, compatibility.text)
        result = compatibility.json()
        self.assertEqual(result["inferred_task"], "segment")
        self.assertEqual(result["tasks"]["detect"]["quality"], "degraded")
        self.assertIn("polygon_to_bbox", result["tasks"]["detect"]["transformations"])
        self.assertEqual(result["tasks"]["segment"]["quality"], "degraded")
        self.assertIn(
            "rectangle_to_polygon", result["tasks"]["segment"]["transformations"]
        )
        self.assertEqual(result["tasks"]["detect"]["model_ids"], ["yolo-test.pt"])
        self.assertEqual(
            result["tasks"]["segment"]["model_ids"], ["yolo-test-seg.pt"]
        )

    def test_training_retry_lineage_logs_metrics_and_explicit_task(self) -> None:
        project_id, version_id, _ = self.create_training_version_fixture(
            "训练谱系", rectangles=2, polygons=1
        )
        model_path = Path(TEST_DATA.name) / "yolo-lineage.pt"
        model_path.write_bytes(b"immutable-test-checkpoint")
        model_metadata = {
            "id": model_path.name,
            "task": "detect",
            "sha256": "a" * 64,
            "size_bytes": model_path.stat().st_size,
        }
        resources = {
            "cuda_available": False,
            "devices": [{"id": "cpu", "type": "cpu", "available": True}],
        }
        availability = {
            "available": True,
            "backend": "ultralytics",
            "backend_version": "test",
            "tasks": {},
            "conversion_formats": {},
            "cuda_available": False,
        }
        create_payload = {
            "version_id": version_id,
            "model_arch": model_path.name,
            "task": "detect",
            "epochs": 3,
            "device": "cpu",
            "optimizer": "AdamW",
            "seed": 7,
            "deterministic": True,
            "max_retries": 1,
        }
        with (
            patch.object(training_module, "training_availability", return_value=availability),
            patch.object(training_module, "training_resources", return_value=resources),
            patch.object(
                training_module,
                "_resolve_base_model",
                return_value=(model_path, model_metadata),
            ),
        ):
            created = self.client.post(
                f"/api/projects/{project_id}/training-runs", json=create_payload
            )
        self.assertEqual(created.status_code, 202, created.text)
        run = created.json()
        run_id = run["id"]
        self.assertEqual(run["task"], "detect")
        self.assertEqual(run["settings"]["inferred_version_task"], "segment")
        self.assertEqual(run["settings"]["optimizer"], "AdamW")
        self.assertEqual(run["settings"]["seed"], 7)
        self.assertEqual(run["settings"]["base_model_sha256"], "a" * 64)
        self.assertEqual(run["lineage"]["root_run_id"], run_id)
        self.assertEqual(run["lineage"]["attempt"], 0)

        training_module._append_training_log(run_id, "info", "prepared", "dataset ready")
        training_module._append_training_log(run_id, "info", "started", "trainer started")
        with db() as conn:
            conn.execute(
                """UPDATE training_runs SET status = 'failed', error = 'synthetic failure',
                   metrics_json = ? WHERE id = ?""",
                (json.dumps({"fitness": 0.25}), run_id),
            )
            conn.executemany(
                """INSERT INTO training_metrics
                   (run_id, epoch, progress, metrics_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (run_id, 1, 1 / 3, json.dumps({"map50": 0.1}), now_iso()),
                    (run_id, 2, 2 / 3, json.dumps({"map50": 0.2}), now_iso()),
                ],
            )

        first_page = self.client.get(f"/api/training-runs/{run_id}/logs?limit=2")
        self.assertEqual(first_page.status_code, 200, first_page.text)
        first_logs = first_page.json()
        self.assertEqual(len(first_logs["logs"]), 2)
        self.assertTrue(first_logs["has_more"])
        second_page = self.client.get(
            f"/api/training-runs/{run_id}/logs?after_id={first_logs['next_after_id']}&limit=2"
        )
        self.assertEqual(second_page.status_code, 200, second_page.text)
        self.assertEqual(len(second_page.json()["logs"]), 1)

        metrics = self.client.get(f"/api/training-runs/{run_id}/metrics")
        self.assertEqual(metrics.status_code, 200, metrics.text)
        self.assertEqual([item["epoch"] for item in metrics.json()["history"]], [1, 2])
        self.assertEqual(metrics.json()["latest"]["fitness"], 0.25)

        with patch.object(
            training_module,
            "_resolve_base_model",
            return_value=(model_path, model_metadata),
        ):
            retried = self.client.post(
                f"/api/training-runs/{run_id}/retry", json={"name": "retry lineage"}
            )
        self.assertEqual(retried.status_code, 202, retried.text)
        retry_run = retried.json()
        self.assertEqual(retry_run["lineage"]["parent_run_id"], run_id)
        self.assertEqual(retry_run["lineage"]["root_run_id"], run_id)
        self.assertEqual(retry_run["lineage"]["attempt"], 1)
        self.assertEqual(retry_run["config_hash"], run["config_hash"])
        self.assertEqual(retry_run["settings"], run["settings"])
        with db() as conn:
            conn.execute(
                "UPDATE training_runs SET status = 'failed' WHERE id = ?",
                (retry_run["id"],),
            )
        exhausted = self.client.post(f"/api/training-runs/{retry_run['id']}/retry")
        self.assertEqual(exhausted.status_code, 409, exhausted.text)

    def test_pipeline_retries_are_atomic(self) -> None:
        project_id, version_id, fixture = self.create_training_version_fixture(
            "原子重试与公平队列", rectangles=2
        )
        model_path = Path(TEST_DATA.name) / f"atomic-retry-{project_id}.pt"
        model_path.write_bytes(b"atomic-retry-checkpoint")
        model_metadata = {
            "id": model_path.name,
            "task": "detect",
            "sha256": "c" * 64,
            "size_bytes": model_path.stat().st_size,
        }
        training_settings = json.dumps(
            {
                "version_content_hash": fixture["content_hash"],
                "base_model_sha256": model_metadata["sha256"],
                "device": "cpu",
                "resource_profile": "balanced",
            }
        )
        artifact_dir = settings.training_dir / f"atomic-retry-source-{project_id}"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        source_artifact = artifact_dir / "best.pt"
        source_artifact.write_bytes(b"completed-training-artifact")
        with db() as conn:
            training_parent_cursor = conn.execute(
                """INSERT INTO training_runs
                   (project_id, version_id, name, model_arch, task, status, total_epochs,
                    settings_json, metrics_json, max_retries, config_hash, resource_json,
                    created_by, created_at, finished_at)
                   VALUES (?, ?, 'atomic training parent', ?, 'detect', 'failed', 1,
                           ?, '{}', 2, ?, '{}', 'test', ?, ?)""",
                (
                    project_id,
                    version_id,
                    model_path.name,
                    training_settings,
                    "d" * 64,
                    now_iso(),
                    now_iso(),
                ),
            )
            training_parent_id = int(training_parent_cursor.lastrowid)
            conn.execute(
                "UPDATE training_runs SET root_run_id = ? WHERE id = ?",
                (training_parent_id, training_parent_id),
            )
            completed_run_cursor = conn.execute(
                """INSERT INTO training_runs
                   (project_id, version_id, name, model_arch, task, status, progress,
                    current_epoch, total_epochs, settings_json, metrics_json, artifact_path,
                    created_by, created_at, finished_at)
                   VALUES (?, ?, 'conversion retry source', ?, 'detect', 'completed', 1,
                           1, 1, '{}', '{}', ?, 'test', ?, ?)""",
                (
                    project_id,
                    version_id,
                    model_path.name,
                    str(source_artifact),
                    now_iso(),
                    now_iso(),
                ),
            )
            completed_run_id = int(completed_run_cursor.lastrowid)
            conversion_parent_cursor = conn.execute(
                """INSERT INTO model_conversions
                   (training_run_id, format, status, settings_json, max_retries,
                    resource_json, created_by, created_at, finished_at)
                   VALUES (?, 'onnx', 'failed', ?, 2, '{}', 'test', ?, ?)""",
                (
                    completed_run_id,
                    json.dumps(
                        {
                            "format": "onnx",
                            "image_size": 640,
                            "half": False,
                            "dynamic": False,
                            "simplify": True,
                            "device": "cpu",
                        }
                    ),
                    now_iso(),
                    now_iso(),
                ),
            )
            conversion_parent_id = int(conversion_parent_cursor.lastrowid)
            conn.execute(
                "UPDATE model_conversions SET root_conversion_id = ? WHERE id = ?",
                (conversion_parent_id, conversion_parent_id),
            )

        def concurrent_posts(url: str, payload: dict | None = None) -> list:
            barrier = threading.Barrier(3)
            responses = []
            failures = []
            result_lock = threading.Lock()

            def post_retry(actor: str) -> None:
                try:
                    barrier.wait(timeout=5)
                    response = self.client.post(
                        url,
                        json=payload,
                        headers={"X-Actor-ID": actor},
                    )
                    with result_lock:
                        responses.append(response)
                except BaseException as exc:  # Preserve worker-thread failures for the test.
                    with result_lock:
                        failures.append(exc)

            threads = [
                threading.Thread(target=post_retry, args=(f"retry-{index}",))
                for index in range(2)
            ]
            for thread in threads:
                thread.start()
            barrier.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(failures, [])
            self.assertEqual(len(responses), 2)
            return responses

        with patch.object(
            training_module,
            "_resolve_base_model",
            return_value=(model_path, model_metadata),
        ):
            training_responses = concurrent_posts(
                f"/api/training-runs/{training_parent_id}/retry",
                {"name": "atomic child"},
            )
        self.assertEqual(sorted(item.status_code for item in training_responses), [202, 409])
        training_created = next(item.json() for item in training_responses if item.status_code == 202)
        training_conflict = next(item.json() for item in training_responses if item.status_code == 409)
        self.assertEqual(
            training_conflict["detail"]["existing_child_id"], training_created["id"]
        )
        with db() as conn:
            training_children = [
                dict(row)
                for row in conn.execute(
                    """SELECT id, parent_run_id, root_run_id, attempt
                       FROM training_runs WHERE parent_run_id = ? ORDER BY id""",
                    (training_parent_id,),
                ).fetchall()
            ]
        self.assertEqual(len(training_children), 1)
        self.assertEqual(training_children[0]["id"], training_created["id"])
        self.assertEqual(training_children[0]["root_run_id"], training_parent_id)
        self.assertEqual(training_children[0]["attempt"], 1)

        conversion_responses = concurrent_posts(
            f"/api/conversions/{conversion_parent_id}/retry", {"device": "cpu"}
        )
        self.assertEqual(sorted(item.status_code for item in conversion_responses), [202, 409])
        conversion_created = next(
            item.json() for item in conversion_responses if item.status_code == 202
        )
        conversion_conflict = next(
            item.json() for item in conversion_responses if item.status_code == 409
        )
        self.assertEqual(
            conversion_conflict["detail"]["existing_child_id"], conversion_created["id"]
        )
        with db() as conn:
            conversion_children = [
                dict(row)
                for row in conn.execute(
                    """SELECT id, parent_conversion_id, root_conversion_id, attempt
                       FROM model_conversions WHERE parent_conversion_id = ? ORDER BY id""",
                    (conversion_parent_id,),
                ).fetchall()
            ]
        self.assertEqual(len(conversion_children), 1)
        self.assertEqual(conversion_children[0]["id"], conversion_created["id"])
        self.assertEqual(conversion_children[0]["root_conversion_id"], conversion_parent_id)
        self.assertEqual(conversion_children[0]["attempt"], 1)

    def test_pipeline_queue_is_globally_fair(self) -> None:
        project_id, version_id, _ = self.create_training_version_fixture(
            "公平流水线队列", rectangles=2
        )
        model_arch = f"fairness-{project_id}.pt"
        # Isolate the global queue, then prove that task kind cannot override age.
        with db() as conn:
            conn.execute(
                """UPDATE training_runs SET status = 'cancelled', finished_at = ?
                   WHERE status IN ('queued', 'running')""",
                (now_iso(),),
            )
            conn.execute(
                """UPDATE model_conversions SET status = 'cancelled', finished_at = ?
                   WHERE status IN ('queued', 'running')""",
                (now_iso(),),
            )
            completed_run_cursor = conn.execute(
                """INSERT INTO training_runs
                   (project_id, version_id, name, model_arch, task, status, progress,
                    current_epoch, total_epochs, settings_json, metrics_json,
                    created_by, created_at, finished_at)
                   VALUES (?, ?, 'fair conversion source', ?, 'detect', 'completed', 1,
                           1, 1, '{}', '{}', 'test', ?, ?)""",
                (project_id, version_id, model_arch, now_iso(), now_iso()),
            )
            completed_run_id = int(completed_run_cursor.lastrowid)
            oldest_conversion_cursor = conn.execute(
                """INSERT INTO model_conversions
                   (training_run_id, format, status, settings_json, created_by, created_at)
                   VALUES (?, 'onnx', 'queued', '{}', 'test', ?)""",
                (completed_run_id, "2020-01-01T00:00:01+00:00"),
            )
            oldest_conversion_id = int(oldest_conversion_cursor.lastrowid)
            first_training_cursor = conn.execute(
                """INSERT INTO training_runs
                   (project_id, version_id, name, model_arch, task, status, total_epochs,
                    settings_json, metrics_json, created_by, created_at)
                   VALUES (?, ?, 'fair training 1', ?, 'detect', 'queued', 1,
                           '{}', '{}', 'test', ?)""",
                (
                    project_id,
                    version_id,
                    model_arch,
                    "2020-01-01T00:00:02+00:00",
                ),
            )
            first_training_id = int(first_training_cursor.lastrowid)
            second_training_cursor = conn.execute(
                """INSERT INTO training_runs
                   (project_id, version_id, name, model_arch, task, status, total_epochs,
                    settings_json, metrics_json, created_by, created_at)
                   VALUES (?, ?, 'fair training 2', ?, 'detect', 'queued', 1,
                           '{}', '{}', 'test', ?)""",
                (
                    project_id,
                    version_id,
                    model_arch,
                    "2020-01-01T00:00:03+00:00",
                ),
            )
            second_training_id = int(second_training_cursor.lastrowid)
            newest_conversion_cursor = conn.execute(
                """INSERT INTO model_conversions
                   (training_run_id, format, status, settings_json, created_by, created_at)
                   VALUES (?, 'torchscript', 'queued', '{}', 'test', ?)""",
                (completed_run_id, "2020-01-01T00:00:04+00:00"),
            )
            newest_conversion_id = int(newest_conversion_cursor.lastrowid)

        expected_order = [
            ("model_conversions", oldest_conversion_id),
            ("training_runs", first_training_id),
            ("training_runs", second_training_id),
            ("model_conversions", newest_conversion_id),
        ]
        observed_order = []

        def finish_without_running(
            table: str,
            task_id: int,
            _worker_id: str,
            _generation: int,
            _runner,
        ) -> None:
            observed_order.append((table, task_id))
            with db() as conn:
                conn.execute(
                    f"UPDATE {table} SET status = 'completed', lease_expires_at = NULL WHERE id = ?",
                    (task_id,),
                )

        async def exercise_worker() -> None:
            worker = training_module.PipelineWorker("fairness-test")
            task = asyncio.create_task(worker.run_forever())
            deadline = time.monotonic() + 5
            while len(observed_order) < len(expected_order) and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            worker.stop()
            await asyncio.wait_for(task, timeout=5)

        with patch.object(
            training_module, "_run_with_lease", side_effect=finish_without_running
        ):
            asyncio.run(exercise_worker())
        self.assertEqual(observed_order, expected_order)

    def test_conversion_cancel_retry_lineage_and_logs(self) -> None:
        project_id, version_id, _ = self.create_training_version_fixture(
            "转换取消", rectangles=2
        )
        run_dir = settings.training_dir / f"fixture-run-{project_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        artifact = run_dir / "best.pt"
        artifact.write_bytes(b"trained-model")
        now = now_iso()
        with db() as conn:
            run_cursor = conn.execute(
                """INSERT INTO training_runs
                   (project_id, version_id, name, model_arch, task, status, progress,
                    current_epoch, total_epochs, settings_json, metrics_json, artifact_path,
                    created_by, created_at, finished_at)
                   VALUES (?, ?, 'completed fixture', 'fixture.pt', 'detect', 'completed', 1,
                           1, 1, '{}', '{}', ?, 'test', ?, ?)""",
                (project_id, version_id, str(artifact), now, now),
            )
            run_id = int(run_cursor.lastrowid)
            conversion_cursor = conn.execute(
                """INSERT INTO model_conversions
                   (training_run_id, format, status, settings_json, max_retries,
                    resource_json, progress_message, created_by, created_at)
                   VALUES (?, 'onnx', 'queued', ?, 1, '{}', 'queued', 'test', ?)""",
                (
                    run_id,
                    json.dumps(
                        {
                            "format": "onnx",
                            "image_size": 640,
                            "half": False,
                            "dynamic": False,
                            "simplify": True,
                            "device": "cpu",
                        }
                    ),
                    now,
                ),
            )
            conversion_id = int(conversion_cursor.lastrowid)
            conn.execute(
                "UPDATE model_conversions SET root_conversion_id = ? WHERE id = ?",
                (conversion_id, conversion_id),
            )

        cancelled = self.client.post(f"/api/conversions/{conversion_id}/cancel")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        cancelled_item = cancelled.json()
        self.assertEqual(cancelled_item["status"], "cancelled")
        self.assertTrue(cancelled_item["cancel_requested"])
        self.assertEqual(cancelled_item["progress_message"], "已取消")

        retried = self.client.post(
            f"/api/conversions/{conversion_id}/retry", json={"device": "cpu"}
        )
        self.assertEqual(retried.status_code, 202, retried.text)
        retry_item = retried.json()
        self.assertEqual(retry_item["lineage"]["parent_conversion_id"], conversion_id)
        self.assertEqual(retry_item["lineage"]["root_conversion_id"], conversion_id)
        self.assertEqual(retry_item["lineage"]["attempt"], 1)
        logs = self.client.get(f"/api/conversions/{conversion_id}/logs")
        self.assertEqual(logs.status_code, 200, logs.text)
        self.assertEqual(
            [item["event"] for item in logs.json()["logs"]],
            ["cancel_requested", "retry_created"],
        )

        running_now = now_iso()
        with db() as conn:
            running_cursor = conn.execute(
                """INSERT INTO model_conversions
                   (training_run_id, format, status, settings_json, claimed_by,
                    progress_message, created_by, created_at)
                   VALUES (?, 'torchscript', 'running', '{}', 'worker:test',
                           'running', 'test', ?)""",
                (run_id, running_now),
            )
            running_id = int(running_cursor.lastrowid)
        running_cancel = self.client.post(f"/api/conversions/{running_id}/cancel")
        self.assertEqual(running_cancel.status_code, 200, running_cancel.text)
        self.assertEqual(running_cancel.json()["status"], "running")
        self.assertTrue(running_cancel.json()["cancel_requested"])
        self.assertEqual(running_cancel.json()["progress_message"], "正在安全停止")
        cpu_snapshot = {
            "snapshot_at": now_iso(),
            "hostname": "test-host",
            "cuda_available": False,
            "cpu": {"id": "cpu", "type": "cpu", "name": "test cpu", "available": True},
            "devices": [
                {"id": "cpu", "type": "cpu", "name": "test cpu", "available": True}
            ],
        }
        with patch.object(training_module, "training_resources", return_value=cpu_snapshot):
            training_module._run_with_lease(
                "model_conversions", running_id, "worker:test", 0,
                training_module.run_conversion,
            )
        safely_cancelled = one(
            "SELECT status, progress_message FROM model_conversions WHERE id = ?",
            (running_id,),
        )
        self.assertEqual(safely_cancelled["status"], "cancelled")
        self.assertEqual(safely_cancelled["progress_message"], "已取消")

        other_project_id, other_version_id, _ = self.create_training_version_fixture(
            "转换项目隔离", rectangles=2
        )
        with db() as conn:
            other_run_cursor = conn.execute(
                """INSERT INTO training_runs
                   (project_id, version_id, name, model_arch, task, status, total_epochs,
                    settings_json, metrics_json, created_by, created_at)
                   VALUES (?, ?, 'other run', 'fixture.pt', 'detect', 'completed', 1,
                           '{}', '{}', 'test', ?)""",
                (other_project_id, other_version_id, now_iso()),
            )
            other_run_id = int(other_run_cursor.lastrowid)
            other_conversion_cursor = conn.execute(
                """INSERT INTO model_conversions
                   (training_run_id, format, status, settings_json, created_by, created_at)
                   VALUES (?, 'onnx', 'queued', '{}', 'test', ?)""",
                (other_run_id, now_iso()),
            )
            other_conversion_id = int(other_conversion_cursor.lastrowid)
        conversion_availability = {"conversion_formats": {}}
        with patch.object(
            training_module, "training_availability", return_value=conversion_availability
        ):
            active = self.client.get(
                f"/api/projects/{project_id}/model-conversions?status=active"
            )
            first_page = self.client.get(
                f"/api/projects/{project_id}/model-conversions?limit=1"
            )
        self.assertEqual(active.status_code, 200, active.text)
        self.assertEqual(
            [item["id"] for item in active.json()["conversions"]], [retry_item["id"]]
        )
        self.assertEqual(active.json()["counts"]["queued"], 1)
        self.assertEqual(active.json()["counts"]["cancelled"], 2)
        self.assertNotIn(
            other_conversion_id,
            [item["id"] for item in active.json()["conversions"]],
        )
        self.assertEqual(first_page.status_code, 200, first_page.text)
        self.assertTrue(first_page.json()["has_more"])
        before_id = first_page.json()["next_before_id"]
        with patch.object(
            training_module, "training_availability", return_value=conversion_availability
        ):
            second_page = self.client.get(
                f"/api/projects/{project_id}/model-conversions?limit=1&before_id={before_id}"
            )
        self.assertEqual(second_page.status_code, 200, second_page.text)
        self.assertLess(second_page.json()["conversions"][0]["id"], before_id)
        self.assertNotEqual(second_page.json()["conversions"][0]["id"], other_conversion_id)

    def test_pipeline_fencing_failure_state_and_compute_resource_mutex(self) -> None:
        project_id, version_id, fixture = self.create_training_version_fixture(
            "流水线 fencing", rectangles=2
        )
        with db() as conn:
            # The pipeline queue is global. Terminalize leftovers from earlier API tests so
            # this test can assert the exact task claimed without relying on method order.
            conn.execute(
                """UPDATE training_runs SET status = 'cancelled', finished_at = ?
                   WHERE status IN ('queued', 'running')""",
                (now_iso(),),
            )
            conn.execute("DELETE FROM pipeline_resource_leases")
            settings_json = json.dumps(
                {
                    "device": "cpu",
                    "resource_profile": "balanced",
                    "base_model": "/missing/checkpoint.pt",
                    "base_model_sha256": "b" * 64,
                    "version_content_hash": fixture["content_hash"],
                    "epochs": 1,
                    "image_size": 64,
                    "batch_size": 1,
                    "workers": 0,
                    "patience": 1,
                }
            )
            queued_cursor = conn.execute(
                """INSERT INTO training_runs
                   (project_id, version_id, name, model_arch, task, status, total_epochs,
                    settings_json, metrics_json, resource_json, created_by, created_at)
                   VALUES (?, ?, 'fencing fixture', 'fixture.pt', 'detect', 'queued', 1,
                           ?, '{}', '{}', 'test', ?)""",
                (project_id, version_id, settings_json, now_iso()),
            )
            run_id = int(queued_cursor.lastrowid)

        first_claim = training_module._claim_next("training_runs", "worker:first")
        self.assertEqual(first_claim, (run_id, 1, False))
        with db() as conn:
            conn.execute(
                "UPDATE training_runs SET lease_expires_at = ? WHERE id = ?",
                ("1970-01-01T00:00:00+00:00", run_id),
            )
        second_claim = training_module._claim_next("training_runs", "worker:second")
        self.assertEqual(second_claim, (run_id, 2, True))

        # A stale generation may still return from framework code, but it must not mutate
        # the task now owned by generation 2.
        training_module._run_with_lease(
            "training_runs", run_id, "worker:first", 1, training_module.run_training
        )
        after_stale_runner = one(
            "SELECT status, claimed_by, claim_generation FROM training_runs WHERE id = ?",
            (run_id,),
        )
        self.assertEqual(after_stale_runner["status"], "running")
        self.assertEqual(after_stale_runner["claimed_by"], "worker:second")
        self.assertEqual(after_stale_runner["claim_generation"], 2)

        # Regression for the former missing comma in the failure UPDATE: a real runner
        # exception must be persisted as failed instead of escaping and leaving running.
        with db() as conn:
            failed_cursor = conn.execute(
                """INSERT INTO training_runs
                   (project_id, version_id, name, model_arch, task, status, total_epochs,
                    settings_json, metrics_json, resource_json, claimed_by, claim_generation,
                    created_by, created_at)
                   VALUES (?, ?, 'failure fixture', 'fixture.pt', 'detect', 'running', 1,
                           ?, '{}', '{}', 'worker:failure', 1, 'test', ?)""",
                (project_id, version_id, settings_json, now_iso()),
            )
            failed_id = int(failed_cursor.lastrowid)
        with (
            patch.object(training_module, "_resolve_base_model", return_value=None),
            patch.object(training_module.logger, "exception"),
        ):
            training_module._run_with_lease(
                "training_runs", failed_id, "worker:failure", 1,
                training_module.run_training,
            )
        failed = one(
            "SELECT status, error, progress_message FROM training_runs WHERE id = ?",
            (failed_id,),
        )
        self.assertEqual(failed["status"], "failed")
        self.assertIn("基础模型", failed["error"])
        self.assertEqual(failed["progress_message"], "训练失败")
        failure_logs = self.client.get(f"/api/training-runs/{failed_id}/logs").json()["logs"]
        self.assertEqual(failure_logs[-1]["event"], "failed")

        with db() as conn:
            missing_run_cursor = conn.execute(
                """INSERT INTO training_runs
                   (project_id, version_id, name, model_arch, task, status, progress,
                    current_epoch, total_epochs, settings_json, metrics_json, artifact_path,
                    created_by, created_at, finished_at)
                   VALUES (?, ?, 'missing source', 'fixture.pt', 'detect', 'completed', 1,
                           1, 1, '{}', '{}', ?, 'test', ?, ?)""",
                (
                    project_id,
                    version_id,
                    str(settings.training_dir / "missing-source.pt"),
                    now_iso(),
                    now_iso(),
                ),
            )
            missing_run_id = int(missing_run_cursor.lastrowid)
            conversion_cursor = conn.execute(
                """INSERT INTO model_conversions
                   (training_run_id, format, status, settings_json, claimed_by,
                    claim_generation, created_by, created_at)
                   VALUES (?, 'onnx', 'running', ?, 'worker:conversion-failure', 1,
                           'test', ?)""",
                (
                    missing_run_id,
                    json.dumps(
                        {
                            "image_size": 640,
                            "half": False,
                            "dynamic": False,
                            "simplify": True,
                            "device": "cpu",
                        }
                    ),
                    now_iso(),
                ),
            )
            failed_conversion_id = int(conversion_cursor.lastrowid)
        with (
            patch.object(
                training_module,
                "_acquire_resource_lease",
                return_value={"resolved_device": "cpu"},
            ),
            patch.object(training_module.logger, "exception"),
        ):
            training_module.run_conversion(
                failed_conversion_id, "worker:conversion-failure", 1
            )
        failed_conversion = one(
            "SELECT status, error, progress_message FROM model_conversions WHERE id = ?",
            (failed_conversion_id,),
        )
        self.assertEqual(failed_conversion["status"], "failed")
        self.assertIn("产物文件不存在", failed_conversion["error"])
        self.assertEqual(failed_conversion["progress_message"], "转换失败")
        conversion_failure_logs = self.client.get(
            f"/api/conversions/{failed_conversion_id}/logs"
        ).json()["logs"]
        self.assertEqual(conversion_failure_logs[-1]["event"], "failed")

        fake_cpu = {
            "id": "cpu",
            "type": "cpu",
            "name": "test cpu",
            "available": True,
        }
        fake_gpu = {
            "id": "cuda:0",
            "type": "cuda",
            "index": 0,
            "name": "test gpu",
            "available": True,
            "total_memory_bytes": 16_000,
            "free_memory_bytes": 12_000,
            "utilization_percent": 0,
        }
        resource_snapshot = {
            "snapshot_at": now_iso(),
            "hostname": "test-host",
            "cuda_available": True,
            "cpu": fake_cpu,
            "devices": [fake_cpu, fake_gpu],
        }
        first_resource = None
        third_resource = None
        first_resource_task = failed_id + 10_000
        second_resource_task = failed_id + 10_001
        third_resource_task = failed_id + 10_002
        try:
            with patch.object(
                training_module, "training_resources", return_value=resource_snapshot
            ):
                first_resource = training_module._acquire_resource_lease(
                    "cuda:0", "balanced", "training", first_resource_task,
                    "worker:gpu-a", 1,
                )
                with self.assertRaises(training_module.ResourceBusy):
                    training_module._acquire_resource_lease(
                        "cuda:0", "balanced", "training", second_resource_task,
                        "worker:gpu-b", 1,
                    )
                active = one(
                    "SELECT task_id, worker_id FROM pipeline_resource_leases WHERE resource_id = ?",
                    ("cuda:0",),
                )
                self.assertEqual(active["task_id"], first_resource_task)
                self.assertEqual(active["worker_id"], "worker:gpu-a")

                training_module._release_file_resource_lock(
                    "training", first_resource_task, 1
                )
                with db() as conn:
                    conn.execute(
                        "DELETE FROM pipeline_resource_leases WHERE resource_id = ?",
                        ("cuda:0",),
                    )
                first_resource = None
                third_resource = training_module._acquire_resource_lease(
                    "cuda:0", "balanced", "training", third_resource_task,
                    "worker:gpu-c", 1,
                )
                self.assertEqual(third_resource["resolved_device"], "cuda:0")
        finally:
            if first_resource is not None:
                training_module._release_file_resource_lock(
                    "training", first_resource_task, 1
                )
            if third_resource is not None:
                training_module._release_file_resource_lock(
                    "training", third_resource_task, 1
                )
            with db() as conn:
                conn.execute("DELETE FROM pipeline_resource_leases")

        cpu_first_task = failed_id + 20_000
        cpu_second_task = failed_id + 20_001
        try:
            with patch.object(
                training_module, "training_resources", return_value=resource_snapshot
            ):
                cpu_resource = training_module._acquire_resource_lease(
                    "cpu", "balanced", "training", cpu_first_task,
                    "worker:cpu-a", 1,
                )
                self.assertEqual(cpu_resource["resolved_device"], "cpu")
                with self.assertRaises(training_module.ResourceBusy):
                    training_module._acquire_resource_lease(
                        "cpu", "balanced", "conversion", cpu_second_task,
                        "worker:cpu-b", 1,
                    )
        finally:
            training_module._release_file_resource_lock(
                "training", cpu_first_task, 1
            )
            with db() as conn:
                conn.execute("DELETE FROM pipeline_resource_leases")

    def test_thumbnail_cache_and_legacy_yolo_split_are_training_safe(self) -> None:
        project = self.client.post(
            "/api/projects", json={"name": "快速导出", "labels": ["object"]}
        ).json()
        project_id = project["id"]
        files = [
            (
                "files",
                (f"sample-{index}.png", png_bytes(color=f"#{index + 30:06x}"), "image/png"),
            )
            for index in range(4)
        ]
        uploaded = self.client.post(f"/api/projects/{project_id}/assets", files=files)
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        asset_ids = uploaded.json()["asset_ids"]
        label_id = self.client.get(f"/api/projects/{project_id}").json()["labels"][0]["id"]
        for index, asset_id in enumerate(asset_ids):
            shape = (
                {
                    "shape_type": "polygon",
                    "points": [20, 20, 100, 20, 110, 90, 20, 100],
                }
                if index == 0
                else {"shape_type": "rectangle", "points": [20, 20, 100, 100]}
            )
            annotation = self.client.post(
                f"/api/assets/{asset_id}/annotations",
                json={"label_id": label_id, "expected_asset_revision": 0, **shape},
            )
            self.assertEqual(annotation.status_code, 201, annotation.text)

        thumbnail = self.client.get(f"/api/assets/{asset_ids[0]}/thumbnail?size=100")
        self.assertEqual(thumbnail.status_code, 200, thumbnail.text)
        self.assertEqual(thumbnail.headers["content-type"], "image/webp")
        self.assertIn("immutable", thumbnail.headers["cache-control"])
        with Image.open(io.BytesIO(thumbnail.content)) as rendered:
            self.assertEqual(rendered.size, (100, 75))
            self.assertEqual(rendered.format, "WEBP")
        cached = self.client.get(f"/api/assets/{asset_ids[0]}/thumbnail?size=100")
        self.assertEqual(cached.content, thumbnail.content)
        self.assertEqual(self.client.get(f"/api/assets/{asset_ids[0]}/thumbnail?size=10").status_code, 422)
        original = self.client.get(f"/api/assets/{asset_ids[0]}/image")
        self.assertEqual(original.status_code, 200)
        self.assertEqual(original.headers["content-type"], "image/png")

        exported = self.client.get(f"/api/projects/{project_id}/export/yolo")
        self.assertEqual(exported.status_code, 200, exported.text)
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            names = archive.namelist()
            yaml = archive.read("data.yaml").decode()
            self.assertIn("train: images/train", yaml)
            self.assertIn("val: images/val", yaml)
            train_images = {
                Path(name).name for name in names if name.startswith("images/train/")
            }
            val_images = {Path(name).name for name in names if name.startswith("images/val/")}
            self.assertTrue(train_images)
            self.assertTrue(val_images)
            self.assertFalse(train_images & val_images)
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(
                {item["split"] for item in manifest["assets"]}, {"train", "val"}
            )
            label_files = [name for name in names if name.startswith("labels/")]
            self.assertEqual(len(label_files), 4)
            for name in label_files:
                for line in archive.read(name).decode().splitlines():
                    tokens = line.split()
                    # A mixed-shape project is exported consistently as YOLO Segment:
                    # class id followed by at least 3 normalized x/y point pairs.
                    self.assertGreaterEqual(len(tokens), 7)
                    self.assertEqual((len(tokens) - 1) % 2, 0)


if __name__ == "__main__":
    unittest.main()
