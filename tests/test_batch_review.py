from __future__ import annotations

import importlib
import io
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.auth import Actor, get_actor
from app.config import settings
from app.database import all_rows, db, one

main = importlib.import_module("app.main")


class BatchReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mdp-batch-review-")
        values = dict(data_dir=Path(self.temp.name), embedded_worker=False,
                      embedded_pipeline_worker=False, auth_mode="disabled", enable_demo_model=True)
        self.previous = {key: getattr(settings, key) for key in values}
        for key, value in values.items():
            object.__setattr__(settings, key, value)
        app = FastAPI(lifespan=main.lifespan)
        app.include_router(main.app.router)
        self.assertIn("/api/projects/{project_id}/review/batch", app.openapi()["paths"])
        self.actor = Actor("test-admin", "admin")
        app.dependency_overrides[get_actor] = lambda: self.actor
        self.client = TestClient(app).__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for key, value in self.previous.items():
            object.__setattr__(settings, key, value)
        self.temp.cleanup()

    def fixture(self, count=1, annotated=True):
        response = self.client.post("/api/projects", json={"name": "Batch review fixture", "task_type": "detect", "labels": ["object"]})
        self.assertEqual(response.status_code, 201, response.text)
        project = response.json()
        project = self.client.get(f"/api/projects/{project['id']}").json()
        assets = []
        for index in range(count):
            image = io.BytesIO()
            Image.new("RGB", (64, 64), (index + 10, 35, 75)).save(image, "PNG")
            result = self.client.post(f"/api/projects/{project['id']}/assets", files={"files": (f"image-{index}.png", image.getvalue(), "image/png")})
            self.assertEqual(result.status_code, 201, result.text)
            asset_id = result.json()["asset_ids"][0]
            if annotated:
                annotation = self.client.post(f"/api/assets/{asset_id}/annotations", json={"label_id": project["labels"][0]["id"], "shape_type": "rectangle", "points": [3, 3, 30, 30]})
                self.assertEqual(annotation.status_code, 201, annotation.text)
            assets.append(asset_id)
        return project, assets

    def targets(self, assets):
        return [{"asset_id": asset, "expected_revision": one("SELECT revision FROM assets WHERE id=?", (asset,))["revision"]} for asset in assets]

    def action(self, project, assets, action="submit", *, preview=False, reason="", targets=None):
        return self.client.post(f"/api/projects/{project['id']}/review/{'preview' if preview else 'batch'}",
                                json={"action": action, "assets": targets or self.targets(assets), "reason": reason})

    def status(self, assets, value):
        with db() as conn:
            conn.executemany("UPDATE assets SET review_status=? WHERE id=?", [(value, asset) for asset in assets])

    def test_preview_is_read_only_and_submit_releases_only_owned_lock(self):
        project, assets = self.fixture(2)
        future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        with db() as conn:
            conn.execute("UPDATE assets SET lock_owner=?,lock_expires_at=? WHERE id=?", (self.actor.id, future, assets[0]))
            conn.execute("UPDATE assets SET lock_owner='other',lock_expires_at=? WHERE id=?", (past, assets[1]))
        before = all_rows("SELECT * FROM assets ORDER BY id")
        logs = one("SELECT COUNT(*) AS n FROM audit_logs")["n"]
        preview = self.action(project, assets, preview=True)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["counts"]["eligible"], 2)
        self.assertEqual(all_rows("SELECT * FROM assets ORDER BY id"), before)
        self.assertEqual(one("SELECT COUNT(*) AS n FROM audit_logs")["n"], logs)
        result = self.action(project, assets).json()
        self.assertEqual(result["counts"]["succeeded"], 2)
        self.assertIsNone(one("SELECT lock_owner FROM assets WHERE id=?", (assets[0],))["lock_owner"])
        self.assertEqual(one("SELECT lock_owner FROM assets WHERE id=?", (assets[1],))["lock_owner"], "other")
        self.assertEqual(one("SELECT COUNT(*) AS n FROM review_events")["n"], 2)
        self.assertEqual(one("SELECT COUNT(*) AS n FROM audit_logs WHERE action='submit' AND resource_type='asset_review'")["n"], 2)

    def test_submit_only_annotated_editable_states(self):
        project, assets = self.fixture(5)
        self.status([assets[1]], "rejected")
        self.status([assets[2]], "submitted")
        self.status([assets[3]], "approved")
        with db() as conn:
            conn.execute("UPDATE annotations SET deleted_at='deleted' WHERE asset_id=?", (assets[4],))
        result = self.action(project, assets).json()
        self.assertEqual(result["counts"]["succeeded"], 2)
        self.assertEqual(result["counts"]["skipped"], 3)
        self.assertEqual(result["items"][4]["code"], "no_annotations")

    def test_approval_never_bypasses_submission_or_missing_annotations(self):
        project, assets = self.fixture(3)
        self.status(assets[:2], "submitted")
        with db() as conn:
            conn.execute("DELETE FROM annotations WHERE asset_id=?", (assets[1],))
        self.actor = Actor("test-reviewer", "reviewer")
        result = self.action(project, assets, "approve").json()
        self.assertEqual(result["counts"]["succeeded"], 1)
        self.assertEqual(result["counts"]["skipped"], 2)
        self.assertEqual(one("SELECT review_status FROM assets WHERE id=?", (assets[2],))["review_status"], "draft")

    def test_annotator_cannot_approve_or_reject_but_can_submit(self):
        project, assets = self.fixture(2)
        self.status([assets[0]], "submitted")
        self.actor = Actor("test-annotator", "annotator")
        for action in ("approve", "reject"):
            for preview in (True, False):
                result = self.action(project, [assets[0]], action, preview=preview, reason="边界需要调整").json()
                self.assertEqual(result["counts"]["forbidden"], 1)
        self.assertEqual(self.action(project, [assets[1]]).json()["counts"]["succeeded"], 1)
        self.assertEqual(one("SELECT review_status FROM assets WHERE id=?", (assets[0],))["review_status"], "submitted")

    def test_reject_requires_reason_and_records_feedback_atomically(self):
        project, assets = self.fixture()
        self.status(assets, "submitted")
        self.assertEqual(self.action(project, assets, "reject", reason="  ").status_code, 422)
        self.assertEqual(self.action(project, assets, "reject", preview=True).json()["counts"]["eligible"], 1)
        result = self.action(project, assets, "reject", reason="  请校正右侧边界  ").json()
        self.assertEqual(result["counts"]["succeeded"], 1)
        self.assertEqual(result["items"][0]["asset"]["review_reason"], "请校正右侧边界")
        event = one("SELECT * FROM review_events WHERE asset_id=?", (assets[0],))
        self.assertEqual((event["from_status"], event["to_status"], event["reason"]), ("submitted", "rejected", "请校正右侧边界"))

    def test_stale_revision_other_lock_and_ai_busy_are_individual_conflicts(self):
        project, assets = self.fixture(4)
        targets = self.targets(assets)
        targets[0]["expected_revision"] += 1
        future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        with db() as conn:
            conn.execute("UPDATE assets SET lock_owner='someone-else',lock_expires_at=? WHERE id=?", (future, assets[1]))
        job = self.client.post(f"/api/projects/{project['id']}/jobs", json={"model_id": "demo", "prompt": "object", "asset_ids": [assets[2]]})
        self.assertEqual(job.status_code, 202, job.text)
        result = self.action(project, assets, targets=targets).json()
        self.assertEqual(result["counts"]["conflict"], 3)
        self.assertEqual(result["counts"]["succeeded"], 1)
        self.assertEqual([item["code"] for item in result["items"][:3]], ["revision_changed", "locked", "busy"])
        self.assertEqual(one("SELECT lock_owner FROM assets WHERE id=?", (assets[1],))["lock_owner"], "someone-else")

    def test_cross_project_missing_and_duplicates_do_not_modify_unrelated_images(self):
        project, assets = self.fixture()
        other, other_assets = self.fixture()
        targets = self.targets(assets + other_assets)
        targets += [dict(targets[0]), {"asset_id": 999999, "expected_revision": 0}]
        result = self.action(project, assets, targets=targets).json()
        self.assertEqual((result["total"], result["duplicate_count"]), (3, 1))
        self.assertEqual(result["counts"]["succeeded"], 1)
        self.assertEqual(result["counts"]["not_found"], 2)
        self.assertNotIn("filename", result["items"][1])
        self.assertEqual(one("SELECT review_status FROM assets WHERE id=?", (other_assets[0],))["review_status"], "draft")

    def test_limits_and_revisions_are_validated_before_any_write(self):
        project, assets = self.fixture()
        target = self.targets(assets)[0]
        path = f"/api/projects/{project['id']}/review/batch"
        for bad in ([], [target] * 501, [{**target, "expected_revision": -1}], [{**target, "asset_id": True}], [{**target, "asset_id": 2**64}], [{**target, "expected_revision": 2**64}], [target, {**target, "expected_revision": 999}]):
            response = self.client.post(path, json={"action": "submit", "assets": bad})
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.client.post(path, json={"action": "reopen", "assets": [target]}).status_code, 422)
        self.assertEqual(self.client.post(f"/api/projects/999999/review/batch", json={"action": "submit", "assets": [target]}).status_code, 404)
        self.assertEqual(self.client.post(path.replace("batch", "preview"), json={"action": "submit", "assets": [target] * 500}).json()["duplicate_count"], 499)
        self.assertEqual(one("SELECT COUNT(*) AS n FROM review_events")["n"], 0)

    def test_change_after_preview_requires_fresh_review(self):
        project, assets = self.fixture()
        self.status(assets, "submitted")
        targets = self.targets(assets)
        self.assertEqual(self.action(project, assets, "approve", preview=True, targets=targets).json()["counts"]["eligible"], 1)
        with db() as conn:
            conn.execute("UPDATE assets SET revision=revision+1 WHERE id=?", (assets[0],))
        result = self.action(project, assets, "approve", targets=targets).json()
        self.assertEqual(result["counts"]["conflict"], 1)
        self.assertEqual(one("SELECT COUNT(*) AS n FROM review_events")["n"], 0)

    def test_concurrent_review_only_one_transition_is_written(self):
        project, assets = self.fixture()
        self.status(assets, "submitted")
        targets = self.targets(assets)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.action(project, assets, "approve", targets=targets).json(), range(2)))
        self.assertEqual(sum(result["counts"]["succeeded"] for result in results), 1)
        self.assertEqual(sum(result["counts"]["conflict"] for result in results), 1)
        self.assertEqual(one("SELECT COUNT(*) AS n FROM review_events")["n"], 1)
        self.assertEqual(one("SELECT COUNT(*) AS n FROM audit_logs WHERE action='approve'")["n"], 1)


if __name__ == "__main__":
    unittest.main()
