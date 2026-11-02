from __future__ import annotations

import hashlib
import importlib
import io
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from PIL import Image

from app import edge
from app.auth import Actor, get_actor
from app.config import settings
from app.database import all_rows, db, init_db, now_iso, one

main = importlib.import_module("app.main")


class EdgeIngestionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cvsprout-edge-test-")
        values = {"data_dir": Path(self.temp.name), "auth_mode": "disabled", "public_origin": ""}
        self.previous = {key: getattr(settings, key) for key in values}
        for key, value in values.items():
            object.__setattr__(settings, key, value)
        init_db()
        self.app = FastAPI()
        if "/api/edge/devices" not in main.app.openapi()["paths"]:
            self.app.include_router(edge.router)
        self.app.include_router(main.app.router)

        @asynccontextmanager
        async def isolated_lifespan(_):
            # Modern FastAPI merges the included router's lifespan as well.
            # These tests claim jobs explicitly; never start actual workers.
            yield

        self.app.router.lifespan_context = isolated_lifespan

        def actor(request: Request):
            return Actor("edge-test-admin", request.headers.get("x-test-role", "admin"))

        self.app.dependency_overrides[get_actor] = actor
        self.client = TestClient(self.app).__enter__()
        response = self.client.post("/api/projects", json={"name": "Device images", "task_type": "detect", "labels": ["object"]})
        self.assertEqual(response.status_code, 201, response.text)
        self.project_id = response.json()["id"]
        self.device = self.create()
        self.token = self.device["device_token"]

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for key, value in self.previous.items():
            object.__setattr__(settings, key, value)
        self.temp.cleanup()

    def create(self, **values):
        response = self.client.post("/api/edge/devices", json={"name": "reCamera A", "project_id": self.project_id, **values})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def image(self, color="green"):
        result = io.BytesIO()
        Image.new("RGB", (40, 30), color).save(result, "PNG")
        return result.getvalue()

    def capture(self, capture_id="capture-1", *, content=None, token=None, **fields):
        return self.client.post("/api/edge/v1/captures",
            headers={"Authorization": "Bearer " + (token or self.token)},
            data={"capture_id": capture_id, "captured_at": "2026-10-31T16:00:00+08:00", **fields},
            files={"file": ("scene.png", self.image() if content is None else content, "image/png")})

    def config(self, token=None):
        return self.client.get("/api/edge/v1/config", headers={"Authorization": "Bearer " + (token or self.token)})

    def listed(self):
        devices = self.client.get("/api/edge/devices")
        self.assertEqual(devices.status_code, 200, devices.text)
        return next(device for device in devices.json()["devices"] if device["id"] == self.device["id"])

    def send_heartbeat(self, **fields):
        return self.client.post("/api/edge/v1/heartbeat", json=fields,
                                headers={"Authorization": "Bearer " + self.token})

    def test_connection_requires_authenticated_contact_and_expires_at_server_boundary(self):
        self.assertEqual(self.listed()["connection"]["state"], "never_connected")
        self.assertEqual(self.config("invalid").status_code, 401)
        self.assertEqual(self.listed()["connection"]["state"], "never_connected")
        contacted = datetime(2100, 1, 1, tzinfo=timezone.utc)
        with patch.object(edge, "now_iso", return_value=contacted.isoformat()):
            self.assertEqual(self.config().json()["heartbeat_schema_version"], 2)
            connected = self.listed()
        self.assertEqual(connected["connection"]["state"], "online")
        self.assertEqual(connected["connection"]["age_seconds"], 0)
        self.assertEqual(connected["connection"]["last_contact_at"], contacted.isoformat())
        self.assertIsNone(connected["connection"]["last_heartbeat_at"])
        self.assertEqual(connected["ingestion"]["state"], "waiting_heartbeat")
        for seconds, expected in ((90, "online"), (90.001, "offline"), (-6, "offline")):
            with self.subTest(seconds=seconds), patch.object(edge, "now_iso", return_value=(contacted + timedelta(seconds=seconds)).isoformat()):
                result = self.listed()
            self.assertEqual(result["connection"]["state"], expected)
            self.assertEqual(result["connection"]["online"], expected == "online")
            self.assertEqual(result["connection"]["timeout_seconds"], 90)

    def test_heartbeat_separates_camera_upload_errors_and_receipt_progress(self):
        now = "2100-01-01T00:00:00+00:00"
        cases = [
            ({"state": "waiting_frames", "frames_received": 0}, "waiting_camera"),
            ({"state": "capturing", "frames_received": 7, "captured_images": 0}, "waiting_first_image"),
            ({"state": "capturing", "pending_images": 3}, "upload_pending"),
            ({"state": "queue_full", "pending_images": 500}, "queue_full"),
            ({"state": "session_limit", "session_complete": True}, "session_complete"),
            ({"state": "capture_error", "capture_error": "jpeg_encode_failed"}, "error"),
            ({"state": "capturing", "upload_error": "http_422", "pending_images": 3}, "error"),
            ({"state": "capture_stopped"}, "error"),
        ]
        for heartbeat, expected in cases:
            with self.subTest(state=heartbeat), patch.object(edge, "now_iso", return_value=now):
                response = self.send_heartbeat(**heartbeat)
                self.assertEqual(response.status_code, 200, response.text)
                result = self.listed()
            self.assertEqual(result["connection"]["state"], "online")
            self.assertEqual(result["connection"]["last_heartbeat_at"], now)
            self.assertTrue(result["ingestion"]["heartbeat_fresh"])
            self.assertEqual(result["ingestion"]["state"], expected)
        with patch.object(edge, "now_iso", return_value=now):
            self.assertEqual(self.capture().status_code, 201)
            self.assertEqual(self.send_heartbeat(state="capturing", frames_received=12, captured_images=1,
                                                 last_frame_age_seconds=0.5).status_code, 200)
            result = self.listed()
        self.assertEqual(result["ingestion"]["state"], "receiving")
        self.assertEqual(result["ingestion"]["captured_images"], 1)
        self.assertEqual(result["ingestion"]["last_frame_age_seconds"], 0.5)
        self.assertEqual(result["capture_count"], 1)

    def test_old_heartbeat_payload_still_works_without_invented_diagnostics(self):
        response = self.send_heartbeat(state="capturing", pending_images=0, uploaded_images=7, app_version="1.0.1")
        self.assertEqual(response.status_code, 200, response.text)
        result = self.listed()
        self.assertEqual(result["connection"]["state"], "online")
        self.assertEqual(result["ingestion"]["state"], "waiting_first_image")
        self.assertIsNone(result["ingestion"]["frames_received"])
        self.assertEqual(result["heartbeat"]["app_version"], "1.0.1")
        self.assertEqual(result["capture_count"], 0)
        self.assertNotIn("_received_at", result["heartbeat"])
        self.assertNotIn("_credential_confirmed", result["heartbeat"])

    def test_old_receipts_do_not_report_current_uploads_and_respect_capture_interval(self):
        received = datetime(2100, 1, 1, tzinfo=timezone.utc)
        with patch.object(edge, "now_iso", return_value=received.isoformat()):
            self.assertEqual(self.capture().status_code, 201)
        for interval, elapsed, expected in ((10, 90, "receiving"), (10, 91, "no_recent_images"),
                                            (3600, 7200, "receiving"), (3600, 10801, "no_recent_images")):
            with self.subTest(interval=interval, elapsed=elapsed), patch.object(edge, "now_iso", return_value=(received + timedelta(seconds=elapsed)).isoformat()):
                self.client.patch(f"/api/edge/devices/{self.device['id']}", json={"capture_interval_seconds": interval})
                self.assertEqual(self.send_heartbeat(state="capturing", frames_received=9, captured_images=0).status_code, 200)
                result = self.listed()
            self.assertEqual(result["connection"]["state"], "online")
            self.assertEqual(result["ingestion"]["state"], expected)
            self.assertEqual(result["ingestion"]["image_timeout_seconds"], max(90, interval * 3))
            self.assertEqual(result["capture_count"], 1)

    def test_rotation_requires_new_token_contact_and_new_heartbeat_even_at_same_timestamp(self):
        with patch.object(edge, "now_iso", return_value="2100-01-01T00:00:00+00:00"):
            self.assertEqual(self.send_heartbeat(state="capturing", pending_images=2).status_code, 200)
            rotated = self.client.post(f"/api/edge/devices/{self.device['id']}/rotate-token").json()
            self.assertEqual(rotated["device"]["connection"]["state"], "reconnect_required")
            self.assertFalse(rotated["device"]["ingestion"]["heartbeat_fresh"])
            self.assertEqual(self.config().status_code, 401)
            self.assertEqual(self.listed()["connection"]["state"], "reconnect_required")
            self.token = rotated["device_token"]
            self.assertEqual(self.config().status_code, 200)
            result = self.listed()
            self.assertEqual(result["connection"]["state"], "online")
            self.assertEqual(result["ingestion"]["state"], "waiting_heartbeat")
            self.assertIsNone(result["ingestion"]["pending_images"])
            self.assertEqual(self.send_heartbeat(state="capturing").status_code, 200)
            self.assertTrue(self.listed()["ingestion"]["heartbeat_fresh"])
            self.client.delete(f"/api/edge/devices/{self.device['id']}")
            self.assertEqual(self.listed()["connection"]["state"], "revoked")

    def test_rotation_before_first_contact_is_not_reported_as_never_configured(self):
        rotated = self.client.post(f"/api/edge/devices/{self.device['id']}/rotate-token").json()
        self.assertEqual(rotated["device"]["connection"]["state"], "reconnect_required")
        self.assertIsNone(rotated["device"]["connection"]["last_contact_at"])

    def test_stale_heartbeat_diagnostics_are_not_shown_as_current(self):
        at = datetime(2100, 1, 1, tzinfo=timezone.utc)
        with patch.object(edge, "now_iso", return_value=at.isoformat()):
            self.assertEqual(self.send_heartbeat(state="capture_error", last_error="jpeg_encode_failed", pending_images=3).status_code, 200)
        with patch.object(edge, "now_iso", return_value=(at + timedelta(seconds=100)).isoformat()):
            self.assertEqual(self.config().status_code, 200)
            result = self.listed()
        self.assertEqual(result["connection"]["state"], "online")
        self.assertEqual(result["connection"]["heartbeat_age_seconds"], 100)
        self.assertEqual(result["ingestion"]["state"], "waiting_heartbeat")
        self.assertFalse(result["ingestion"]["heartbeat_fresh"])
        self.assertEqual(result["ingestion"]["last_error"], "")
        self.assertIsNone(result["ingestion"]["pending_images"])

    def test_paused_and_archived_projects_remain_connected_but_explain_no_new_images(self):
        self.assertEqual(self.config().status_code, 200)
        self.client.patch(f"/api/edge/devices/{self.device['id']}", json={"capture_enabled": False})
        self.assertEqual(self.send_heartbeat(state="paused", pending_images=2, upload_error="http_413").status_code, 200)
        paused = self.listed()
        self.assertEqual(paused["connection"]["state"], "online")
        self.assertEqual(paused["ingestion"]["state"], "paused")
        self.assertEqual(paused["ingestion"]["last_error"], "http_413")
        self.assertEqual(paused["ingestion"]["pending_images"], 2)
        with db() as conn:
            conn.execute("UPDATE projects SET archived_at=? WHERE id=?", (now_iso(), self.project_id))
        archived = self.listed()
        self.assertEqual(archived["connection"]["state"], "online")
        self.assertEqual(archived["ingestion"]["state"], "archived")

    def test_malformed_legacy_heartbeat_and_timestamps_do_not_break_device_listing(self):
        for stored in ("[]", "not-json", '{"state":{},"_received_at":"future"}', '{"unknown_secret":"hidden"}'):
            with self.subTest(stored=stored), db() as conn:
                conn.execute("UPDATE edge_devices SET heartbeat_json=?,last_seen_at=? WHERE id=?", (stored, "bad-time", self.device["id"]))
            result = self.listed()
            self.assertEqual(result["connection"]["state"], "offline")
            self.assertIsNone(result["connection"]["last_contact_at"])
            self.assertIsNone(result["connection"]["age_seconds"])
            self.assertNotIn("hidden", json.dumps(result))
            self.assertEqual(result["ingestion"]["state"], "waiting_connection")

    def test_all_heartbeat_strings_are_redacted_and_new_fields_are_validated(self):
        self.assertEqual(self.send_heartbeat(state=self.token, app_version=self.token, firmware_version=self.token,
            sdk_version=self.token, model_release=self.token, capture_error=self.token,
            upload_error="Authorization: Bearer other-secret\nfailed", last_error=self.token).status_code, 200)
        text = json.dumps(self.listed())
        self.assertNotIn(self.token, text)
        self.assertNotIn("other-secret", text)
        stored = one("SELECT heartbeat_json FROM edge_devices WHERE id=?", (self.device["id"],))["heartbeat_json"]
        self.assertNotIn(self.token, stored)
        self.assertNotIn("other-secret", stored)
        for fields in ({"frames_received": -1}, {"frames_received": True}, {"captured_images": "2"},
                       {"last_frame_age_seconds": -1}, {"last_frame_age_seconds": True},
                       {"session_complete": "true"}, {"_received_at": "2100-01-01T00:00:00Z"}):
            with self.subTest(fields=fields):
                self.assertEqual(self.send_heartbeat(**fields).status_code, 422)

    def test_configuration_rechecks_credentials_inside_contact_transaction(self):
        authenticated = one("SELECT * FROM edge_devices WHERE id=?", (self.device["id"],))

        def revoke_after_authentication():
            with db() as conn:
                conn.execute("UPDATE edge_devices SET enabled=0,token_hash=NULL WHERE id=?", (self.device["id"],))
            return authenticated

        self.app.dependency_overrides[edge.get_device] = revoke_after_authentication
        self.assertEqual(self.config().status_code, 401)
        self.assertIsNone(one("SELECT last_seen_at FROM edge_devices WHERE id=?", (self.device["id"],))["last_seen_at"])

    def test_retry_refreshes_contact_without_inventing_new_image_receipt(self):
        with patch.object(edge, "now_iso", return_value="2100-01-01T00:00:00+00:00"):
            receipt = self.capture().json()
        with patch.object(edge, "now_iso", return_value="2100-01-01T00:02:00+00:00"):
            self.assertEqual(self.capture().json(), receipt)
            result = self.listed()
        self.assertEqual(result["connection"]["state"], "online")
        self.assertEqual(result["connection"]["last_contact_at"], "2100-01-01T00:02:00+00:00")
        self.assertEqual(result["last_capture_at"], "2100-01-01T00:00:00+00:00")
        self.assertEqual(result["capture_count"], 1)

    def test_one_time_credentials_hash_and_project_scoped_configuration(self):
        self.assertEqual(self.device["server_url"], "http://testserver")
        saved = one("SELECT * FROM edge_devices WHERE id=?", (self.device["id"],))
        self.assertEqual(saved["token_hash"], hashlib.sha256(self.token.encode()).hexdigest())
        self.assertNotIn(self.token, json.dumps(saved))
        devices = self.client.get("/api/edge/devices").json()["devices"]
        self.assertNotIn("device_token", devices[0])
        self.assertNotIn("token_hash", devices[0])
        config = self.config().json()
        self.assertEqual(config["project_id"], self.project_id)
        self.assertTrue(config["capture_enabled"])
        self.assertEqual(config["max_image_bytes"], 10 * 1024 * 1024)
        self.assertEqual(self.config("invalid").status_code, 401)
        self.assertEqual(self.client.get("/api/edge/v1/config").status_code, 401)
        self.assertEqual(self.client.patch(f"/api/edge/devices/{self.device['id']}", json={"project_id": 99}).status_code, 422)

    def test_same_capture_retry_and_same_content_different_events(self):
        first = self.capture(batch_id="day1", model_release="release-2")
        self.assertEqual(first.status_code, 201, first.text)
        self.assertTrue(first.json()["received"])
        self.assertFalse(first.json()["duplicate"])
        self.assertEqual(self.capture().json(), first.json())
        other = self.capture("capture-2")
        self.assertEqual(other.json()["asset_id"], first.json()["asset_id"])
        self.assertTrue(other.json()["duplicate"])
        self.assertEqual(one("SELECT COUNT(*) AS n FROM assets")["n"], 1)
        self.assertEqual(one("SELECT COUNT(*) AS n FROM edge_capture_events")["n"], 2)
        self.assertEqual(len(list(settings.media_dir.iterdir())), 1)
        listed = self.client.get(f"/api/edge/devices/{self.device['id']}/captures?limit=1&offset=1").json()
        self.assertEqual(listed["total"], 2)
        self.assertEqual(listed["items"][0]["batch_id"], "day1")
        self.assertEqual(listed["items"][0]["model_release"], "release-2")
        self.assertEqual(listed["items"][0]["captured_at"], "2026-10-31T08:00:00+00:00")
        device = self.client.get("/api/edge/devices").json()["devices"][0]
        self.assertEqual((device["capture_count"], device["asset_count"]), (2, 1))

    def test_reused_id_with_different_content_conflicts_and_cleans_staging(self):
        original = self.capture().json()
        response = self.capture(content=self.image("red"))
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(one("SELECT COUNT(*) AS n FROM assets")["n"], 1)
        self.assertEqual(len(list(settings.media_dir.iterdir())), 1)
        self.assertEqual(self.capture().json()["asset_id"], original["asset_id"])

    def test_devices_share_content_but_keep_distinct_capture_provenance(self):
        first = self.capture().json()
        device2 = self.create(name="reCamera B")
        second = self.capture(token=device2["device_token"])
        self.assertEqual(second.status_code, 201, second.text)
        self.assertEqual(second.json()["asset_id"], first["asset_id"])
        self.assertEqual(one("SELECT COUNT(*) AS n FROM edge_capture_events")["n"], 2)
        self.assertEqual(one("SELECT COUNT(DISTINCT device_id) AS n FROM edge_capture_events")["n"], 2)

    def test_manual_upload_dedup_and_existing_work_block_membership_unchanged(self):
        upload = self.client.post(f"/api/projects/{self.project_id}/assets", files={"files": ("manual.png", self.image(), "image/png")})
        self.assertEqual(upload.status_code, 201, upload.text)
        original_id = upload.json()["asset_ids"][0]
        blocks = self.client.post(f"/api/projects/{self.project_id}/work-blocks", json={"block_size": 1})
        self.assertEqual(blocks.status_code, 201, blocks.text)
        claimed = self.client.post(f"/api/projects/{self.project_id}/work-blocks/claim", json={})
        self.assertEqual(claimed.status_code, 200, claimed.text)
        before = all_rows("SELECT * FROM work_block_assets")
        duplicate = self.capture().json()
        self.assertEqual(duplicate["asset_id"], original_id)
        self.assertTrue(duplicate["duplicate"])
        new = self.capture("new-image", content=self.image("red")).json()["asset_id"]
        self.assertEqual(all_rows("SELECT * FROM work_block_assets"), before)
        self.assertIsNone(one("SELECT * FROM work_block_assets WHERE asset_id=?", (new,)))
        manual_again = self.client.post(f"/api/projects/{self.project_id}/assets", files={"files": ("duplicate.png", self.image("red"), "image/png")})
        self.assertEqual(manual_again.json()["duplicate_count"], 1)

    def test_pause_stops_sampling_but_drains_queue_and_archive_blocks_import(self):
        updated = self.client.patch(f"/api/edge/devices/{self.device['id']}", json={"capture_enabled": False, "capture_interval_seconds": 30})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertFalse(self.config().json()["capture_enabled"])
        self.assertEqual(self.config().json()["capture_interval_seconds"], 30)
        self.assertEqual(self.capture().status_code, 201)
        with db() as conn:
            conn.execute("UPDATE projects SET archived_at=? WHERE id=?", (now_iso(), self.project_id))
        self.assertTrue(self.config().json()["project_archived"])
        self.assertEqual(self.capture("archived").status_code, 409)
        response = self.client.post("/api/edge/devices", json={"name": "archived", "project_id": self.project_id})
        self.assertEqual(response.status_code, 409)

    def test_rotate_and_revoke_immediately_disable_old_credentials(self):
        self.assertEqual(self.capture().status_code, 201)
        rotated = self.client.post(f"/api/edge/devices/{self.device['id']}/rotate-token")
        self.assertEqual(rotated.status_code, 200, rotated.text)
        new_token = rotated.json()["device_token"]
        self.assertNotEqual(new_token, self.token)
        self.assertEqual(self.config().status_code, 401)
        self.assertEqual(self.capture("stale").status_code, 401)
        self.assertEqual(self.config(new_token).status_code, 200)
        revoked = self.client.delete(f"/api/edge/devices/{self.device['id']}")
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(self.config(new_token).status_code, 401)
        self.assertEqual(one("SELECT COUNT(*) AS n FROM edge_capture_events")["n"], 1)
        self.assertEqual(self.client.post(f"/api/edge/devices/{self.device['id']}/rotate-token").status_code, 409)
        audit_text = json.dumps(all_rows("SELECT * FROM audit_logs"))
        self.assertNotIn(self.token, audit_text)
        self.assertNotIn(new_token, audit_text)

    def test_revocation_racing_upload_cannot_commit(self):
        original_store = edge._store_image

        def revoke_during_store(*args):
            image = original_store(*args)
            with db() as conn:
                conn.execute("UPDATE edge_devices SET enabled=0,token_hash=NULL WHERE id=?", (self.device["id"],))
            return image

        with patch.object(edge, "_store_image", side_effect=revoke_during_store):
            response = self.capture()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(one("SELECT COUNT(*) AS n FROM assets")["n"], 0)
        self.assertEqual(list(settings.media_dir.iterdir()), [])

    def test_heartbeat_and_admin_only_management(self):
        heartbeat = {"state": "capturing", "pending_images": 7, "pending_bytes": 5000,
                     "uploaded_images": 12, "app_version": "1.0.0", "last_error": "token " + self.token}
        result = self.client.post("/api/edge/v1/heartbeat", json=heartbeat, headers={"Authorization": "Bearer " + self.token})
        self.assertEqual(result.status_code, 200, result.text)
        device = self.client.get("/api/edge/devices").json()["devices"][0]
        self.assertEqual(device["heartbeat"]["pending_images"], 7)
        self.assertTrue(device["last_seen_at"])
        self.assertNotIn(self.token, json.dumps(device))
        for role in ("annotator", "reviewer"):
            headers = {"x-test-role": role}
            for method, url, payload in [
                ("GET", "/api/edge/devices", None),
                ("POST", "/api/edge/devices", {"name": "bad", "project_id": self.project_id}),
                ("PATCH", f"/api/edge/devices/{self.device['id']}", {"capture_enabled": False}),
                ("POST", f"/api/edge/devices/{self.device['id']}/rotate-token", None),
                ("DELETE", f"/api/edge/devices/{self.device['id']}", None),
                ("GET", f"/api/edge/devices/{self.device['id']}/captures", None),
            ]:
                response = self.client.request(method, url, headers=headers, **({"json": payload} if payload else {}))
                self.assertEqual(response.status_code, 403, response.text)

    def test_device_token_cannot_be_used_for_human_api_even_in_disabled_auth_mode(self):
        self.app.dependency_overrides.pop(get_actor)
        for mode in ("disabled", "accounts"):
            object.__setattr__(settings, "auth_mode", mode)
            for path in ("/api/edge/devices", "/api/projects"):
                for prefix in ("Bearer ", "Bearer   "):
                    response = self.client.get(path, headers={"Authorization": prefix + self.token})
                    self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(self.config().status_code, 200)

    def test_invalid_images_fields_config_and_oversize_leave_no_rows_or_files(self):
        for fields, content, status in [
            ({}, b"not an image", 415),
            ({"captured_at": "2026-11-01"}, self.image(), 422),
            ({"batch_id": "x" * 129}, self.image(), 422),
        ]:
            response = self.capture(content=content, **fields)
            self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(self.capture("a/b").status_code, 422)
        with patch.object(edge, "MAX_CAPTURE_BYTES", 10):
            self.assertEqual(self.capture().status_code, 413)
        for payload in ({"name": "  "}, {"capture_interval_seconds": 0}, {"capture_interval_seconds": True},
                        {"capture_enabled": None}, {"max_pending_images": 0},
                        {"server_url": "http://user:password@example.com"}, {"server_url": "file:///etc/passwd"}):
            self.assertEqual(self.client.patch(f"/api/edge/devices/{self.device['id']}", json=payload).status_code, 422)
        self.assertEqual(one("SELECT COUNT(*) AS n FROM edge_capture_events")["n"], 0)
        self.assertEqual(list(settings.media_dir.iterdir()), [])

    def test_concurrent_retries_commit_exactly_one_asset_and_event(self):
        barrier = threading.Barrier(3)

        def upload(_):
            barrier.wait(timeout=10)
            return self.capture()

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(upload, range(3)))
        self.assertEqual([result.status_code for result in results], [201, 201, 201])
        self.assertEqual(len({result.json()["asset_id"] for result in results}), 1)
        self.assertEqual(one("SELECT COUNT(*) AS n FROM edge_capture_events")["n"], 1)
        self.assertEqual(len(list(settings.media_dir.iterdir())), 1)

    def test_failed_receipt_transaction_rolls_back_asset_and_cleans_file(self):
        with db() as conn:
            conn.execute("CREATE TRIGGER reject_edge_receipt BEFORE INSERT ON edge_capture_events BEGIN SELECT RAISE(FAIL, 'receipt test failure'); END")
        with self.assertRaisesRegex(Exception, "receipt test failure"):
            self.capture()
        self.assertEqual(one("SELECT COUNT(*) AS n FROM assets")["n"], 0)
        self.assertEqual(list(settings.media_dir.iterdir()), [])

    def test_project_deletion_removes_devices_receipts_and_media(self):
        self.assertEqual(self.capture().status_code, 201)
        response = self.client.request("DELETE", f"/api/projects/{self.project_id}", json={"confirmation_name": "Device images"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["deleted"])
        for table in ("edge_devices", "edge_capture_events", "assets"):
            self.assertEqual(one(f"SELECT COUNT(*) AS n FROM {table}")["n"], 0)
        self.assertEqual(self.config().status_code, 401)
        self.assertEqual(list(settings.media_dir.iterdir()), [])

    def test_replay_after_asset_deletion_is_a_tombstone_and_migration_is_repeatable(self):
        capture = self.capture().json()
        with db() as conn:
            conn.execute("DELETE FROM assets WHERE id=?", (capture["asset_id"],))
        self.assertEqual(self.capture().status_code, 410)
        self.assertEqual(one("SELECT COUNT(*) AS n FROM assets")["n"], 0)
        init_db()
        init_db()
        self.assertEqual(self.config().status_code, 200)
        self.assertEqual(one("SELECT COUNT(*) AS n FROM edge_capture_events")["n"], 1)
        with db() as conn:
            self.assertEqual(list(conn.execute("PRAGMA foreign_key_check")), [])


if __name__ == "__main__":
    unittest.main()
