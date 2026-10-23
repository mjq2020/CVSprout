from __future__ import annotations

import asyncio
import importlib.util
import json
import unittest

from fastapi import Request

from tests import test_sam3_detection as sam3_fixture


@unittest.skipUnless(importlib.util.find_spec("torch") and importlib.util.find_spec("cv2"),
                     "requires CPU model dependencies")
class OwnerSam3AiTest(unittest.TestCase):
    """Use real SAM3 geometry, API and worker with isolated member identities.

    Reuse the tensor/checkpoint fixture, without inheriting its tests. No model
    downloads or production database are involved; only the SAM loader is fake.
    """

    fixture = sam3_fixture.Sam3DetectionApiTest.fixture
    snapshot = sam3_fixture.Sam3DetectionApiTest.snapshot
    tearDown = sam3_fixture.Sam3DetectionApiTest.tearDown

    def setUp(self):
        from app.auth import Actor, get_actor
        from app.database import now_iso

        sam3_fixture.Sam3DetectionApiTest.setUp(self)
        self.users = {}
        with self.db() as conn:
            for name, role in (("admin", "admin"), ("alice", "annotator"),
                               ("bob", "annotator"), ("review", "reviewer")):
                user_id = conn.execute(
                    "INSERT INTO users(username,display_name,password_hash,role,created_at) VALUES (?,?,?,?,?)",
                    (name, name, "unused-test-hash", role, now_iso()),
                ).lastrowid
                self.users[name] = {"id": user_id, "role": role}

        def actor(request: Request):
            name = request.headers.get("x-test-user", "admin")
            user = self.users[name]
            session = request.headers.get("x-test-session", "first")
            return Actor(f"user:{name}:{session}", user["role"], name, user["id"], name)

        self.client.app.dependency_overrides[get_actor] = actor

    def api(self, method, path, *, user="admin", session="first", **kwargs):
        return self.client.request(method, path, headers={
            "x-test-user": user, "x-test-session": session}, **kwargs)

    def request(self, project, assets, endpoint="annotation-preview", *, user="alice", session="first", **options):
        return self.api("POST", f"/api/projects/{project['id']}/{endpoint}", user=user, session=session, json={
            "model_id": "sam3", "mode": "auto", "prompt": "object", "target_labels": ["object"],
            "asset_ids": assets, "confidence_threshold": 0.4, "max_retries": 0, **options})

    def create_blocks(self, project, size):
        result = self.api("POST", f"/api/projects/{project['id']}/work-blocks", json={"block_size": size})
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()["blocks"]

    def claim(self, project, block, user="alice"):
        result = self.api("POST", f"/api/projects/{project['id']}/work-blocks/claim", user=user,
                          json={"block_id": block["id"]})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def block(self, project, block_id):
        result = self.api("GET", f"/api/projects/{project['id']}/work-blocks/{block_id}")
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def annotation(self, project, asset_id, user="alice"):
        result = self.api("POST", f"/api/assets/{asset_id}/annotations", user=user, json={
            "label_id": project["labels"][0]["id"], "shape_type": "rectangle", "points": [1, 1, 10, 10]})
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()

    def workflow(self, asset_id, action, user="alice"):
        revision = self.one("SELECT revision FROM assets WHERE id=?", (asset_id,))["revision"]
        result = self.api("POST", f"/api/assets/{asset_id}/workflow", user=user,
                          json={"action": action, "expected_revision": revision, "reason": ""})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def annotations(self, asset_id, *, active=True):
        return self.all_rows("SELECT * FROM annotations WHERE asset_id=?" +
                             (" AND deleted_at IS NULL" if active else "") + " ORDER BY id", (asset_id,))

    def queue(self, project, assets, *, user="alice", **options):
        response = self.request(project, assets, endpoint="jobs", user=user, **options)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def run_job(self, job_id, status="completed"):
        from app import jobs

        self.assertEqual(jobs.claim_next_job("owner-sam3-test"), job_id)
        asyncio.run(jobs.run_annotation_job(job_id, "owner-sam3-test"))
        result = self.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        self.assertEqual(result["status"], status, result)
        return result

    def test_owner_two_image_auto_preserves_manual_and_human_modified_results(self):
        project, assets = self.fixture(count=2)
        # Create actual SAM3 annotations before assignment, then correct one by
        # the real annotation API. This checks provenance rather than hand-made rows.
        initial = self.queue(project, assets, user="admin")
        self.run_job(initial["id"])
        corrected_id = self.annotations(assets[0])[0]["id"]
        pristine_id = self.annotations(assets[1])[0]["id"]
        block = self.claim(project, self.create_blocks(project, 2)[0])
        changed = self.api("PATCH", f"/api/annotations/{corrected_id}", user="alice", json={
            "expected_version": 1, "points": [20, 10, 70, 55]})
        self.assertEqual(changed.status_code, 200, changed.text)
        manual_id = self.annotation(project, assets[1])["id"]
        protected = [self.one("SELECT * FROM annotations WHERE id=?", (item,))
                     for item in (corrected_id, manual_id)]
        self.assertEqual(protected[0]["human_modified"], 1)
        self.assertEqual(protected[1]["source"], "manual")

        before = self.snapshot()
        preview = self.request(project, assets, session="second")
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["totals"]["accepted"], 2)
        self.assertEqual(self.snapshot(), before)
        for item in preview.json()["items"]:
            self.assertEqual(item["annotations"][0]["shape_type"], "rectangle")
            self.assertEqual(item["annotations"][0]["points"], [25, 5, 90, 50])

        job = self.queue(project, assets, session="second")
        self.assertEqual(job["settings"]["block_owner_key"], f"user:{self.users['alice']['id']}")
        self.assertEqual(job["mode"], "auto")  # SAM3 has no assist mode.
        completed = self.run_job(job["id"])
        self.assertEqual((completed["processed"], completed["created_count"]), (2, 2))
        self.assertEqual([self.one("SELECT * FROM annotations WHERE id=?", (item,))
                          for item in (corrected_id, manual_id)], protected)
        self.assertIsNotNone(self.one("SELECT deleted_at FROM annotations WHERE id=?", (pristine_id,))["deleted_at"])
        for asset_id in assets:
            rows = self.annotations(asset_id)
            self.assertEqual(len(rows), 2)
            new = rows[-1]
            self.assertEqual((new["source"], new["model_id"], new["shape_type"]), ("auto", "sam3", "rectangle"))
            self.assertEqual(json.loads(new["points_json"]), [25, 5, 90, 50])
        self.assertEqual(self.block(project, block["id"])["owner_user_id"], self.users["alice"]["id"])

    def test_project_auto_selects_own_drafts_and_rejected_and_skips_review_and_foreign_block(self):
        project, assets = self.fixture(count=6)
        blocks = self.create_blocks(project, 4)
        own = self.claim(project, blocks[0])
        other = self.claim(project, blocks[1], user="bob")
        for asset_id in assets[:3]:
            self.annotation(project, asset_id)
            self.workflow(asset_id, "submit")
        self.workflow(assets[0], "approve", user="review")
        self.workflow(assets[2], "reject", user="review")
        self.assertEqual(self.block(project, own["id"])["status"], "rejected")
        untouched_ids = [assets[0], assets[1], *other["asset_ids"]]
        before_assets = [self.one("SELECT * FROM assets WHERE id=?", (item,)) for item in untouched_ids]
        before_annotations = [self.annotations(item, active=False) for item in untouched_ids]

        job = self.queue(project, None)
        self.assertEqual(job["settings"]["asset_ids"], assets[2:4])
        completed = self.run_job(job["id"])
        self.assertEqual((completed["processed"], completed["created_count"]), (2, 2))
        self.assertEqual([self.one("SELECT * FROM assets WHERE id=?", (item,)) for item in untouched_ids], before_assets)
        self.assertEqual([self.annotations(item, active=False) for item in untouched_ids], before_annotations)
        for asset_id in assets[2:4]:
            self.assertEqual(self.annotations(asset_id)[-1]["shape_type"], "rectangle")

    def test_forged_owner_cannot_write_foreign_block_but_read_only_preview_stays_available(self):
        project, assets = self.fixture(count=2)
        self.claim(project, self.create_blocks(project, 2)[0])
        before = self.snapshot()
        preview = self.request(project, assets, user="bob")
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(self.snapshot(), before)
        inference_calls = self.loader.call_count
        for user in ("bob", "admin"):
            with self.subTest(user=user):
                rejected = self.request(project, assets, endpoint="jobs", user=user,
                    block_owner_key=f"user:{self.users['alice']['id']}",
                    assist_owner_key=f"user:{self.users['alice']['id']}")
                self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.loader.call_count, inference_calls)

    def test_include_reviewed_cannot_write_reviewed_or_completed_owned_blocks(self):
        project, assets = self.fixture(count=2)
        block = self.claim(project, self.create_blocks(project, 2)[0], user="admin")
        for asset_id in assets:
            self.annotation(project, asset_id, user="admin")
            self.workflow(asset_id, "submit", user="admin")
        self.workflow(assets[0], "approve", user="review")
        before = self.snapshot()
        for asset_id in assets:
            response = self.request(project, [asset_id], endpoint="jobs", user="admin", include_reviewed=True)
            self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.snapshot(), before)
        self.workflow(assets[1], "approve", user="review")
        self.assertEqual(self.block(project, block["id"])["status"], "completed")
        before = self.snapshot()
        response = self.request(project, assets, endpoint="jobs", user="admin", include_reviewed=True)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.snapshot(), before)
        self.loader.assert_not_called()

    def test_queued_owner_job_fences_assignment_and_retries_require_original_member(self):
        project, assets = self.fixture(count=2)
        block = self.claim(project, self.create_blocks(project, 2)[0])
        job = self.queue(project, assets)
        current = self.block(project, block["id"])
        prefix = f"/api/projects/{project['id']}/work-blocks/{block['id']}"
        for action, user, extra in (("release", "alice", {}),
                                    ("assign", "admin", {"user_id": self.users["bob"]["id"]})):
            response = self.api("POST", f"{prefix}/{action}", user=user,
                                json={"expected_revision": current["revision"], **extra})
            self.assertEqual(response.status_code, 409, response.text)
        cancelled = self.api("POST", f"/api/jobs/{job['id']}/cancel", user="alice")
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        for user in ("bob", "admin"):
            response = self.api("POST", f"/api/jobs/{job['id']}/retry", user=user)
            self.assertEqual(response.status_code, 403, response.text)
        retried = self.api("POST", f"/api/jobs/{job['id']}/retry", user="alice", session="second")
        self.assertEqual(retried.status_code, 200, retried.text)
        self.run_job(job["id"])
        self.assertEqual(self.block(project, block["id"])["owner_user_id"], self.users["alice"]["id"])

    def test_worker_rechecks_block_owner_after_prediction_before_writing(self):
        project, assets = self.fixture(count=2)
        block = self.claim(project, self.create_blocks(project, 2)[0])
        job = self.queue(project, assets)
        output = self.processor.set_text_prompt.return_value

        def concurrent_owner_change(*_args, **_kwargs):
            # Normal transfers are fenced while queued/running (test above).
            # Simulate stale ownership from outside that API to exercise the
            # final transaction guard independently of revision protection.
            with self.db() as conn:
                conn.execute("UPDATE work_blocks SET owner_key=?,owner_user_id=? WHERE id=?",
                    (f"user:{self.users['bob']['id']}", self.users["bob"]["id"], block["id"]))
            return output

        self.processor.set_text_prompt.side_effect = concurrent_owner_change
        result = self.run_job(job["id"], status="failed")
        self.assertEqual(result["created_count"], 0)
        self.assertTrue(result["error"])
        self.assertEqual([self.annotations(item) for item in assets], [[], []])
        self.assertEqual(self.all_rows("SELECT * FROM job_asset_results WHERE job_id=?", (job["id"],)), [])

    def test_worker_rechecks_review_status_even_when_owner_is_reviewer(self):
        project, assets = self.fixture(count=2)
        self.claim(project, self.create_blocks(project, 2)[0], user="admin")
        job = self.queue(project, assets, user="admin", include_reviewed=True)
        output = self.processor.set_text_prompt.return_value

        def concurrent_review_change(*_args, **_kwargs):
            # Isolate the review fence: do not change revision or assignment.
            with self.db() as conn:
                conn.execute("UPDATE assets SET review_status='approved' WHERE id=?", (assets[0],))
            return output

        self.processor.set_text_prompt.side_effect = concurrent_review_change
        result = self.run_job(job["id"], status="failed")
        self.assertEqual(result["created_count"], 0)
        self.assertEqual([self.annotations(item) for item in assets], [[], []])
        self.assertEqual(self.one("SELECT review_status FROM assets WHERE id=?", (assets[0],))["review_status"], "approved")


if __name__ == "__main__":
    unittest.main()
