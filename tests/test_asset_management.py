from __future__ import annotations

import importlib
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image


class AssetManagementTest(unittest.TestCase):
    def setUp(self):
        # Do not instantiate shared settings while test discovery is importing
        # modules: the legacy API suite first establishes its isolated data dir.
        global Actor, settings, all_rows, db, one, main
        from app.auth import Actor, get_actor
        from app.config import settings
        from app.database import all_rows, db, one
        from app.asset_management import router
        main = importlib.import_module("app.main")
        self.temp = tempfile.TemporaryDirectory(prefix="mdp-asset-delete-")
        values = dict(data_dir=Path(self.temp.name), embedded_worker=False,
                      embedded_pipeline_worker=False, enable_demo_model=True, auth_mode="disabled")
        self.previous = {key: getattr(settings, key) for key in values}
        for key, value in values.items():
            object.__setattr__(settings, key, value)
        app = FastAPI(lifespan=main.lifespan)
        app.include_router(main.app.router)
        if not any(route.path == "/api/projects/{project_id}/assets/delete-batch" for route in app.routes):
            app.include_router(router)
        self.actor = Actor("delete-test-admin", "admin")
        app.dependency_overrides[get_actor] = lambda: self.actor
        self.client = TestClient(app, headers={"X-MDP-Client": "browser"}).__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for key, value in self.previous.items():
            object.__setattr__(settings, key, value)
        self.temp.cleanup()

    def fixture(self, count=1):
        result = self.client.post("/api/projects", json={"name": "Delete fixture", "task_type": "detect", "labels": ["object"]})
        self.assertEqual(result.status_code, 201, result.text)
        project = self.client.get(f"/api/projects/{result.json()['id']}").json()
        assets = []
        for index in range(count):
            content = io.BytesIO()
            Image.new("RGB", (64, 64), (index + 10, 35, 75)).save(content, "PNG")
            result = self.client.post(f"/api/projects/{project['id']}/assets",
                                      files={"files": (f"image-{index}.png", content.getvalue(), "image/png")})
            self.assertEqual(result.status_code, 201, result.text)
            asset = result.json()["asset_ids"][0]
            result = self.client.post(f"/api/assets/{asset}/annotations", json={
                "label_id": project["labels"][0]["id"], "shape_type": "rectangle", "points": [3, 3, 30, 30]})
            self.assertEqual(result.status_code, 201, result.text)
            assets.append(asset)
        return project, assets

    def targets(self, assets):
        return [{"asset_id": asset, "expected_revision": one("SELECT revision FROM assets WHERE id=?", (asset,))["revision"]}
                for asset in assets]

    def delete(self, project, assets=None, *, preview=False, targets=None):
        return self.client.post(f"/api/projects/{project['id']}/assets/delete-{'preview' if preview else 'batch'}",
                                json={"assets": targets if targets is not None else self.targets(assets)})

    def test_preview_is_read_only_and_reports_annotations_versions_and_review_history(self):
        project, assets = self.fixture()
        version = self.client.post(f"/api/projects/{project['id']}/versions", json={"approved_only": False})
        self.assertEqual(version.status_code, 201, version.text)
        submitted = self.client.post(f"/api/assets/{assets[0]}/workflow", json={
            "action": "submit", "expected_revision": self.targets(assets)[0]["expected_revision"]})
        self.assertEqual(submitted.status_code, 200, submitted.text)
        before = all_rows("SELECT * FROM assets")
        logs = all_rows("SELECT * FROM audit_logs")
        files = sorted(path.name for path in settings.media_dir.iterdir())
        preview = self.delete(project, assets, preview=True)
        self.assertEqual(preview.status_code, 200, preview.text)
        item = preview.json()["items"][0]
        self.assertEqual((item["status"], item["annotation_count"], item["review_event_count"], item["version_count"]), ("eligible", 1, 1, 1))
        self.assertEqual(item["versions"][0]["id"], version.json()["id"])
        self.assertTrue(item["versions_preserved"])
        self.assertEqual(all_rows("SELECT * FROM assets"), before)
        self.assertEqual(all_rows("SELECT * FROM audit_logs"), logs)
        self.assertEqual(sorted(path.name for path in settings.media_dir.iterdir()), files)

    def test_single_delete_cascades_current_data_preserves_audit_and_allows_reimport(self):
        project, assets = self.fixture()
        asset = one("SELECT * FROM assets WHERE id=?", (assets[0],))
        source = settings.media_dir / asset["storage_name"]
        content = source.read_bytes()
        existing_logs = {row["id"] for row in all_rows("SELECT id FROM audit_logs")}
        result = self.delete(project, assets)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["counts"]["succeeded"], 1)
        self.assertEqual(result.json()["items"][0]["media_cleanup"]["status"], "deleted")
        self.assertFalse(source.exists())
        self.assertEqual(self.client.get(f"/api/projects/{project['id']}/assets").json(), [])
        self.assertEqual(self.client.get(f"/api/assets/{assets[0]}/image").status_code, 404)
        self.assertEqual(one("SELECT COUNT(*) AS n FROM annotations")["n"], 0)
        self.assertEqual(self.client.get(f"/api/projects/{project['id']}/export/json").status_code, 409)
        self.assertTrue(existing_logs.issubset({row["id"] for row in all_rows("SELECT id FROM audit_logs")}))
        audit = one("SELECT * FROM audit_logs WHERE action='delete' AND resource_type='asset'")
        details = json.loads(audit["payload_json"])
        self.assertEqual((details["annotation_count"], details["sha256"], details["media_cleanup"]["status"]), (1, asset["sha256"], "deleted"))
        reimport = self.client.post(f"/api/projects/{project['id']}/assets", files={"files": (asset["filename"], content, "image/png")})
        self.assertEqual(reimport.status_code, 201, reimport.text)
        self.assertNotEqual(reimport.json()["asset_ids"][0], assets[0])
        self.assertEqual(one("SELECT COUNT(*) AS n FROM assets")["n"], 1)

    def test_augmented_copies_count_as_one_preserved_version(self):
        project, assets = self.fixture()
        version = self.client.post(f"/api/projects/{project['id']}/versions", json={
            "approved_only": False, "train_ratio": 1, "valid_ratio": 0, "test_ratio": 0,
            "augmentation": {"copies_per_image": 3, "operations": [{"type": "noise"}]},
        })
        self.assertEqual(version.status_code, 201, version.text)
        self.assertEqual(version.json()["asset_count"], 4)
        preview = self.delete(project, assets, preview=True).json()["items"][0]
        self.assertEqual(preview["version_count"], 1)
        self.assertEqual([row["id"] for row in preview["versions"]], [version.json()["id"]])
        self.assertFalse(preview["versions_truncated"])
        deleted = self.delete(project, assets).json()["items"][0]
        self.assertEqual(deleted["version_count"], 1)
        self.assertEqual(self.client.get(f"/api/versions/{version.json()['id']}").json()["asset_count"], 4)

    def test_immutable_version_and_shared_thumbnail_survive_current_delete(self):
        project, assets = self.fixture(2)
        other, other_assets = self.fixture()
        version = self.client.post(f"/api/projects/{project['id']}/versions", json={"approved_only": False}).json()
        snapshot = one("SELECT * FROM dataset_version_assets WHERE version_id=? AND original_asset_id=?", (version["id"], assets[0]))
        copied_file = settings.versions_dir / f"version-{version['id']}" / snapshot["storage_name"]
        copied_bytes = copied_file.read_bytes()
        # Training and conversion artifacts refer to the immutable version, not
        # to the editable assets table. No worker/model execution is needed.
        artifact = settings.training_dir / "preserved-fixture.pt"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"test-artifact")
        with db() as conn:
            run = conn.execute("""INSERT INTO training_runs
                (project_id,version_id,name,model_arch,status,total_epochs,artifact_path,created_by,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""", (project["id"], version["id"], "kept", "fixture.pt", "completed", 1,
                str(artifact), self.actor.id, "2026-01-01")).lastrowid
            conn.execute("""INSERT INTO model_conversions (training_run_id,format,status,created_by,created_at)
                VALUES (?,'onnx','queued',?,'2026-01-01')""", (run, self.actor.id))
        self.assertEqual(self.client.get(f"/api/assets/{assets[0]}/thumbnail").status_code, 200)
        thumbnails = list(settings.thumbnail_dir.glob("*.webp"))
        self.assertTrue(thumbnails)
        self.assertEqual(self.delete(project, [assets[0]]).json()["counts"]["succeeded"], 1)
        self.assertEqual(copied_file.read_bytes(), copied_bytes)
        self.assertEqual(artifact.read_bytes(), b"test-artifact")
        self.assertIsNotNone(one("SELECT id FROM training_runs WHERE id=?", (run,)))
        self.assertIsNotNone(one("SELECT id FROM model_conversions WHERE training_run_id=?", (run,)))
        self.assertTrue(all(path.exists() for path in thumbnails))
        self.assertEqual(self.client.get(f"/api/assets/{other_assets[0]}/image").status_code, 200)
        for format in ("json", "coco", "yolo"):
            frozen = self.client.get(f"/api/versions/{version['id']}/export/{format}")
            current = self.client.get(f"/api/projects/{project['id']}/export/{format}")
            self.assertEqual(frozen.status_code, 200, frozen.text[:200])
            self.assertEqual(current.status_code, 200, current.text[:200])
            with zipfile.ZipFile(io.BytesIO(frozen.content)) as archive:
                ids = [row["original_asset_id"] for row in json.loads(archive.read("manifest.json"))["assets"]]
                self.assertIn(assets[0], ids)
                self.assertEqual(len([name for name in archive.namelist() if name.endswith(".png")]), 2)
            with zipfile.ZipFile(io.BytesIO(current.content)) as archive:
                ids = [row.get("original_asset_id", row.get("id")) for row in json.loads(archive.read("manifest.json"))["assets"]]
                self.assertEqual(ids, [assets[1]])
                self.assertEqual(len([name for name in archive.namelist() if name.endswith(".png")]), 1)

    def test_admin_required_and_current_session_is_rechecked_inside_transaction(self):
        project, assets = self.fixture()
        for role in ("annotator", "reviewer"):
            self.actor = Actor("someone", role)
            for preview in (True, False):
                self.assertEqual(self.delete(project, assets, preview=preview).status_code, 403)
        self.actor = Actor("expired-admin", "admin", user_id=987654, session_id="expired")
        result = self.delete(project, assets).json()
        self.assertEqual(result["counts"]["forbidden"], 1)
        self.assertEqual(result["items"][0]["code"], "permission_changed")
        self.assertIsNotNone(one("SELECT id FROM assets WHERE id=?", (assets[0],)))

    def test_partial_batch_rechecks_revisions_locks_ai_scope_and_project(self):
        project, assets = self.fixture(5)
        other, other_assets = self.fixture()
        targets = self.targets(assets + other_assets)
        targets[0]["expected_revision"] += 1
        future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        with db() as conn:
            conn.execute("UPDATE assets SET lock_owner='other',lock_expires_at=? WHERE id=?", (future, assets[1]))
            conn.execute("UPDATE assets SET lock_owner=?,lock_expires_at=? WHERE id=?", (self.actor.id, future, assets[3]))
            conn.execute("UPDATE assets SET review_status='approved' WHERE id=?", (assets[4],))
        job = self.client.post(f"/api/projects/{project['id']}/jobs", json={"model_id": "demo", "prompt": "object", "asset_ids": [assets[2]]})
        self.assertEqual(job.status_code, 202, job.text)
        targets += [targets[3], {"asset_id": 999999, "expected_revision": 0}]
        result = self.delete(project, targets=targets).json()
        self.assertEqual((result["counts"]["conflict"], result["counts"]["succeeded"], result["counts"]["not_found"], result["duplicate_count"]), (3, 2, 2, 1))
        self.assertEqual([item["code"] for item in result["items"][:3]], ["revision_changed", "locked", "busy"])
        self.assertNotIn("filename", result["items"][5])
        self.assertIsNotNone(one("SELECT id FROM assets WHERE id=?", (other_assets[0],)))

    def test_legacy_project_wide_running_job_blocks_delete_and_retry_skips_deleted_ids(self):
        project, assets = self.fixture(2)
        result = self.client.post(f"/api/projects/{project['id']}/jobs", json={"model_id": "demo", "prompt": "object", "asset_ids": assets})
        self.assertEqual(result.status_code, 202, result.text)
        job = result.json()
        with db() as conn:
            conn.execute("UPDATE jobs SET status='running',settings_json='{}' WHERE id=?", (job["id"],))
        self.assertEqual(self.delete(project, assets).json()["counts"]["conflict"], 2)
        with db() as conn:
            conn.execute("UPDATE jobs SET status='failed',settings_json=? WHERE id=?", (json.dumps({"asset_ids": assets}), job["id"]))
        self.assertEqual(self.delete(project, [assets[0]]).json()["counts"]["succeeded"], 1)
        retry = self.client.post(f"/api/jobs/{job['id']}/retry")
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(json.loads(one("SELECT settings_json FROM jobs WHERE id=?", (job["id"],))["settings_json"])["asset_ids"], [assets[1]])
        with db() as conn:
            conn.execute("UPDATE jobs SET status='failed' WHERE id=?", (job["id"],))
        self.assertEqual(self.delete(project, [assets[1]]).json()["counts"]["succeeded"], 1)
        self.assertEqual(self.client.post(f"/api/jobs/{job['id']}/retry").status_code, 409)

    def test_concurrent_same_revision_deletes_once_and_retry_is_safe(self):
        project, assets = self.fixture()
        targets = self.targets(assets)
        self.assertEqual(self.delete(project, preview=True, targets=targets).json()["counts"]["eligible"], 1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.delete(project, targets=targets).json(), range(2)))
        self.assertEqual(sum(result["counts"]["succeeded"] for result in results), 1)
        self.assertEqual(sum(result["counts"]["not_found"] for result in results), 1)
        self.assertEqual(one("SELECT COUNT(*) AS n FROM audit_logs WHERE action='delete' AND resource_type='asset'")["n"], 1)
        self.assertEqual(self.delete(project, targets=targets).json()["counts"]["not_found"], 1)

    def test_change_after_preview_requires_fresh_revision_and_validation_writes_nothing(self):
        project, assets = self.fixture()
        target = self.targets(assets)[0]
        self.assertEqual(self.delete(project, targets=[target], preview=True).json()["counts"]["eligible"], 1)
        with db() as conn:
            conn.execute("UPDATE assets SET revision=revision+1 WHERE id=?", (assets[0],))
        self.assertEqual(self.delete(project, targets=[target]).json()["items"][0]["code"], "revision_changed")
        for bad in ([], [target] * 501, [{**target, "asset_id": True}], [{**target, "expected_revision": -1}],
                    [{**target, "asset_id": 2**64}], [target, {**target, "expected_revision": 999}]):
            self.assertEqual(self.delete(project, targets=bad).status_code, 422)
        self.assertEqual(self.delete({"id": 999999}, targets=[target]).status_code, 404)
        self.assertEqual(one("SELECT COUNT(*) AS n FROM audit_logs WHERE action='delete' AND resource_type='asset'")["n"], 0)

    def test_filesystem_failure_never_rolls_back_a_committed_deletion_or_removes_other_files(self):
        project, assets = self.fixture(2)
        source = settings.media_dir / one("SELECT storage_name FROM assets WHERE id=?", (assets[0],))["storage_name"]
        unlink = Path.unlink
        def fail_one(path, *args, **kwargs):
            if path == source:
                raise PermissionError("test-only denied")
            return unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail_one):
            result = self.delete(project, assets).json()
        self.assertEqual((result["counts"]["succeeded"], result["cleanup_pending_count"]), (2, 1))
        self.assertTrue(source.exists())
        self.assertEqual(one("SELECT COUNT(*) AS n FROM assets")["n"], 0)
        audit = one("SELECT payload_json FROM audit_logs WHERE action='delete' AND resource_id=?", (str(assets[0]),))
        self.assertEqual(json.loads(audit["payload_json"])["media_cleanup"]["status"], "pending")

    def test_database_rollback_keeps_source_annotations_and_audit_consistent(self):
        project, assets = self.fixture()
        source = settings.media_dir / one("SELECT storage_name FROM assets WHERE id=?", (assets[0],))["storage_name"]
        with db() as conn:
            conn.execute("""CREATE TRIGGER abort_test_delete BEFORE DELETE ON assets
                BEGIN SELECT RAISE(ABORT, 'test transaction failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.delete(project, assets)
        self.assertTrue(source.is_file())
        self.assertIsNotNone(one("SELECT id FROM assets WHERE id=?", (assets[0],)))
        self.assertEqual(one("SELECT COUNT(*) AS n FROM annotations")["n"], 1)
        self.assertEqual(one("SELECT COUNT(*) AS n FROM audit_logs WHERE action='delete' AND resource_type='asset'")["n"], 0)

    def test_missing_file_can_be_removed_but_unsafe_storage_is_rejected(self):
        project, assets = self.fixture(3)
        row = one("SELECT * FROM assets WHERE id=?", (assets[0],))
        (settings.media_dir / row["storage_name"]).unlink()
        outside = Path(self.temp.name) / "do-not-delete.png"
        outside.write_bytes(b"outside-media")
        link = settings.media_dir / "unsafe-link.png"
        link.symlink_to(outside)
        with db() as conn:
            conn.execute("UPDATE assets SET storage_name='../do-not-delete.png' WHERE id=?", (assets[1],))
            conn.execute("UPDATE assets SET storage_name='unsafe-link.png' WHERE id=?", (assets[2],))
        result = self.delete(project, assets).json()
        self.assertEqual(result["counts"]["succeeded"], 1)
        self.assertEqual(result["items"][0]["media_cleanup"]["status"], "missing")
        self.assertEqual(result["counts"]["conflict"], 2)
        self.assertEqual(outside.read_bytes(), b"outside-media")
        self.assertTrue(link.is_symlink())


if __name__ == "__main__":
    unittest.main()
