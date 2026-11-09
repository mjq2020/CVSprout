"""System-model pull protocol against isolated SQLite and immutable files."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
import threading
import unittest
from unittest.mock import patch

from app import edge_models as models
from app.config import settings
from app.database import db, init_db, now_iso, one
from tests import test_edge as edge_tests
from tests import test_edge_deployment as legacy_tests


class EdgeSystemModelsTest(unittest.TestCase):
    setUp = edge_tests.EdgeIngestionTest.setUp
    tearDown = edge_tests.EdgeIngestionTest.tearDown
    create = edge_tests.EdgeIngestionTest.create
    agent_id = "persistent-agent-12345678"

    def headers(self, token=None):
        return {"Authorization": "Bearer " + (token or self.token)}

    def conversion(self, project_id=None):
        conversion_id = legacy_tests.EdgeDeploymentTest.conversion(self, project_id)
        with db() as conn:
            conversion = dict(conn.execute("SELECT * FROM model_conversions WHERE id=?", (conversion_id,)).fetchone())
            metadata = json.loads(conversion["artifact_metadata_json"])
            model_path = Path(conversion["artifact_path"])
            model_path.write_bytes(b"RKNN isolated test payload; never executable")
            metadata.update(sha256=hashlib.sha256(model_path.read_bytes()).hexdigest(), size_bytes=model_path.stat().st_size)
            deployment = metadata["deployment"]
            outputs = [{"shape": [1, channels, 640 // stride, 640 // stride], "dtype": "INT8", "role": role}
                       for stride in (8, 16, 32) for channels, role in ((64, "box"), (1, "class"))]
            deployment.update(deployment_profile="recamera_system", system_model={
                "schema_version": 1, "profile": models.PROFILE, "algorithm": "yolov8", "category": "Detection",
                "input_size": 640, "classes": ["person"], "outputs": outputs,
                "preprocessing": {"color_space": "RGB", "dtype": "uint8", "resize": "letterbox",
                                  "padding_value": 114, "padding_position": "bottom_right"}})
            deployment["source_onnx"]["outputs"] = [{"shape": output["shape"], "dtype": "FLOAT"} for output in outputs]
            deployment["preprocessing"]["padding_position"] = "bottom_right"
            deployment["preprocessing"]["resize_rounding"] = "floor_even"
            deployment["postprocessing"] = {"family": "recamera_system_dfl", "nms_in_graph": False, "end2end": False}
            conn.execute("UPDATE model_conversions SET artifact_metadata_json=? WHERE id=?", (json.dumps(metadata), conversion_id))
            conn.execute("UPDATE training_runs SET model_arch='yolov8n',artifact_metadata_json=? WHERE id=?",
                         (json.dumps({"model_arch": "yolov8n"}), conversion["training_run_id"]))
        return conversion_id

    def release(self, conversion_id=None):
        response = self.client.post("/api/edge/model-releases", json={"conversion_id": conversion_id or self.conversion()})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def status(self, **changes):
        body = {"agent_id": self.agent_id, "capabilities": {"schema_version": 1, "target_platform": "rv1126b",
                "model_api": True, "algorithms": ["yolov8", "yolo11"]}, "current": self.runtime("old-model.rknn"), **changes}
        return self.client.post("/api/edge/v1/system/status", headers=self.headers(), json=body)

    def runtime(self, filename, **changes):
        return {"iEnable": 1, "sModel": filename, "sStatus": "running", "iFPS": 10,
                "iActualFPS": 9.8, "bExternalHold": False, **changes}

    def queue(self, release=None, **changes):
        release = release or self.release()
        return self.client.post(f"/api/edge/devices/{self.device['id']}/model-deployments",
                                json={"release_id": release["id"], **changes})

    def claim(self, **changes):
        return self.client.post("/api/edge/v1/model-tasks/claim", headers=self.headers(),
                                json={"agent_id": self.agent_id, **changes})

    def setup_task(self, activate=True):
        self.assertEqual(self.status().status_code, 200)
        release = self.release()
        queued = self.queue(release, activate=activate)
        self.assertEqual(queued.status_code, 202, queued.text)
        claimed = self.claim(lease_token=secrets.token_urlsafe(32))
        self.assertEqual(claimed.status_code, 200, claimed.text)
        return release, claimed.json()["task"]

    def report(self, task, **changes):
        body = {"agent_id": self.agent_id, "lease_token": task["lease_token"], "sequence": 0,
                "status": "running", "stage": "downloading", **changes}
        return self.client.post(f"/api/edge/v1/model-tasks/{task['id']}/report", headers=self.headers(), json=body)

    def options(self):
        return self.client.get(f"/api/edge/devices/{self.device['id']}/system-models").json()

    def test_model_name_defaults_are_shared_by_candidates_releases_and_deployments(self):
        release = self.release()
        contract = release["contract"]
        expected = f"yolov8-run{contract['training_run_id']}-conv{contract['conversion_id']}"
        self.assertEqual(release["default_model_name"], expected)
        self.assertEqual(self.status().status_code, 200)
        options = self.options()
        self.assertEqual(options["conversions"][0]["default_model_name"], expected)
        self.assertEqual(options["releases"][0]["default_model_name"], expected)
        for changes in ({}, {"model_name": ""}, {"model_name": "   "}):
            with self.subTest(changes=changes):
                response = self.queue(release, **changes)
                self.assertEqual(response.status_code, 202, response.text)
                task = response.json()
                self.assertEqual(task["model_name"], expected)
                self.assertEqual(task["filename"], f"{expected}-r{release['id']}.rknn")
                self.assertEqual(self.options()["deployments"][0]["filename"], task["filename"])
                self.assertEqual(self.client.post(f"/api/edge/devices/{self.device['id']}/model-deployments/{task['id']}/cancel").status_code, 200)

    def test_custom_name_is_preserved_for_claim_download_resume_and_running_verification(self):
        release = self.release()
        self.assertEqual(self.status().status_code, 200)
        queued = self.queue(release, model_name="  Factory_Line-1  ", activate=True)
        self.assertEqual(queued.status_code, 202, queued.text)
        name = f"Factory_Line-1-r{release['id']}.rknn"
        self.assertEqual(queued.json()["model_name"], "Factory_Line-1")
        self.assertEqual(queued.json()["filename"], name)
        task = self.claim(lease_token=secrets.token_urlsafe(32)).json()["task"]
        self.assertEqual(task["filename"], name)
        self.assertEqual(task["model_info"], release["model_info"])
        recovered = self.claim(task_id=task["id"], lease_token=task["lease_token"]).json()["task"]
        self.assertEqual(recovered["filename"], name)
        headers = {**self.headers(), "X-Agent-Id": self.agent_id, "X-Model-Lease": task["lease_token"]}
        response = self.client.get(task["artifact_url"], headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'filename="{name}"', response.headers["content-disposition"])
        self.assertEqual(hashlib.sha256(response.content).hexdigest(), release["sha256"])
        stored = one("SELECT * FROM edge_model_releases WHERE id=?", (release["id"],))
        self.assertEqual(models._release_path(stored).name, release["filename"])
        self.assertNotEqual(release["filename"], name)
        self.assertEqual(self.report(task, status="succeeded", stage="verified", current=self.runtime(release["filename"])).status_code, 409)
        result = self.report(task, status="succeeded", stage="verified", current=self.runtime(name))
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["task"]["filename"], name)
        self.assertEqual(self.options()["current"]["sModel"], name)

    def test_same_release_can_use_different_device_names_and_release_suffix_prevents_collisions(self):
        release = self.release()
        self.assertEqual(self.status().status_code, 200)
        first = self.queue(release, model_name="Camera_A").json()
        other = self.create()
        status = self.client.post("/api/edge/v1/system/status", headers=self.headers(other["device_token"]),
                                 json={"agent_id": self.agent_id, "capabilities": {"model_api": True, "algorithms": ["yolov8"]}})
        self.assertEqual(status.status_code, 200, status.text)
        second = self.client.post(f"/api/edge/devices/{other['id']}/model-deployments",
                                  json={"release_id": release["id"], "model_name": "Camera_B"})
        self.assertEqual(second.status_code, 202, second.text)
        self.assertEqual(second.json()["filename"], f"Camera_B-r{release['id']}.rknn")
        self.assertNotEqual(first["filename"], second.json()["filename"])
        self.assertEqual(one("SELECT COUNT(*) n FROM edge_model_releases")["n"], 1)
        self.assertEqual(self.client.post(f"/api/edge/devices/{self.device['id']}/model-deployments/{first['id']}/cancel").status_code, 200)
        newer = self.release()
        third = self.queue(newer, model_name="Camera_A")
        self.assertEqual(third.status_code, 202, third.text)
        self.assertEqual(third.json()["filename"], f"Camera_A-r{newer['id']}.rknn")
        self.assertNotEqual(first["filename"], third.json()["filename"])

    def test_model_names_reject_unsafe_paths_types_and_lengths_before_enqueue(self):
        release = self.release()
        self.assertEqual(self.status().status_code, 200)
        for invalid in ("../model", "a/b", "a\\b", "a.b", "model.rknn", "-name", "_name", "模型", "a b",
                        "a\x00b", "a\nb", "a" * 65, None, 123, True):
            with self.subTest(model_name=invalid):
                response = self.queue(release, model_name=invalid)
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(one("SELECT COUNT(*) n FROM edge_model_deployments")["n"], 0)
        maximum = self.queue(release, model_name="A" * 64)
        self.assertEqual(maximum.status_code, 202, maximum.text)
        self.assertEqual(maximum.json()["model_name"], "A" * 64)

    def test_name_migration_preserves_claimed_legacy_filename_and_is_idempotent(self):
        release, task = self.setup_task()
        with db() as conn:
            # Reproduce the pre-name schema with a live persisted deployment lease.
            conn.execute("ALTER TABLE edge_model_deployments DROP COLUMN model_name")
            conn.execute("DELETE FROM schema_migrations WHERE version=17")
        before = one("SELECT * FROM edge_model_deployments WHERE id=?", (task["id"],))
        init_db()
        init_db()
        after = one("SELECT * FROM edge_model_deployments WHERE id=?", (task["id"],))
        self.assertEqual(after.pop("model_name"), "")
        self.assertEqual(after, before)
        self.assertEqual(one("SELECT COUNT(*) n FROM schema_migrations WHERE version=17")["n"], 1)
        observed = self.claim(task_id=task["id"], lease_token=task["lease_token"], observe_only=True).json()["task"]
        self.assertEqual(observed["filename"], release["filename"])
        self.assertEqual(observed["model_name"], "")
        self.assertEqual(self.options()["deployments"][0]["filename"], release["filename"])
        headers = {**self.headers(), "X-Agent-Id": self.agent_id, "X-Model-Lease": task["lease_token"]}
        download = self.client.get(task["artifact_url"], headers=headers)
        self.assertEqual(download.status_code, 200)
        self.assertIn(f'filename="{release["filename"]}"', download.headers["content-disposition"])
        self.assertEqual(self.report(task, status="succeeded", stage="verified", current=self.runtime(task["filename"])).status_code, 409)
        legacy_result = self.report(task, status="succeeded", stage="verified", current=self.runtime(release["filename"]))
        self.assertEqual(legacy_result.status_code, 200, legacy_result.text)
        named = self.queue(release, model_name="After_Migration").json()
        init_db()
        self.assertEqual(self.options()["deployments"][0]["filename"], named["filename"])
        self.assertEqual(one("SELECT model_name FROM edge_model_deployments WHERE id=?", (named["id"],))["model_name"], "After_Migration")

    def test_selected_conversion_beyond_recent_limit_keeps_device_deployment_occupancy(self):
        old_conversion = self.conversion()
        old_release = self.release(old_conversion)
        with db() as conn:
            for _ in range(100):
                newer_conversion = conn.execute("""INSERT INTO model_conversions
                    (training_run_id,format,status,artifact_metadata_json,created_by,created_at)
                    SELECT training_run_id,format,status,artifact_metadata_json,created_by,created_at
                    FROM model_conversions WHERE id=?""", (old_conversion,)).lastrowid
                conn.execute("""INSERT INTO edge_model_releases(project_id,conversion_id,status,created_at)
                    VALUES (?,?,'failed',?)""", (self.project_id, newer_conversion, now_iso()))
        active_release = self.release()
        self.assertEqual(self.status().status_code, 200)
        queued = self.queue(active_release)
        self.assertEqual(queued.status_code, 202, queued.text)
        unfiltered = self.options()
        self.assertEqual(len(unfiltered["conversions"]), 100)
        self.assertEqual(len(unfiltered["releases"]), 100)
        self.assertNotIn(old_conversion, [item["id"] for item in unfiltered["conversions"]])
        self.assertNotIn(old_release["id"], [item["id"] for item in unfiltered["releases"]])

        response = self.client.get(f"/api/edge/devices/{self.device['id']}/system-models",
                                   params={"conversion_id": old_conversion})
        self.assertEqual(response.status_code, 200, response.text)
        selected = response.json()
        self.assertEqual([item["id"] for item in selected["conversions"]], [old_conversion])
        self.assertTrue(selected["conversions"][0]["compatible"])
        self.assertEqual([item["id"] for item in selected["releases"]], [old_release["id"]])
        self.assertEqual([item["id"] for item in selected["deployments"]], [queued.json()["id"]])
        self.assertFalse(selected["can_deploy"])
        self.assertIn("已有部署任务", selected["blocked_reason"])

    def test_conversion_filter_cannot_cross_projects_and_rejects_invalid_ids(self):
        local_release = self.release()
        project = self.client.post("/api/projects", json={"name": "Other", "task_type": "detect", "labels": ["other"]}).json()
        foreign_conversion = self.conversion(project["id"])
        self.release(foreign_conversion)
        url = f"/api/edge/devices/{self.device['id']}/system-models"
        for conversion_id in (foreign_conversion, 2**63 - 1):
            response = self.client.get(url, params={"conversion_id": conversion_id})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["project_id"], self.project_id)
            self.assertEqual(response.json()["conversions"], [])
            self.assertEqual(response.json()["releases"], [])
        selected = self.client.get(url, params={"conversion_id": local_release["conversion_id"]}).json()
        self.assertEqual([item["id"] for item in selected["releases"]], [local_release["id"]])
        for invalid in ("0", "-1", "abc", "1.5", str(2**63)):
            with self.subTest(conversion_id=invalid):
                self.assertEqual(self.client.get(url, params={"conversion_id": invalid}).status_code, 422)
        for role in ("annotator", "reviewer"):
            self.assertEqual(self.client.get(url, params={"conversion_id": local_release["conversion_id"]},
                                             headers={"x-test-role": role}).status_code, 403)

    def test_release_is_immutable_and_checks_both_hashes(self):
        conversion = self.conversion()
        release = self.release(conversion)
        again = self.release(conversion)
        self.assertEqual(release["id"], again["id"])
        self.assertFalse(release["contract"]["board_validated"])
        self.assertEqual(release["model_info"]["algorithm"], "yolov8")
        original = Path(one("SELECT artifact_path FROM model_conversions WHERE id=?", (conversion,))["artifact_path"])
        path = models._release_path(one("SELECT * FROM edge_model_releases WHERE id=?", (release["id"],)))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), release["sha256"])
        self.assertEqual(hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest(), release["md5"])
        original.write_bytes(b"changed source after release")
        self.assertEqual(models._release_path(one("SELECT * FROM edge_model_releases WHERE id=?", (release["id"],))), path)
        path.chmod(0o644)
        path.write_bytes(b"changed immutable release")
        self.status()
        self.assertEqual(self.queue(release).status_code, 409)

    def test_interrupted_release_build_can_retry_and_corrupt_source_never_publishes(self):
        conversion_id = self.conversion()
        with db() as conn:
            old_id = conn.execute("""INSERT INTO edge_model_releases(project_id,conversion_id,status,created_at)
                VALUES (?,?,'building','2000-01-01T00:00:00+00:00')""", (self.project_id, conversion_id)).lastrowid
        release = self.release(conversion_id)
        self.assertNotEqual(release["id"], old_id)
        self.assertEqual(one("SELECT status FROM edge_model_releases WHERE id=?", (old_id,))["status"], "failed")
        broken = self.conversion()
        source = Path(one("SELECT artifact_path FROM model_conversions WHERE id=?", (broken,))["artifact_path"])
        source.write_bytes(b"RKNN corrupt source")
        response = self.client.post("/api/edge/model-releases", json={"conversion_id": broken})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(one("SELECT status FROM edge_model_releases WHERE conversion_id=?", (broken,))["status"], "failed")

    def test_task_metadata_budget_counts_escaped_labels_and_allows_large_valid_receipts(self):
        def many_classes(count):
            conversion_id = self.conversion()
            with db() as conn:
                metadata = json.loads(conn.execute("SELECT artifact_metadata_json FROM model_conversions WHERE id=?", (conversion_id,)).fetchone()[0])
                deployment = metadata["deployment"]
                names = [str(index) + "类" * 195 for index in range(count)]
                deployment["system_model"]["classes"] = names
                deployment["labels"] = [{"category_index": index, "name": name} for index, name in enumerate(names)]
                for index in (1, 3, 5):
                    deployment["system_model"]["outputs"][index]["shape"][1] = count
                    deployment["source_onnx"]["outputs"][index]["shape"][1] = count
                conn.execute("UPDATE model_conversions SET artifact_metadata_json=? WHERE id=?", (json.dumps(metadata), conversion_id))
            return conversion_id

        too_large = many_classes(500)
        rejected = self.client.post("/api/edge/model-releases", json={"conversion_id": too_large})
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertIn("1 MiB", rejected.json()["detail"])
        self.assertEqual(one("SELECT COUNT(*) n FROM edge_model_releases")["n"], 0)
        release = self.release(many_classes(100))
        self.status()
        self.assertEqual(self.queue(release).status_code, 202)
        claimed = self.claim(lease_token=secrets.token_urlsafe(32))
        self.assertEqual(claimed.status_code, 200, claimed.text[:100])
        self.assertGreater(len(claimed.content), 65536)
        self.assertLess(len(json.dumps(claimed.json(), ensure_ascii=True).encode()), 2 * 1024 * 1024)

    def test_legacy_outputs_wrong_chip_preprocessing_dtype_and_architecture_rejected(self):
        legacy = legacy_tests.EdgeDeploymentTest.conversion(self)
        self.assertEqual(self.client.post("/api/edge/model-releases", json={"conversion_id": legacy}).status_code, 409)
        for change in (
            lambda d: d.update(target_platform="rk3588"),
            lambda d: d.update(precision="float16"),
            lambda d: d["system_model"].update(algorithm="yolo26"),
            lambda d: d["system_model"]["preprocessing"].update(padding_position="center"),
            lambda d: d["preprocessing"].update(std_values=[1, 1, 1]),
            lambda d: d["preprocessing"].update(resize_rounding="round"),
            lambda d: d["system_model"]["outputs"][0].update(dtype="FLOAT"),
            lambda d: d["system_model"]["outputs"][1].update(shape=[1, 80, 80, 80]),
            lambda d: d["source_onnx"].update(outputs=[{"shape": [1, 5, 8400], "dtype": "FLOAT"}]),
            lambda d: d["postprocessing"].update(nms_in_graph=True),
        ):
            conversion_id = self.conversion()
            with db() as conn:
                row = conn.execute("SELECT artifact_metadata_json FROM model_conversions WHERE id=?", (conversion_id,)).fetchone()
                metadata = json.loads(row[0])
                change(metadata["deployment"])
                conn.execute("UPDATE model_conversions SET artifact_metadata_json=? WHERE id=?", (json.dumps(metadata), conversion_id))
            response = self.client.post("/api/edge/model-releases", json={"conversion_id": conversion_id})
            self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(one("SELECT COUNT(*) n FROM edge_model_releases")["n"], 0)

    def test_status_is_separate_and_staleness_does_not_invent_running(self):
        self.assertIsNone(self.options()["current"])
        self.assertFalse(self.options()["can_deploy"])
        at = datetime(2100, 1, 1, tzinfo=timezone.utc)
        with patch.object(models, "now_iso", return_value=at.isoformat()):
            response = self.status(error_code="Authorization: Bearer cvsprout_device_secret", firmware_version="1.2.3")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(self.options()["system_status"]["fresh"])
            self.assertEqual(self.options()["system_status"]["error_code"], "device_error")
        with patch.object(models, "now_iso", return_value=(at + timedelta(seconds=91)).isoformat()):
            self.assertFalse(self.options()["system_status"]["fresh"])
        self.assertNotIn("credential_hash", json.dumps(self.options()))
        self.assertEqual(self.client.get("/api/edge/v1/config", headers=self.headers()).json()["system_model_schema_version"], 1)

    def test_queue_requires_capability_but_may_wait_for_offline_device(self):
        release = self.release()
        self.assertEqual(self.queue(release).status_code, 409)
        self.status(capabilities={"model_api": True, "algorithms": ["yolo11"]})
        self.assertEqual(self.queue(release).status_code, 409)
        self.status()
        with db() as conn:
            conn.execute("UPDATE edge_system_status SET reported_at='2000-01-01T00:00:00+00:00'")
        queued = self.queue(release)
        self.assertEqual(queued.status_code, 202, queued.text)
        self.assertEqual(queued.json()["stage"], "queued")
        self.assertEqual(self.queue(release).status_code, 409)

    def test_first_claim_lost_reply_is_recoverable_and_other_agents_cannot_steal(self):
        self.status()
        queued = self.queue()
        token = secrets.token_urlsafe(32)
        first = self.claim(lease_token=token)
        retry = self.claim(lease_token=token)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(retry.json()["task"]["id"], queued.json()["id"])
        self.assertEqual(first.json()["task"]["lease_token"], token)
        self.assertEqual(self.claim().status_code, 409)
        self.assertEqual(self.claim(agent_id="different-agent", lease_token=token).status_code, 409)
        self.assertEqual(self.status(agent_id="different-agent").status_code, 409)
        stored = one("SELECT * FROM edge_model_deployments WHERE id=?", (queued.json()["id"],))
        self.assertNotIn(token, json.dumps(stored))
        self.assertNotIn("lease_hash", json.dumps(self.options()))

    def test_artifact_uses_own_device_and_lease_and_detects_tampering(self):
        release, task = self.setup_task()
        url = task["artifact_url"]
        self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.client.get(url, headers=self.headers()).status_code, 409)
        headers = {**self.headers(), "X-Agent-Id": self.agent_id, "X-Model-Lease": task["lease_token"]}
        response = self.client.get(url, headers=headers)
        self.assertEqual(response.status_code, 200, response.text[:100])
        self.assertEqual(hashlib.sha256(response.content).hexdigest(), release["sha256"])
        other = self.create()
        self.assertEqual(self.client.get(url, headers={**headers, **self.headers(other["device_token"])}).status_code, 404)
        path = models._release_path(one("SELECT * FROM edge_model_releases WHERE id=?", (release["id"],)))
        path.chmod(0o644)
        path.write_bytes(b"tampered")
        self.assertEqual(self.client.get(url, headers=headers).status_code, 409)

    def test_import_success_is_not_activation_and_reports_are_idempotent(self):
        _, task = self.setup_task(activate=False)
        invalid = self.report(task, status="succeeded", stage="verified", current=self.runtime(task["filename"]))
        self.assertEqual(invalid.status_code, 409)
        success = self.report(task, status="succeeded", stage="uploaded")
        self.assertEqual(success.status_code, 200, success.text)
        repeated = self.report(task, status="succeeded", stage="uploaded")
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(self.report(task, sequence=1, status="running", stage="uploading").status_code, 409)
        terminal_claim = self.claim(task_id=task["id"], lease_token=task["lease_token"])
        self.assertEqual(terminal_claim.json()["task"]["status"], "succeeded")
        self.assertEqual(self.claim(lease_token=task["lease_token"]).json()["task"]["id"], task["id"])
        self.assertEqual(self.options()["current"]["sModel"], "old-model.rknn")

    def test_omitting_activate_defaults_to_import_only_and_rejects_activation_reports(self):
        self.assertEqual(self.status().status_code, 200)
        queued = self.queue()
        self.assertEqual(queued.status_code, 202, queued.text)
        self.assertIs(queued.json()["activate"], False)
        stored = one("SELECT activate FROM edge_model_deployments WHERE id=?", (queued.json()["id"],))
        self.assertEqual(stored["activate"], 0)
        task = self.claim(lease_token=secrets.token_urlsafe(32)).json()["task"]
        self.assertIs(task["activate"], False)
        self.assertEqual(self.report(task, status="running", stage="activating").status_code, 409)
        self.assertEqual(self.report(task, status="succeeded", stage="verified", current=self.runtime(task["filename"])).status_code, 409)
        completed = self.report(task, status="succeeded", stage="uploaded")
        self.assertEqual(completed.status_code, 200, completed.text)
        self.assertEqual(completed.json()["task"]["stage"], "uploaded")
        self.assertEqual(self.options()["current"]["sModel"], "old-model.rknn")

    def test_activation_requires_actual_running_matching_model_and_no_hold(self):
        _, task = self.setup_task()
        for current in (None, self.runtime("other.rknn"), self.runtime(task["filename"], iActualFPS=0),
                        self.runtime(task["filename"], bExternalHold=True), self.runtime(task["filename"], sStatus="starting"),
                        self.runtime(task["filename"], iFPS=30)):
            response = self.report(task, status="succeeded", stage="verified", current=current)
            self.assertEqual(response.status_code, 409, response.text)
        success = self.report(task, status="succeeded", stage="verified", current=self.runtime(task["filename"]))
        self.assertEqual(success.status_code, 200, success.text)
        self.assertEqual(self.options()["current"]["sModel"], task["filename"])

    def test_expired_lease_is_not_reassigned_unknown_keeps_device_locked(self):
        at = datetime(2100, 1, 1, tzinfo=timezone.utc)
        with patch.object(models, "now_iso", return_value=at.isoformat()):
            release, task = self.setup_task()
        with patch.object(models, "now_iso", return_value=(at + timedelta(seconds=models.LEASE_SECONDS + 1)).isoformat()):
            self.assertTrue(self.options()["deployments"][0]["lease_expired"])
            self.assertEqual(self.report(task).status_code, 409)
            self.assertEqual(self.claim(lease_token=secrets.token_urlsafe(32)).status_code, 409)
            self.assertEqual(self.queue(release).status_code, 409)
            resumed = self.claim(task_id=task["id"], lease_token=task["lease_token"])
            self.assertEqual(resumed.status_code, 200, resumed.text)
            self.assertEqual(self.report(task, status="unknown", stage="unknown", rollback_status="unknown").status_code, 200)
            self.assertEqual(self.queue(release).status_code, 409)
            self.assertEqual(self.report(task, sequence=1, status="failed", stage="failed", rollback_status="unknown").status_code, 409)
            resolved = self.report(task, sequence=1, status="failed", stage="failed", rollback_status="restored")
            self.assertEqual(resolved.status_code, 200, resolved.text)
            self.assertEqual(self.queue(release).status_code, 202)

    def test_observe_unknown_does_not_renew_and_admin_receipt_remains_readable(self):
        at = datetime(2100, 1, 1, tzinfo=timezone.utc)
        with patch.object(models, "now_iso", return_value=at.isoformat()):
            _, task = self.setup_task()
            self.assertEqual(self.report(task, status="unknown", stage="unknown").status_code, 200)
            before = one("SELECT * FROM edge_model_deployments WHERE id=?", (task["id"],))
            self.assertEqual(self.claim(observe_only=True, lease_token=task["lease_token"]).status_code, 422)
        for seconds in (30, 90, 121, 180):
            with patch.object(models, "now_iso", return_value=(at + timedelta(seconds=seconds)).isoformat()):
                observed = self.claim(task_id=task["id"], lease_token=task["lease_token"], observe_only=True)
                self.assertEqual(observed.status_code, 200, observed.text)
                self.assertEqual(observed.json()["task"]["lease_expires_at"], before["lease_expires_at"])
                self.assertEqual(observed.json()["task"]["updated_at"], before["updated_at"])
        with patch.object(models, "now_iso", return_value=(at + timedelta(seconds=180)).isoformat()):
            resolved = self.client.post(f"/api/edge/devices/{self.device['id']}/model-deployments/{task['id']}/resolve",
                json={"confirmation_name": self.device["name"], "acknowledged": True, "expected_updated_at": before["updated_at"]})
            self.assertEqual(resolved.status_code, 200, resolved.text)
            receipt = self.claim(task_id=task["id"], lease_token=task["lease_token"], observe_only=True)
            self.assertEqual(receipt.status_code, 200, receipt.text)
            self.assertEqual(receipt.json()["task"]["status"], "failed")
            self.assertIsNone(receipt.json()["task"]["lease_expires_at"])

    def test_observed_unknown_requires_real_renewal_before_verified_report(self):
        at = datetime(2100, 1, 1, tzinfo=timezone.utc)
        with patch.object(models, "now_iso", return_value=at.isoformat()):
            _, task = self.setup_task()
            self.report(task, status="unknown", stage="unknown")
        with patch.object(models, "now_iso", return_value=(at + timedelta(seconds=121)).isoformat()):
            self.claim(task_id=task["id"], lease_token=task["lease_token"], observe_only=True)
            body = {"sequence": 1, "status": "succeeded", "stage": "verified", "current": self.runtime(task["filename"])}
            self.assertEqual(self.report(task, **body).status_code, 409)
            self.assertEqual(self.claim(task_id=task["id"], lease_token=task["lease_token"]).status_code, 200)
            self.assertEqual(self.report(task, **body).status_code, 200)

    def test_report_sequence_conflicts_and_phase_regression_rejected(self):
        _, task = self.setup_task()
        self.assertEqual(self.report(task, sequence=10, stage="uploaded").status_code, 200)
        self.assertEqual(self.report(task, sequence=10, stage="uploaded").status_code, 200)
        self.assertEqual(self.report(task, sequence=10, stage="activating").status_code, 409)
        self.assertEqual(self.report(task, sequence=9, stage="uploading").status_code, 409)
        self.assertEqual(self.report(task, sequence=11, stage="uploading").status_code, 409)

    def test_token_rotation_revocation_and_archive_fence_every_device_route(self):
        _, task = self.setup_task()
        new_token = self.client.post(f"/api/edge/devices/{self.device['id']}/rotate-token").json()["device_token"]
        self.assertEqual(self.report(task).status_code, 401)
        self.token = new_token
        self.assertEqual(self.report(task).status_code, 409)
        self.assertEqual(self.claim(task_id=task["id"], lease_token=task["lease_token"]).status_code, 409)
        self.assertIsNone(self.options()["capabilities"])
        self.status()
        with db() as conn:
            conn.execute("UPDATE projects SET archived_at=? WHERE id=?", (now_iso(), self.project_id))
        self.assertEqual(self.status().status_code, 409)
        self.assertEqual(self.claim().status_code, 409)
        self.client.delete(f"/api/edge/devices/{self.device['id']}")
        self.assertEqual(self.status().status_code, 401)

    def test_authentication_rechecked_inside_transaction(self):
        _, task = self.setup_task()
        authenticated = one("SELECT * FROM edge_devices WHERE id=?", (self.device["id"],))

        def revoke_after_authentication():
            with db() as conn:
                conn.execute("UPDATE edge_devices SET token_hash=NULL,enabled=0 WHERE id=?", (self.device["id"],))
            return authenticated

        self.app.dependency_overrides[models.get_device] = revoke_after_authentication
        self.assertEqual(self.report(task).status_code, 401)
        self.assertEqual(one("SELECT last_sequence FROM edge_model_deployments WHERE id=?", (task["id"],))["last_sequence"], -1)

    def test_queued_cancellation_and_project_scoping(self):
        self.status()
        release = self.release()
        job = self.queue(release).json()
        url = f"/api/edge/devices/{self.device['id']}/model-deployments/{job['id']}/cancel"
        self.assertEqual(self.client.post(url).status_code, 200)
        self.assertEqual(self.client.post(url).status_code, 200)
        self.assertIsNone(self.claim().json()["task"])
        _, claimed = self.setup_task()
        url = f"/api/edge/devices/{self.device['id']}/model-deployments/{claimed['id']}/cancel"
        self.assertEqual(self.client.post(url).status_code, 409)
        project = self.client.post("/api/projects", json={"name": "Other", "task_type": "detect", "labels": ["other"]}).json()
        other = self.create(project_id=project["id"])
        options = self.client.get(f"/api/edge/devices/{other['id']}/system-models")
        self.assertEqual(options.status_code, 200, options.text)
        self.assertEqual(options.json()["project_id"], project["id"])
        self.assertEqual(options.json()["conversions"], [])
        self.assertEqual(options.json()["releases"], [])
        response = self.client.post(f"/api/edge/devices/{other['id']}/model-deployments", json={"release_id": release["id"]})
        self.assertEqual(response.status_code, 404)

    def test_admin_only_and_legacy_deployments_are_mutually_exclusive(self):
        release, task = self.setup_task()
        for role in ("annotator", "reviewer"):
            for path, method, body in ((f"/api/edge/devices/{self.device['id']}/system-models", "GET", None),
                                      ("/api/edge/model-releases", "POST", {"conversion_id": release["conversion_id"]}),
                                      (f"/api/edge/devices/{self.device['id']}/model-deployments", "POST", {"release_id": release["id"]})):
                self.assertEqual(self.client.request(method, path, headers={"x-test-role": role}, json=body).status_code, 403)
        conversion = legacy_tests.EdgeDeploymentTest.conversion(self)
        response = self.client.post("/api/edge/releases", json={"conversion_id": conversion})
        self.assertEqual(response.status_code, 201, response.text)
        legacy_release = response.json()
        self.assertEqual(legacy_tests.EdgeDeploymentTest.queue(self, legacy_release).status_code, 409)
        self.report(task, status="failed", stage="failed")
        legacy = legacy_tests.EdgeDeploymentTest.queue(self, legacy_release)
        self.assertEqual(legacy.status_code, 202, legacy.text)
        self.assertEqual(self.queue(release).status_code, 409)

    def test_concurrent_claims_have_one_owner_and_repeatable_migration(self):
        self.status()
        self.queue()
        barrier = threading.Barrier(2)

        def attempt(_):
            barrier.wait()
            return self.claim(lease_token=secrets.token_urlsafe(32)).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            codes = list(pool.map(attempt, range(2)))
        self.assertEqual(sorted(codes), [200, 409])
        init_db()
        self.assertEqual(one("SELECT COUNT(*) n FROM schema_migrations WHERE version=15")["n"], 1)
        self.assertEqual(one("SELECT COUNT(*) n FROM edge_model_deployments")["n"], 1)

    def test_admin_resolution_requires_expiry_name_and_revision_and_fences_old_agent(self):
        at = datetime(2100, 1, 1, tzinfo=timezone.utc)
        with patch.object(models, "now_iso", return_value=at.isoformat()):
            _, task = self.setup_task()
            url = f"/api/edge/devices/{self.device['id']}/model-deployments/{task['id']}/resolve"
            body = {"confirmation_name": self.device["name"], "acknowledged": True, "expected_updated_at": task["updated_at"]}
            self.assertEqual(self.client.post(url, json=body).status_code, 409)
        with patch.object(models, "now_iso", return_value=(at + timedelta(seconds=models.LEASE_SECONDS + 1)).isoformat()):
            self.assertEqual(self.client.post(url, json={**body, "confirmation_name": "wrong"}).status_code, 409)
            self.assertEqual(self.client.post(url, json={**body, "expected_updated_at": "stale"}).status_code, 409)
            old_token = self.token
            self.token = self.client.post(f"/api/edge/devices/{self.device['id']}/rotate-token").json()["device_token"]
            result = self.client.post(url, json=body)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["status"], "failed")
            self.assertEqual(result.json()["rollback_status"], "unknown")
            receipt = self.claim(task_id=task["id"], lease_token=task["lease_token"])
            self.assertEqual(receipt.status_code, 200, receipt.text)
            self.assertEqual(receipt.json()["task"]["status"], "failed")
            self.assertIsNone(receipt.json()["task"]["lease_expires_at"])
            headers = {**self.headers(), "X-Agent-Id": self.agent_id, "X-Model-Lease": task["lease_token"]}
            self.assertEqual(self.client.get(task["artifact_url"], headers=headers).status_code, 409)
            self.assertEqual(self.report(task, status="succeeded", stage="verified", current=self.runtime(task["filename"])).status_code, 409)
            old = self.client.post("/api/edge/v1/model-tasks/claim", headers=self.headers(old_token),
                                   json={"agent_id": self.agent_id, "task_id": task["id"], "lease_token": task["lease_token"]})
            self.assertEqual(old.status_code, 401)
            self.assertEqual(self.status().status_code, 200)
            self.assertEqual(self.queue().status_code, 202)

    def test_project_deletion_removes_owned_models_and_blocks_unresolved_deployment(self):
        self.status()
        release = self.release()
        job = self.queue(release).json()
        url = f"/api/projects/{self.project_id}"
        body = {"confirmation_name": "Device images"}
        self.assertEqual(self.client.request("DELETE", url, json=body).status_code, 409)
        self.client.post(f"/api/edge/devices/{self.device['id']}/model-deployments/{job['id']}/cancel")
        output = settings.training_dir / f"edge-model-release-{release['id']}"
        self.assertTrue(output.exists())
        response = self.client.request("DELETE", url, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(output.exists())
        self.assertEqual(one("SELECT COUNT(*) n FROM edge_model_releases")["n"], 0)
