from __future__ import annotations

import asyncio
import importlib
import io
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from PIL import Image

from app.auth import Actor, get_actor
from app.config import settings
from app.database import all_rows, db, now_iso, one
from app.jobs import claim_next_job, run_annotation_job

main = importlib.import_module("app.main")


class WorkBlocksTest(unittest.TestCase):
    """Exercise actual HTTP transactions against a separate database per test."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cvsprout-work-blocks-")
        values = dict(data_dir=Path(self.temp.name), embedded_worker=False,
                      embedded_pipeline_worker=False, auth_mode="disabled", enable_demo_model=True)
        self.previous = {key: getattr(settings, key) for key in values}
        for key, value in values.items():
            object.__setattr__(settings, key, value)
        app = FastAPI(lifespan=main.lifespan)
        app.include_router(main.app.router)
        self.users = {}

        def actor(request: Request):
            name = request.headers.get("x-test-user", "admin")
            user = self.users[name]
            session = request.headers.get("x-test-session", "first")
            return Actor(f"user:{name}:{session}", user["role"], name, user["id"], name,
                         edit_token=request.headers.get("x-edit-token"))

        app.dependency_overrides[get_actor] = actor
        self.client = TestClient(app).__enter__()
        with db() as conn:
            for name, role in (("admin", "admin"), ("alice", "annotator"),
                               ("bob", "annotator"), ("review", "reviewer")):
                user_id = conn.execute(
                    "INSERT INTO users (username,display_name,password_hash,role,created_at) VALUES (?,?,?,?,?)",
                    (name, name, "unused-test-password-hash", role, now_iso()),
                ).lastrowid
                self.users[name] = {"id": user_id, "role": role}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for key, value in self.previous.items():
            object.__setattr__(settings, key, value)
        self.temp.cleanup()

    def request(self, method, path, *, user="admin", session="first", edit_token=None, **kwargs):
        headers = {"x-test-user": user, "x-test-session": session}
        if edit_token is not None:
            headers["x-edit-token"] = edit_token
        return self.client.request(method, path, headers=headers, **kwargs)

    def fixture(self, count=3, task="detect", annotated=False):
        created = self.request("POST", "/api/projects", json={"name": "Work blocks", "task_type": task, "labels": ["object"]})
        self.assertEqual(created.status_code, 201, created.text)
        project = self.client.get(f"/api/projects/{created.json()['id']}").json()
        assets = [self.upload(project, index) for index in range(count)]
        if annotated:
            for asset_id in assets:
                self.annotation(project, asset_id)
        return project, assets

    def upload(self, project, index):
        image = io.BytesIO()
        Image.new("RGB", (64, 48), (index % 255, index // 255, 70)).save(image, "PNG")
        result = self.request("POST", f"/api/projects/{project['id']}/assets",
                              files={"files": (f"image-{index}.png", image.getvalue(), "image/png")})
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()["asset_ids"][0]

    def base(self, project):
        return f"/api/projects/{project['id']}/work-blocks"

    def blocks(self, project, user="admin"):
        result = self.request("GET", self.base(project), user=user)
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def create_blocks(self, project, size=50):
        result = self.request("POST", self.base(project), json={"block_size": size})
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()

    def claim(self, project, user="alice", block_id=None, **kwargs):
        result = self.request("POST", self.base(project) + "/claim", user=user,
                              json={} if block_id is None else {"block_id": block_id}, **kwargs)
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def detail(self, project, block_id, user="admin"):
        result = self.request("GET", self.base(project) + f"/{block_id}", user=user)
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def revision(self, asset_id):
        return one("SELECT revision FROM assets WHERE id=?", (asset_id,))["revision"]

    def annotation(self, project, asset_id, user="admin", session="first"):
        result = self.request("POST", f"/api/assets/{asset_id}/annotations", user=user, session=session,
                              json={"label_id": project["labels"][0]["id"], "shape_type": "rectangle", "points": [2, 2, 30, 35]})
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()

    def workflow(self, asset_id, action, user="alice", reason=""):
        return self.request("POST", f"/api/assets/{asset_id}/workflow", user=user,
                            json={"action": action, "expected_revision": self.revision(asset_id), "reason": reason})

    def test_partition_is_disjoint_and_new_imports_do_not_change_existing_blocks(self):
        project, assets = self.fixture(5)
        self.assertFalse(self.blocks(project)["enabled"])
        created = self.create_blocks(project, 2)
        self.assertEqual(created["created_count"], 3)
        self.assertTrue(created["enabled"])
        self.assertEqual(created["unassigned_count"], 0)
        groups = {block["id"]: block["asset_ids"] for block in created["blocks"]}
        self.assertEqual(sorted(len(ids) for ids in groups.values()), [1, 2, 2])
        self.assertEqual(sorted(asset for ids in groups.values() for asset in ids), assets)
        self.claim(project)
        again = self.request("POST", self.base(project), json={"block_size": 1})
        self.assertEqual(again.status_code, 409, again.text)
        self.assertEqual(len(self.blocks(project)["blocks"]), 3)
        new_asset = self.upload(project, 99)
        self.assertEqual(self.blocks(project)["unassigned_count"], 1)
        updated = self.create_blocks(project, 2)
        self.assertEqual(updated["created_count"], 1)
        for block in updated["blocks"]:
            self.assertEqual(block["asset_ids"], groups.get(block["id"], [new_asset]))

    def test_invalid_sizes_and_permissions_cannot_enable_block_mode(self):
        project, _ = self.fixture(1)
        for size in (0, -1, 501, True, 1.5, "50"):
            result = self.request("POST", self.base(project), json={"block_size": size})
            self.assertEqual(result.status_code, 422, result.text)
        for user in ("alice", "review"):
            self.assertEqual(self.request("POST", self.base(project), user=user, json={}).status_code, 403)
        self.assertFalse(self.blocks(project)["enabled"])
        self.assertEqual(self.blocks(project)["blocks"], [])

    def test_concurrent_claim_of_one_block_has_one_winner(self):
        project, _ = self.fixture(2)
        block_id = self.create_blocks(project)["blocks"][0]["id"]
        barrier = threading.Barrier(2)

        def claim(user):
            barrier.wait(timeout=10)
            return self.request("POST", self.base(project) + "/claim", user=user, json={"block_id": block_id})

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ["alice", "bob"]))
        self.assertEqual(sorted(result.status_code for result in results), [200, 409], [result.text for result in results])
        winner = next(result.json() for result in results if result.status_code == 200)
        self.assertEqual(self.detail(project, block_id)["owner_user_id"], winner["owner_user_id"])

    def test_concurrent_next_claims_receive_disjoint_blocks(self):
        project, assets = self.fixture(4)
        self.create_blocks(project, 2)
        barrier = threading.Barrier(2)

        def claim(user):
            barrier.wait(timeout=10)
            return self.claim(project, user=user)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.map(claim, ["alice", "bob"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertFalse(set(first["asset_ids"]) & set(second["asset_ids"]))
        self.assertEqual(set(first["asset_ids"]) | set(second["asset_ids"]), set(assets))

    def test_owner_is_account_not_login_session(self):
        project, assets = self.fixture(1)
        self.create_blocks(project)
        block = self.claim(project)
        self.annotation(project, assets[0], "alice", session="second-browser")
        detail = self.request("GET", self.base(project) + f"/{block['id']}", user="alice", session="after-login-again").json()
        self.assertTrue(detail["is_mine"])
        self.assertEqual(detail["owner_user_id"], self.users["alice"]["id"])
        self.assertEqual(detail["status"], "in_progress")

    def test_unclaimed_and_foreign_blocks_reject_every_manual_write(self):
        project, assets = self.fixture(1, annotated=True)
        item = self.client.get(f"/api/assets/{assets[0]}/annotations").json()[0]
        self.create_blocks(project)
        self.assertEqual(self.request("POST", f"/api/assets/{assets[0]}/lock", user="alice").status_code, 403)
        self.claim(project)
        for user in ("bob", "admin"):
            before = one("SELECT * FROM assets WHERE id=?", (assets[0],))
            requests = [
                self.request("POST", f"/api/assets/{assets[0]}/lock", user=user),
                self.request("POST", f"/api/assets/{assets[0]}/annotations", user=user,
                             json={"label_id": item["label_id"], "shape_type": "rectangle", "points": [1, 1, 20, 20]}),
                self.request("PATCH", f"/api/annotations/{item['id']}", user=user,
                             json={"expected_version": item["version"], "points": [1, 1, 20, 20]}),
                self.request("DELETE", f"/api/annotations/{item['id']}?expected_version={item['version']}", user=user),
                self.workflow(assets[0], "start", user=user),
                self.workflow(assets[0], "submit", user=user),
                self.workflow(assets[0], "confirm_empty", user=user),
            ]
            for response in requests:
                self.assertEqual(response.status_code, 403, response.text)
            targets = [{"asset_id": assets[0], "expected_revision": before["revision"]}]
            for endpoint in ("preview", "batch"):
                result = self.request("POST", f"/api/projects/{project['id']}/review/{endpoint}", user=user,
                                      json={"action": "submit", "assets": targets})
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["counts"]["forbidden"], 1, result.text)
            self.assertEqual(one("SELECT * FROM assets WHERE id=?", (assets[0],)), before)
        self.assertEqual(self.workflow(assets[0], "submit").status_code, 200)
        self.assertEqual(self.workflow(assets[0], "approve", user="review").status_code, 200)

    def test_transfer_clears_locks_fences_stale_edits_and_records_history(self):
        project, assets = self.fixture(1)
        self.create_blocks(project)
        block = self.claim(project)
        item = self.annotation(project, assets[0], "alice")
        self.assertEqual(self.request("POST", f"/api/assets/{assets[0]}/lock", user="alice").status_code, 200)
        old_revision = self.revision(assets[0])
        block = self.detail(project, block["id"])
        path = self.base(project) + f"/{block['id']}/assign"
        payload = {"expected_revision": block["revision"], "user_id": self.users["bob"]["id"], "reason": "交接给下一班"}
        self.assertEqual(self.request("POST", path, user="alice", json=payload).status_code, 403)
        transferred = self.request("POST", path, json=payload)
        self.assertEqual(transferred.status_code, 200, transferred.text)
        self.assertEqual(transferred.json()["owner_user_id"], self.users["bob"]["id"])
        self.assertGreater(self.revision(assets[0]), old_revision)
        self.assertIsNone(one("SELECT lock_owner FROM assets WHERE id=?", (assets[0],))["lock_owner"])
        self.assertEqual(self.request("PATCH", f"/api/annotations/{item['id']}", user="alice",
                                      json={"expected_version": item["version"], "points": [3, 3, 33, 33]}).status_code, 403)
        stale = self.request("POST", f"/api/assets/{assets[0]}/workflow", user="bob",
                             json={"action": "submit", "expected_revision": old_revision})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.request("POST", path, json=payload).status_code, 409)
        detail = self.detail(project, block["id"])
        self.assertTrue(any(event["payload"].get("reason") == "交接给下一班" for event in detail["events"]))
        self.assertEqual(self.workflow(assets[0], "submit", user="bob").status_code, 200)

    def test_release_preserves_annotations_and_next_owner_can_continue(self):
        project, assets = self.fixture(1)
        self.create_blocks(project)
        block = self.claim(project)
        annotation = self.annotation(project, assets[0], "alice")
        block = self.detail(project, block["id"])
        path = self.base(project) + f"/{block['id']}/release"
        payload = {"expected_revision": block["revision"], "reason": "主动交回"}
        self.assertEqual(self.request("POST", path, user="bob", json=payload).status_code, 403)
        released = self.request("POST", path, user="alice", json=payload)
        self.assertEqual(released.status_code, 200, released.text)
        self.assertEqual(released.json()["status"], "available")
        self.assertIsNone(released.json()["owner_user_id"])
        self.assertEqual(self.client.get(f"/api/assets/{assets[0]}/annotations").json()[0]["id"], annotation["id"])
        self.assertEqual(self.workflow(assets[0], "submit").status_code, 403)
        self.assertEqual(self.claim(project, "bob")["id"], block["id"])
        self.assertEqual(self.workflow(assets[0], "submit", user="bob").status_code, 200)

    def test_review_completion_does_not_requeue_and_reopen_is_explicit(self):
        project, assets = self.fixture(2, annotated=True)
        self.create_blocks(project)
        block = self.claim(project)
        for asset_id in assets:
            self.assertEqual(self.workflow(asset_id, "submit").status_code, 200)
        self.assertEqual(self.detail(project, block["id"])["status"], "submitted")
        self.assertEqual(self.workflow(assets[0], "approve", user="review").status_code, 200)
        self.assertNotEqual(self.detail(project, block["id"])["status"], "completed")
        self.assertEqual(self.workflow(assets[1], "approve", user="review").status_code, 200)
        completed = self.detail(project, block["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["approved_count"], 2)
        self.assertIsNotNone(completed["completed_at"])
        self.assertEqual(self.request("POST", self.base(project) + "/claim", user="bob", json={}).status_code, 409)
        self.assertEqual(self.workflow(assets[0], "reopen", user="review").status_code, 409)
        path = self.base(project) + f"/{block['id']}/reopen"
        payload = {"expected_revision": completed["revision"], "reason": "复查标注标准"}
        self.assertEqual(self.request("POST", path, user="review", json=payload).status_code, 403)
        self.assertEqual(self.request("POST", path, json={**payload, "reason": " "}).status_code, 422)
        reopened = self.request("POST", path, json=payload)
        self.assertEqual(reopened.status_code, 200, reopened.text)
        self.assertNotEqual(reopened.json()["status"], "completed")
        self.assertIsNone(reopened.json()["completed_at"])

    def test_rejection_stays_with_owner_and_approved_images_remain_locked(self):
        project, assets = self.fixture(2, annotated=True)
        self.create_blocks(project)
        block = self.claim(project)
        for asset_id in assets:
            self.assertEqual(self.workflow(asset_id, "submit").status_code, 200)
        self.assertEqual(self.workflow(assets[0], "approve", user="review").status_code, 200)
        self.assertEqual(self.workflow(assets[1], "reject", user="review", reason="右侧边界需要校正").status_code, 200)
        rejected = self.detail(project, block["id"])
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["owner_user_id"], self.users["alice"]["id"])
        self.assertEqual(self.request("POST", self.base(project) + "/claim", user="bob", json={"block_id": block["id"]}).status_code, 409)
        annotation = self.client.get(f"/api/assets/{assets[0]}/annotations").json()[0]
        self.assertEqual(self.request("PATCH", f"/api/annotations/{annotation['id']}", user="alice",
                                      json={"expected_version": annotation["version"], "points": [3, 3, 33, 33]}).status_code, 409)
        self.assertEqual(self.workflow(assets[1], "submit").status_code, 200)

    def test_submitted_block_does_not_prevent_claiming_next_block(self):
        project, assets = self.fixture(2, annotated=True)
        self.create_blocks(project, 1)
        first = self.claim(project)
        self.assertEqual(self.workflow(first["asset_ids"][0], "submit").status_code, 200)
        second = self.claim(project)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.detail(project, first["id"])["owner_user_id"], self.users["alice"]["id"])

    def test_rejection_without_reason_keeps_owner_and_annotations_and_checks_access(self):
        project, assets = self.fixture(3, annotated=True)
        self.create_blocks(project)
        block = self.claim(project)
        for asset_id in assets:
            self.assertEqual(self.workflow(asset_id, "submit").status_code, 200)
        before = all_rows("SELECT * FROM annotations ORDER BY id")
        self.assertEqual(self.workflow(assets[0], "reject", user="alice").status_code, 403)
        stale = self.request("POST", f"/api/assets/{assets[0]}/workflow", user="review",
                             json={"action": "reject", "expected_revision": self.revision(assets[0]) - 1})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(one("SELECT COUNT(*) AS n FROM review_events WHERE to_status='rejected'")["n"], 0)
        for asset_id, extra in zip(assets, ({}, {"reason": ""}, {"reason": " \t\n "})):
            with self.subTest(extra=extra):
                response = self.request("POST", f"/api/assets/{asset_id}/workflow", user="review",
                                        json={"action": "reject", "expected_revision": self.revision(asset_id), **extra})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["review_reason"], "")
                self.assertEqual(response.json()["review_status"], "rejected")
                event = one("SELECT * FROM review_events WHERE asset_id=? AND to_status='rejected'", (asset_id,))
                self.assertEqual(event["reason"], "")
        rejected = self.detail(project, block["id"])
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["owner_user_id"], self.users["alice"]["id"])
        self.assertEqual(all_rows("SELECT * FROM annotations ORDER BY id"), before)
        self.assertEqual(self.request("POST", self.base(project) + "/claim", user="bob", json={"block_id": block["id"]}).status_code, 409)

    def test_foreign_project_and_disabled_assignee_are_rejected(self):
        project, _ = self.fixture(1)
        other, _ = self.fixture(1)
        block = self.create_blocks(project)["blocks"][0]
        self.create_blocks(other)
        self.assertEqual(self.request("GET", self.base(other) + f"/{block['id']}").status_code, 404)
        self.assertEqual(self.request("POST", self.base(other) + "/claim", user="alice", json={"block_id": block["id"]}).status_code, 404)
        with db() as conn:
            conn.execute("UPDATE users SET enabled=0 WHERE id=?", (self.users["bob"]["id"],))
        response = self.request("POST", self.base(project) + f"/{block['id']}/assign",
                                json={"expected_revision": block["revision"], "user_id": self.users["bob"]["id"], "reason": "停用用户"})
        self.assertIn(response.status_code, (404, 422), response.text)
        assignees = self.request("GET", self.base(project) + "/assignees")
        self.assertEqual(assignees.status_code, 200, assignees.text)
        self.assertNotIn(self.users["bob"]["id"], [user["id"] for user in assignees.json()])
        self.assertEqual(self.request("GET", self.base(project) + "/assignees", user="alice").status_code, 403)

    def test_ai_preparing_block_is_unclaimable_and_owned_block_is_skipped(self):
        project, assets = self.fixture(2)
        self.create_blocks(project, 1)
        job = self.request("POST", f"/api/projects/{project['id']}/jobs",
                           json={"model_id": "demo", "prompt": "object", "asset_ids": [assets[0]]})
        self.assertEqual(job.status_code, 202, job.text)
        preparing = next(block for block in self.blocks(project)["blocks"] if assets[0] in block["asset_ids"])
        self.assertEqual(preparing["status"], "preparing")
        claim = self.request("POST", self.base(project) + "/claim", user="alice", json={"block_id": preparing["id"]})
        self.assertEqual(claim.status_code, 409, claim.text)
        owned = self.claim(project)
        self.assertEqual(owned["asset_ids"], [assets[1]])
        self.assertEqual(self.request("POST", f"/api/jobs/{job.json()['id']}/cancel").status_code, 200)
        whole = self.request("POST", f"/api/projects/{project['id']}/jobs", json={"model_id": "demo", "prompt": "object"})
        self.assertEqual(whole.status_code, 202, whole.text)
        planned = self.client.get(f"/api/jobs/{whole.json()['id']}").json()
        self.assertEqual(planned["settings"]["asset_ids"], [assets[0]])
        explicit = self.request("POST", f"/api/projects/{project['id']}/jobs",
                                json={"model_id": "demo", "prompt": "object", "asset_ids": [assets[1]]})
        self.assertEqual(explicit.status_code, 409, explicit.text)

    def test_single_image_assist_is_reserved_for_owner_and_worker_preserves_assignment(self):
        project, assets = self.fixture(1)
        self.create_blocks(project)
        block = self.claim(project)
        self.assertEqual(self.workflow(assets[0], "confirm_empty").status_code, 200)
        path = f"/api/projects/{project['id']}/jobs"
        payload = {"model_id": "demo", "prompt": "object", "mode": "assist", "asset_ids": assets}
        for user in ("bob", "admin"):
            denied = self.request("POST", path, user=user, json=payload)
            self.assertEqual(denied.status_code, 409, denied.text)
        accepted = self.request("POST", path, user="alice", json=payload)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        job_id = accepted.json()["id"]
        self.assertEqual(accepted.json()["settings"]["assist_owner_key"], f"user:{self.users['alice']['id']}")
        current = self.detail(project, block["id"])
        transfer = self.request("POST", self.base(project) + f"/{block['id']}/assign",
                                json={"expected_revision": current["revision"], "user_id": self.users["bob"]["id"]})
        self.assertEqual(transfer.status_code, 409, transfer.text)
        self.assertEqual(claim_next_job("block-test-worker"), job_id)
        asyncio.run(run_annotation_job(job_id, "block-test-worker"))
        result = self.client.get(f"/api/jobs/{job_id}").json()
        self.assertEqual(result["status"], "completed", result)
        annotations = self.client.get(f"/api/assets/{assets[0]}/annotations").json()
        self.assertEqual(len(annotations), 1)
        self.assertFalse(one("SELECT negative_confirmed FROM assets WHERE id=?", (assets[0],))["negative_confirmed"])
        self.assertEqual(self.detail(project, block["id"])["owner_user_id"], self.users["alice"]["id"])

    def test_confirmed_empty_images_can_complete_review_and_export(self):
        project, assets = self.fixture(1)
        self.create_blocks(project)
        block = self.claim(project)
        self.assertEqual(self.workflow(assets[0], "submit").status_code, 409)
        confirmed = self.workflow(assets[0], "confirm_empty")
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertTrue(confirmed.json()["negative_confirmed"])
        self.assertEqual(self.workflow(assets[0], "submit").status_code, 200)
        self.assertEqual(self.workflow(assets[0], "approve", user="review").status_code, 200)
        self.assertEqual(self.detail(project, block["id"])["status"], "completed")
        version = self.request("POST", f"/api/projects/{project['id']}/versions",
                               json={"name": "Confirmed negatives", "train_ratio": 1, "valid_ratio": 0, "test_ratio": 0})
        self.assertEqual(version.status_code, 201, version.text)
        exported = self.client.get(f"/api/versions/{version.json()['id']}/export/yolo")
        self.assertEqual(exported.status_code, 200, exported.text)
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            labels = [name for name in archive.namelist() if name.startswith("labels/") and name.endswith(".txt")]
            self.assertEqual(len(labels), 1)
            self.assertEqual(archive.read(labels[0]), b"")

    def test_bulk_review_accepts_confirmed_negatives_and_completes_block(self):
        project, assets = self.fixture(2)
        self.create_blocks(project)
        block = self.claim(project)
        self.annotation(project, assets[0], "alice")
        self.assertEqual(self.workflow(assets[1], "confirm_empty").status_code, 200)
        for action, user in (("submit", "alice"), ("approve", "review")):
            targets = [{"asset_id": asset_id, "expected_revision": self.revision(asset_id)} for asset_id in assets]
            payload = {"action": action, "assets": targets}
            preview = self.request("POST", f"/api/projects/{project['id']}/review/preview", user=user, json=payload)
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertEqual(preview.json()["counts"]["eligible"], 2, preview.text)
            applied = self.request("POST", f"/api/projects/{project['id']}/review/batch", user=user, json=payload)
            self.assertEqual(applied.status_code, 200, applied.text)
            self.assertEqual(applied.json()["counts"]["succeeded"], 2, applied.text)
        self.assertEqual(self.detail(project, block["id"])["status"], "completed")

    def test_confirm_empty_is_explicit_and_cleared_when_annotation_added(self):
        project, assets = self.fixture(2)
        self.create_blocks(project)
        self.claim(project)
        self.annotation(project, assets[0], "alice")
        self.assertEqual(self.workflow(assets[0], "confirm_empty").status_code, 409)
        self.assertEqual(self.workflow(assets[1], "confirm_empty").status_code, 200)
        self.annotation(project, assets[1], "alice")
        self.assertFalse(one("SELECT negative_confirmed FROM assets WHERE id=?", (assets[1],))["negative_confirmed"])
        classification, classification_assets = self.fixture(1, task="classify")
        self.create_blocks(classification)
        self.claim(classification)
        self.assertEqual(self.workflow(classification_assets[0], "confirm_empty").status_code, 422)

    def test_editor_tokens_prevent_tabs_from_sharing_or_releasing_an_image_lock(self):
        project, assets = self.fixture(1)
        self.create_blocks(project)
        self.claim(project)
        asset_id = assets[0]
        lock_path = f"/api/assets/{asset_id}/lock"
        token_a, token_b = "a" * 32, "b" * 32
        acquired = self.request("POST", lock_path, user="alice", edit_token=token_a)
        self.assertEqual(acquired.status_code, 200, acquired.text)
        self.assertEqual(self.request("POST", lock_path, user="alice", edit_token=token_b).status_code, 423)
        self.assertEqual(self.request("POST", lock_path, user="alice").status_code, 423)
        payload = {"label_id": project["labels"][0]["id"], "shape_type": "rectangle", "points": [2, 2, 30, 35]}
        self.assertEqual(self.request("POST", f"/api/assets/{asset_id}/annotations", user="alice", edit_token=token_b, json=payload).status_code, 423)
        for token in (token_b, None):
            self.request("DELETE", lock_path, user="alice", edit_token=token)
            self.assertEqual(self.request("POST", lock_path, user="bob", edit_token=token_b).status_code, 403)
            self.assertIsNotNone(one("SELECT lock_owner FROM assets WHERE id=?", (asset_id,))["lock_owner"])
        self.assertEqual(self.request("POST", f"/api/assets/{asset_id}/annotations", user="alice", edit_token=token_a, json=payload).status_code, 201)
        self.assertEqual(self.request("DELETE", lock_path, user="alice", edit_token=token_a).status_code, 200)
        self.assertIsNone(one("SELECT lock_owner FROM assets WHERE id=?", (asset_id,))["lock_owner"])
        self.assertEqual(self.request("POST", lock_path, user="alice").status_code, 200)
        self.assertEqual(self.request("DELETE", lock_path, user="alice").status_code, 200)

    def test_real_actor_dependency_validates_and_resolves_editor_header(self):
        def request_for(value):
            return Request({"type": "http", "method": "GET", "path": "/api/session",
                            "headers": [] if value is None else [(b"x-edit-token", value.encode())]})

        for value in ("page_A-123", None):
            actor = get_actor(request_for(value), authorization=None, x_actor_id="same-session", mdp_session=None)
            self.assertEqual(actor.id, "same-session")
            self.assertEqual(actor.edit_token, value)
        for value in ("", "a" * 129, "bad token"):
            with self.assertRaises(HTTPException) as error:
                get_actor(request_for(value), authorization=None, x_actor_id="same-session", mdp_session=None)
            self.assertEqual(error.exception.status_code, 400)

    def test_legacy_project_remains_editable_without_claim(self):
        project, assets = self.fixture(1)
        self.annotation(project, assets[0], "alice")
        self.assertEqual(self.workflow(assets[0], "submit").status_code, 200)
        self.assertEqual(self.workflow(assets[0], "approve", user="review").status_code, 200)
        self.assertFalse(self.blocks(project)["enabled"])
        self.assertEqual(all_rows("SELECT * FROM work_blocks WHERE project_id=?", (project["id"],)), [])

    def test_legacy_approved_image_can_be_reopened_then_added_to_a_new_block(self):
        project, assets = self.fixture(2, annotated=True)
        self.assertEqual(self.workflow(assets[0], "submit", user="admin").status_code, 200)
        self.assertEqual(self.workflow(assets[0], "approve", user="review").status_code, 200)
        created = self.create_blocks(project)
        self.assertEqual([block["asset_ids"] for block in created["blocks"]], [[assets[1]]])
        self.assertEqual(self.workflow(assets[0], "reopen", user="alice").status_code, 403)
        reopened = self.workflow(assets[0], "reopen", user="review")
        self.assertEqual(reopened.status_code, 200, reopened.text)
        self.assertEqual(reopened.json()["review_status"], "draft")
        self.assertEqual(self.workflow(assets[0], "submit", user="admin").status_code, 403)
        added = self.create_blocks(project)
        self.assertEqual(added["created_count"], 1)
        block = next(block for block in added["blocks"] if assets[0] in block["asset_ids"])
        self.claim(project, "alice", block["id"])
        self.assertEqual(self.workflow(assets[0], "submit").status_code, 200)


if __name__ == "__main__":
    unittest.main()
