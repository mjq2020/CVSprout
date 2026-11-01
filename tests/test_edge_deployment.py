"""Device releases and queued deployment invariants against isolated databases."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import threading
from unittest.mock import Mock, patch
import unittest

from app import edge_deployment as deployment
from app.config import settings
from app.database import db, now_iso, one
from tests import test_edge as edge_tests
from tests.test_recamera_packages import model_fixture


class EdgeDeploymentTest(unittest.TestCase):
    setUp = edge_tests.EdgeIngestionTest.setUp
    tearDown = edge_tests.EdgeIngestionTest.tearDown
    create = edge_tests.EdgeIngestionTest.create

    def conversion(self, project_id=None):
        project_id = project_id or self.project_id
        source, run_fixture, _ = model_fixture()
        with db() as conn:
            version = conn.execute("""INSERT INTO dataset_versions(project_id,version_number,name,train_ratio,valid_ratio,test_ratio,
                content_hash,asset_count,annotation_count,created_by,created_at)
                VALUES (?,?,?,0.8,0.1,0.1,?,1,1,'test',?)""",
                (project_id, conn.execute("SELECT COUNT(*)+1 FROM dataset_versions WHERE project_id=?", (project_id,)).fetchone()[0],
                 "Isolated fixture", "a" * 64, now_iso())).lastrowid
            run_id = conn.execute("""INSERT INTO training_runs(project_id,version_id,name,model_arch,task,status,total_epochs,
                artifact_metadata_json,created_by,created_at) VALUES (?,?,?,'yolo26n','detect','completed',1,?,'test',?)""",
                (project_id, version, "Completed fixture run", run_fixture["artifact_metadata_json"], now_iso())).lastrowid
            conversion_id = conn.execute("""INSERT INTO model_conversions(training_run_id,format,status,created_by,created_at)
                VALUES (?,'rknn','completed','test',?)""", (run_id, now_iso())).lastrowid
            model_dir = settings.training_dir / f"conversion-{conversion_id}" / "attempt-1"
            model_dir.mkdir(parents=True)
            model = model_dir / "model.rknn"
            model.write_bytes(b"mock RKNN payload for packaging integrity; never used for hardware inference")
            metadata = json.loads(source["artifact_metadata_json"])
            metadata.update(sha256=hashlib.sha256(model.read_bytes()).hexdigest(), size_bytes=model.stat().st_size)
            conn.execute("UPDATE model_conversions SET artifact_path=?,artifact_metadata_json=? WHERE id=?",
                         (str(model), json.dumps(metadata), conversion_id))
        return conversion_id

    def release(self):
        conversion = self.conversion()
        response = self.client.post("/api/edge/releases", json={"conversion_id": conversion})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def queue(self, release=None, device=None, **changes):
        release = release or self.release()
        device = device or self.device
        with patch.object(deployment, "ssh_readiness", return_value={"ssh_available": True, "known_hosts_configured": True, "key_configured": True}):
            return self.client.post(f"/api/edge/devices/{device['id']}/deployments",
                                    json={"release_id": release["id"], "host": "192.0.2.10", **changes})

    def test_real_package_release_is_verified_downloadable_and_idempotent(self):
        conversion_id = self.conversion()
        response = self.client.post("/api/edge/releases", json={"conversion_id": conversion_id})
        self.assertEqual(response.status_code, 201, response.text)
        release = response.json()
        self.assertEqual(release["status"], "ready")
        self.assertEqual(release["model_contract"]["classes"], ["person"])
        self.assertFalse(release["model_contract"]["board_validated"])
        download = self.client.get(release["download_url"])
        self.assertEqual(download.status_code, 200, download.text[:100] if download.status_code != 200 else "")
        self.assertEqual(hashlib.sha256(download.content).hexdigest(), release["sha256"])
        again = self.client.post("/api/edge/releases", json={"conversion_id": conversion_id})
        self.assertEqual(again.status_code, 201, again.text)
        self.assertEqual(again.json()["id"], release["id"])
        self.assertEqual(one("SELECT COUNT(*) AS n FROM edge_releases")["n"], 1)
        options = self.client.get(f"/api/edge/devices/{self.device['id']}/deployment-options").json()
        self.assertTrue(options["conversions"][0]["compatible"])
        listed = self.client.get(f"/api/edge/releases?device_id={self.device['id']}").json()
        self.assertEqual([row["id"] for row in listed["releases"]], [release["id"]])

    def test_tampered_model_or_release_is_rejected(self):
        conversion_id = self.conversion()
        source = one("SELECT artifact_path FROM model_conversions WHERE id=?", (conversion_id,))
        Path(source["artifact_path"]).write_bytes(b"tampered source")
        response = self.client.post("/api/edge/releases", json={"conversion_id": conversion_id})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(one("SELECT status FROM edge_releases")["status"], "failed")
        release = self.release()
        row = one("SELECT * FROM edge_releases WHERE id=?", (release["id"],))
        path = deployment._release_path(row)
        path.write_bytes(b"tampered app")
        self.assertEqual(self.client.get(release["download_url"]).status_code, 409)
        self.assertEqual(self.queue(release).status_code, 409)

    def test_release_sequences_increase_even_with_frozen_or_backwards_clock(self):
        with patch.object(deployment.time, "time", return_value=1800000000):
            first = self.release()
            second = self.release()
        with patch.object(deployment.time, "time", return_value=1700000000):
            third = self.release()
        self.assertGreater(second["sequence"], first["sequence"])
        self.assertGreater(third["sequence"], second["sequence"])
        for release in (first, second, third):
            self.assertEqual(self.client.get(release["download_url"]).status_code, 200)

    def test_release_download_survives_project_deletion_after_snapshot(self):
        release = self.release()
        expected_digest = release["sha256"]
        original_pin = deployment.pinned_file_response

        def pin_then_delete(*args, **kwargs):
            response = original_pin(*args, **kwargs)
            deleted = self.client.request("DELETE", f"/api/projects/{self.project_id}",
                                          json={"confirmation_name": "Device images"})
            self.assertEqual(deleted.status_code, 200, deleted.text)
            return response

        with patch.object(deployment, "pinned_file_response", side_effect=pin_then_delete):
            response = self.client.get(release["download_url"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(hashlib.sha256(response.content).hexdigest(), expected_digest)
        self.assertEqual(list((settings.training_dir / ".downloads").iterdir()), [])

    def test_deployment_requires_ssh_readiness_and_rejects_command_injection(self):
        release = self.release()
        url = f"/api/edge/devices/{self.device['id']}/deployments"
        with patch.object(deployment, "ssh_readiness", return_value={"ssh_available": True, "known_hosts_configured": False, "key_configured": False}):
            response = self.client.post(url, json={"release_id": release["id"], "host": "192.0.2.10"})
        self.assertEqual(response.status_code, 409)
        for changes in ({"host": "-oProxyCommand=evil"}, {"host": "host;id"}, {"host": "$(id)"},
                        {"host": "host\nother"}, {"username": "root;id"}, {"port": True}, {"port": 0}):
            response = self.queue(release, **changes)
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(one("SELECT COUNT(*) AS n FROM edge_deployments")["n"], 0)

    def test_devices_cannot_deploy_or_list_another_projects_model(self):
        release = self.release()
        project = self.client.post("/api/projects", json={"name": "Other project", "task_type": "detect", "labels": ["other"]}).json()
        device = self.create(project_id=project["id"])
        response = self.queue(release, device=device)
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self.client.get(f"/api/edge/releases?device_id={device['id']}").json()["releases"], [])
        self.assertEqual(self.client.get(f"/api/edge/devices/{device['id']}/deployment-options").json()["conversions"], [])

    def test_options_skip_run_removed_after_conversion_query(self):
        self.conversion()
        original_one = deployment.one

        def remove_run_from_snapshot(query, params=()):
            if query.startswith("SELECT * FROM training_runs WHERE id="):
                return None
            return original_one(query, params)

        with patch.object(deployment, "one", side_effect=remove_run_from_snapshot):
            response = self.client.get(f"/api/edge/devices/{self.device['id']}/deployment-options")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["conversions"], [])

    def test_one_queued_deployment_per_device_and_worker_completes_without_requeue(self):
        release = self.release()
        first = self.queue(release)
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(self.queue(release).status_code, 409)
        worker = deployment.EdgeDeploymentWorker()
        job = worker.claim()
        self.assertEqual(job["id"], first.json()["id"])
        self.assertIsNone(worker.claim())
        with patch.object(deployment, "execute_deployment", return_value={"status": "succeeded", "stage": "running", "instance_id": "test", "generation": 2}) as execute:
            worker.process(job)
            execute.assert_called_once()
        self.assertIsNone(worker.claim())
        history = self.client.get(f"/api/edge/devices/{self.device['id']}/deployments").json()["deployments"]
        self.assertEqual(history[0]["status"], "succeeded")
        self.assertEqual(history[0]["report"]["generation"], 2)
        self.assertNotIn("lease_expires_at", history[0])

    def test_two_device_records_cannot_deploy_to_same_host_concurrently(self):
        release = self.release()
        second_device = self.create(name="Duplicate physical target")
        self.assertEqual(self.queue(release, host="Camera.Example.").status_code, 202)
        response = self.queue(release, device=second_device, host="camera.example")
        self.assertEqual(response.status_code, 409, response.text)

    def test_expired_running_job_becomes_unknown_and_cannot_repeat_install(self):
        queued = self.queue()
        self.assertEqual(queued.status_code, 202, queued.text)
        worker = deployment.EdgeDeploymentWorker()
        job = worker.claim()
        with db() as conn:
            conn.execute("UPDATE edge_deployments SET lease_expires_at=? WHERE id=?",
                         ((datetime.now(UTC) - timedelta(minutes=20)).isoformat(), job["id"]))
        with patch.object(deployment, "execute_deployment") as execute:
            self.assertIsNone(worker.claim())
            self.assertIsNone(worker.claim())
            execute.assert_not_called()
        self.assertEqual(one("SELECT status FROM edge_deployments WHERE id=?", (job["id"],))["status"], "unknown")

    def test_revoke_or_archive_while_queued_prevents_remote_mutation(self):
        release = self.release()
        queued = self.queue(release)
        self.assertEqual(queued.status_code, 202)
        self.client.delete(f"/api/edge/devices/{self.device['id']}")
        worker = deployment.EdgeDeploymentWorker()
        with patch.object(deployment, "execute_deployment") as execute:
            worker.process(worker.claim())
            execute.assert_not_called()
        self.assertEqual(one("SELECT status FROM edge_deployments")["status"], "failed")
        self.assertEqual(self.queue(release).status_code, 409)
        device2 = self.create(name="Another camera")
        with db() as conn:
            conn.execute("UPDATE projects SET archived_at=? WHERE id=?", (now_iso(), self.project_id))
        self.assertEqual(self.queue(release, device=device2).status_code, 409)
        self.assertEqual(self.client.post("/api/edge/releases", json={"conversion_id": release["conversion_id"]}).status_code, 409)

    def test_project_delete_waits_for_deploy_then_cleans_release(self):
        release = self.release()
        self.assertEqual(self.queue(release).status_code, 202)
        url = f"/api/projects/{self.project_id}"
        response = self.client.request("DELETE", url, json={"confirmation_name": "Device images"})
        self.assertEqual(response.status_code, 409)
        worker = deployment.EdgeDeploymentWorker()
        with patch.object(deployment, "execute_deployment", return_value={"status": "failed", "stage": "preflight", "error": "fixture"}):
            worker.process(worker.claim())
        response = self.client.request("DELETE", url, json={"confirmation_name": "Device images"})
        self.assertEqual(response.status_code, 200, response.text)
        for table in ("edge_releases", "edge_deployments", "edge_devices"):
            self.assertEqual(one(f"SELECT COUNT(*) AS n FROM {table}")["n"], 0)
        self.assertFalse((settings.training_dir / f"edge-release-{release['id']}").exists())

    def test_pending_package_build_expires_and_does_not_block_forever(self):
        conversion_id = self.conversion()
        with db() as conn:
            conn.execute("INSERT INTO edge_releases(project_id,conversion_id,status,created_at) VALUES (?,?,'building',?)",
                         (self.project_id, conversion_id, (datetime.now(UTC) - timedelta(minutes=20)).isoformat()))
        self.assertIsNone(deployment.EdgeDeploymentWorker().claim())
        self.assertEqual(one("SELECT status FROM edge_releases")["status"], "failed")

    def test_remote_missing_or_malformed_report_is_uncertain_and_ssh_is_strict(self):
        release = self.release()
        queued = self.queue(release).json()
        job = one("SELECT * FROM edge_deployments WHERE id=?", (queued["id"],))
        known = settings.data_dir / "known_hosts"
        known.write_text("fixture known-host entry\n")
        old_hosts, old_key = settings.recamera_known_hosts, settings.recamera_ssh_key
        object.__setattr__(settings, "recamera_known_hosts", str(known))
        object.__setattr__(settings, "recamera_ssh_key", "")
        try:
            for output in (b"connection ended\n", b"CVSPROUT_DEPLOY={broken\n", b'CVSPROUT_DEPLOY={"status":"unexpected"}\n'):
                process = Mock(returncode=0)
                process.communicate.return_value = (output, b"")
                process.poll.return_value = 0
                with patch.object(deployment.subprocess, "Popen", return_value=process) as call:
                    report = deployment.execute_deployment(job)
                self.assertEqual(report["status"], "unknown")
                command = call.call_args.args[0]
                self.assertIn("StrictHostKeyChecking=yes", command)
                self.assertIn("BatchMode=yes", command)
                self.assertIn("UserKnownHostsFile=" + str(known), command)
                self.assertLess(command.index("--"), command.index(job["host"]))
                self.assertNotIn("shell", call.call_args.kwargs)
        finally:
            object.__setattr__(settings, "recamera_known_hosts", old_hosts)
            object.__setattr__(settings, "recamera_ssh_key", old_key)

    def test_shutdown_interrupts_local_ssh_but_keeps_remote_outcome_unknown(self):
        queued = self.queue().json()
        job = one("SELECT * FROM edge_deployments WHERE id=?", (queued["id"],))
        known = settings.data_dir / "known_hosts"
        known.write_text("fixture known-host entry\n")
        old_hosts, old_key = settings.recamera_known_hosts, settings.recamera_ssh_key
        object.__setattr__(settings, "recamera_known_hosts", str(known))
        object.__setattr__(settings, "recamera_ssh_key", "")
        process = Mock(returncode=None)
        process.communicate.side_effect = [subprocess.TimeoutExpired("ssh", 1), (b"", b"")]
        process.poll.return_value = None
        stop_event = threading.Event()
        stop_event.set()
        try:
            with patch.object(deployment.subprocess, "Popen", return_value=process):
                report = deployment.execute_deployment(job, stop_event)
            self.assertEqual(report["status"], "unknown")
            self.assertEqual(report["stage"], "connection_lost")
            process.kill.assert_called_once()
        finally:
            object.__setattr__(settings, "recamera_known_hosts", old_hosts)
            object.__setattr__(settings, "recamera_ssh_key", old_key)

    def test_ssh_pipe_failure_after_launch_is_unknown_instead_of_preparation_failed(self):
        queued = self.queue().json()
        job = one("SELECT * FROM edge_deployments WHERE id=?", (queued["id"],))
        known = settings.data_dir / "known_hosts"
        known.write_text("fixture known-host entry\n")
        old_hosts, old_key = settings.recamera_known_hosts, settings.recamera_ssh_key
        object.__setattr__(settings, "recamera_known_hosts", str(known))
        object.__setattr__(settings, "recamera_ssh_key", "")
        process = Mock(returncode=None)
        process.communicate.side_effect = [OSError("broken SSH pipe"), (b"", b"")]
        process.poll.return_value = None
        try:
            with patch.object(deployment.subprocess, "Popen", return_value=process):
                report = deployment.execute_deployment(job)
            self.assertEqual(report["status"], "unknown")
            process.kill.assert_called_once()
        finally:
            object.__setattr__(settings, "recamera_known_hosts", old_hosts)
            object.__setattr__(settings, "recamera_ssh_key", old_key)


if __name__ == "__main__":
    unittest.main()
