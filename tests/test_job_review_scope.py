from __future__ import annotations

import importlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


class JobReviewScopeTest(unittest.TestCase):
    def setUp(self):
        global Actor, settings, db, one, all_rows, now_iso
        from app.auth import Actor, get_actor
        from app.config import settings
        from app.database import db, one, all_rows, now_iso
        main = importlib.import_module("app.main")
        self.temp = tempfile.TemporaryDirectory(prefix="mdp-job-review-")
        values = dict(data_dir=Path(self.temp.name), embedded_worker=False,
                      embedded_pipeline_worker=False, enable_demo_model=True, auth_mode="disabled")
        self.previous = {key: getattr(settings, key) for key in values}
        for key, value in values.items():
            object.__setattr__(settings, key, value)
        app = FastAPI(lifespan=main.lifespan)
        app.include_router(main.app.router)
        self.assertIn("/api/projects/{project_id}/review/jobs/{job_id}", app.openapi()["paths"])
        self.actor = Actor("job-review-admin", "admin")
        app.dependency_overrides[get_actor] = lambda: self.actor
        self.client = TestClient(app, headers={"X-MDP-Client": "browser"}).__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for key, value in self.previous.items():
            object.__setattr__(settings, key, value)
        self.temp.cleanup()

    def fixture(self, count=2):
        project = self.client.post("/api/projects", json={"name": "Job review fixture", "task_type": "detect", "labels": ["object"]})
        self.assertEqual(project.status_code, 201, project.text)
        project = self.client.get(f"/api/projects/{project.json()['id']}").json()
        assets = []
        with db() as conn:
            for index in range(count):
                asset = conn.execute("""INSERT INTO assets
                    (project_id,filename,storage_name,mime_type,width,height,size_bytes,status,created_at,updated_at)
                    VALUES (?,?,?,'image/png',64,64,1,'labeled',?,?)""",
                    (project["id"], f"fixture-{index}.png", f"p{project['id']}-{index}.png", now_iso(), now_iso())).lastrowid
                conn.execute("""INSERT INTO annotations
                    (asset_id,label_id,shape_type,points_json,created_at,updated_at)
                    VALUES (?,?,'rectangle','[3,3,30,30]',?,?)""", (asset, project["labels"][0]["id"], now_iso(), now_iso()))
                assets.append(asset)
        return project, assets

    def job(self, project, assets, *, recorded=None, status="completed", created_count=1):
        result = self.client.post(f"/api/projects/{project['id']}/jobs", json={"model_id": "demo", "prompt": "object", "asset_ids": assets})
        self.assertEqual(result.status_code, 202, result.text)
        job = result.json()
        recorded = assets if recorded is None else recorded
        with db() as conn:
            conn.execute("UPDATE jobs SET status=?,processed=?,finished_at=? WHERE id=?", (status, len(recorded), now_iso(), job["id"]))
            conn.executemany("INSERT INTO job_asset_results(job_id,asset_id,created_count) VALUES (?,?,?)",
                             [(job["id"], asset, created_count) for asset in recorded])
        return job

    def scope(self, project, job):
        return self.client.get(f"/api/projects/{project['id']}/review/jobs/{job['id']}")

    def targets(self, assets):
        return [{"asset_id": asset, "expected_revision": one("SELECT revision FROM assets WHERE id=?", (asset,))["revision"]} for asset in assets]

    def submit(self, project, scope, *, assets=None, preview=False):
        return self.client.post(f"/api/projects/{project['id']}/review/{'preview' if preview else 'batch'}", json={
            "action": "submit", "source_job_id": scope["source_job_id"], "source_job_generation": scope["source_job_generation"],
            "assets": assets if assets is not None else [
                {"asset_id": item["asset_id"], "expected_revision": item["expected_revision"]} for item in scope["items"]]})

    def test_scope_uses_saved_results_only_for_failed_and_cancelled_partial_tasks(self):
        for status in ("failed", "cancelled"):
            with self.subTest(status=status):
                project, assets = self.fixture(3)
                job = self.job(project, assets[:2], recorded=[assets[0]], status=status)
                scope = self.scope(project, job).json()
                self.assertEqual(scope["asset_ids"], [assets[0]])
                self.assertTrue(scope["can_submit"])
                self.assertEqual(scope["counts"]["processed"], 1)
                before = all_rows("SELECT * FROM assets WHERE project_id=?", (project["id"],))
                preview = self.submit(project, scope, assets=self.targets(assets), preview=True).json()
                self.assertEqual((preview["counts"]["eligible"], preview["counts"]["conflict"]), (1, 2))
                self.assertEqual(all_rows("SELECT * FROM assets WHERE project_id=?", (project["id"],)), before)
                result = self.submit(project, scope, assets=self.targets(assets)).json()
                self.assertEqual((result["counts"]["succeeded"], result["counts"]["conflict"]), (1, 2))
                self.assertEqual(result["items"][1]["code"], "source_job_asset_missing")
                self.assertEqual(one("SELECT review_status FROM assets WHERE id=?", (assets[1],))["review_status"], "draft")

    def test_current_manual_corrections_and_zero_created_count_are_supported(self):
        project, assets = self.fixture()
        job = self.job(project, assets, created_count=0)
        with db() as conn:
            conn.execute("UPDATE annotations SET points_json='[4,4,40,40]',human_modified=1 WHERE asset_id=?", (assets[0],))
            conn.execute("UPDATE assets SET revision=revision+1 WHERE id=?", (assets[0],))
            conn.execute("UPDATE annotations SET deleted_at=? WHERE asset_id=?", (now_iso(), assets[1]))
        scope = self.scope(project, job).json()
        self.assertEqual(scope["counts"]["pending_submission"], 1)
        self.assertEqual(scope["counts"]["without_annotations"], 1)
        self.assertEqual(scope["items"][0]["created_count"], 0)
        result = self.submit(project, scope).json()
        self.assertEqual((result["counts"]["succeeded"], result["counts"]["skipped"]), (1, 1))
        self.assertEqual(result["items"][1]["code"], "no_annotations")
        self.assertEqual(one("SELECT points_json FROM annotations WHERE asset_id=?", (assets[0],))["points_json"], "[4,4,40,40]")
        audit = json.loads(one("SELECT payload_json FROM audit_logs WHERE action='submit' AND resource_type='asset_review'")["payload_json"])
        self.assertEqual((audit["source_job_id"], audit["source_job_generation"]), (job["id"], 0))

    def test_queued_and_running_jobs_cannot_submit_even_with_saved_results(self):
        for status in ("queued", "running"):
            with self.subTest(status=status):
                project, assets = self.fixture(1)
                job = self.job(project, assets, status=status)
                scope = self.scope(project, job).json()
                self.assertTrue(scope["scope_available"])
                self.assertFalse(scope["can_submit"])
                self.assertEqual(scope["scope_code"], "job_active")
                for preview in (False, True):
                    result = self.submit(project, scope, preview=preview).json()
                    self.assertEqual(result["items"][0]["code"], "source_job_active")
                self.assertEqual(one("SELECT review_status FROM assets WHERE id=?", (assets[0],))["review_status"], "draft")

    def test_legacy_and_missing_results_never_fall_back_to_requested_or_project_assets(self):
        project, assets = self.fixture()
        for config in ({}, {"asset_ids": None}, {"asset_ids": assets}):
            job = self.job(project, assets, recorded=[])
            with db() as conn:
                conn.execute("UPDATE jobs SET settings_json=?,processed=2 WHERE id=?", (json.dumps(config), job["id"]))
            scope = self.scope(project, job).json()
            self.assertEqual((scope["asset_ids"], scope["total"], scope["scope_code"]), ([], 0, "scope_unavailable"))
            self.assertFalse(scope["scope_available"])
            self.assertFalse(scope["can_submit"])
            self.assertTrue(scope["scope_reason"])
            result = self.submit(project, scope, assets=self.targets(assets)).json()
            self.assertEqual(result["counts"]["conflict"], 2)
        # A legacy missing target list is safe when there IS a saved result.
        with db() as conn:
            conn.execute("UPDATE jobs SET settings_json='{}' WHERE id=?", (job["id"],))
            conn.execute("INSERT INTO job_asset_results(job_id,asset_id,created_count) VALUES (?,?,0)", (job["id"], assets[0]))
        self.assertEqual(self.scope(project, job).json()["asset_ids"], [assets[0]])

    def test_retry_invalidates_preview_even_if_new_results_have_identical_asset_revision(self):
        project, assets = self.fixture(1)
        job = self.job(project, assets, status="failed")
        old = self.scope(project, job).json()
        self.assertEqual(self.submit(project, old, preview=True).json()["counts"]["eligible"], 1)
        retry = self.client.post(f"/api/jobs/{job['id']}/retry")
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(one("SELECT review_generation FROM jobs WHERE id=?", (job["id"],))["review_generation"], 1)
        self.assertEqual(self.submit(project, old).json()["items"][0]["code"], "source_job_changed")
        with db() as conn:
            conn.execute("UPDATE jobs SET status='completed' WHERE id=?", (job["id"],))
            conn.execute("INSERT INTO job_asset_results(job_id,asset_id,created_count) VALUES (?,?,1)", (job["id"], assets[0]))
        self.assertEqual(self.submit(project, old).json()["items"][0]["code"], "source_job_changed")
        current = self.scope(project, job).json()
        self.assertEqual(current["source_job_generation"], 1)
        self.assertEqual(self.submit(project, current).json()["counts"]["succeeded"], 1)

    def test_concurrent_retry_and_submit_are_serialized(self):
        project, assets = self.fixture(1)
        job = self.job(project, assets, status="failed")
        scope = self.scope(project, job).json()
        with ThreadPoolExecutor(max_workers=2) as pool:
            submit_future = pool.submit(self.submit, project, scope)
            retry_future = pool.submit(self.client.post, f"/api/jobs/{job['id']}/retry")
            submit, retry = submit_future.result(), retry_future.result()
        self.assertEqual(submit.status_code, 200, submit.text)
        self.assertIn(retry.status_code, (200, 409), retry.text)
        self.assertEqual(submit.json()["counts"]["succeeded"] + int(retry.status_code == 200), 1)
        if retry.status_code == 200:
            self.assertEqual(submit.json()["items"][0]["code"], "source_job_changed")
            self.assertEqual(one("SELECT review_status FROM assets WHERE id=?", (assets[0],))["review_status"], "draft")

    def test_duplicate_submission_is_not_reapplied_and_deleted_assets_leave_scope(self):
        project, assets = self.fixture()
        job = self.job(project, assets)
        with db() as conn:
            conn.execute("DELETE FROM assets WHERE id=?", (assets[1],))
        scope = self.scope(project, job).json()
        self.assertEqual(scope["asset_ids"], [assets[0]])
        self.assertEqual(self.submit(project, scope).json()["counts"]["succeeded"], 1)
        self.assertEqual(self.submit(project, scope).json()["counts"]["conflict"], 1)
        current = self.scope(project, job).json()
        self.assertFalse(current["can_submit"])
        self.assertEqual(current["counts"]["submitted"], 1)
        self.assertEqual(self.submit(project, current).json()["counts"]["skipped"], 1)
        self.assertEqual(one("SELECT COUNT(*) AS n FROM review_events")["n"], 1)

    def test_scope_cannot_cross_projects_and_role_rules_remain_enforced(self):
        project, assets = self.fixture(3)
        other, other_assets = self.fixture(1)
        job = self.job(project, assets)
        scope = self.scope(project, job).json()
        self.assertEqual(self.scope(other, job).status_code, 404)
        wrong = self.submit(other, scope, assets=self.targets(other_assets)).json()
        self.assertEqual(wrong["items"][0]["code"], "source_job_not_found")
        for index, role in enumerate(("admin", "reviewer", "annotator")):
            self.actor = Actor(f"role-{role}", role)
            self.assertEqual(self.scope(project, job).status_code, 200)
            self.assertEqual(self.submit(project, scope, assets=self.targets([assets[index]])).json()["counts"]["succeeded"], 1)
        self.actor = Actor("unknown", "unknown")
        self.assertEqual(self.scope(project, job).status_code, 403)
        self.assertEqual(self.submit(project, scope, assets=self.targets([assets[0]])).json()["counts"]["forbidden"], 1)
        self.actor = Actor("initial", "admin", must_change_password=True)
        self.assertEqual(self.scope(project, job).status_code, 403)
        self.assertEqual(self.submit(project, scope).status_code, 403)

    def test_source_fields_must_be_paired_validated_and_only_submit(self):
        project, assets = self.fixture(1)
        job = self.job(project, assets)
        payload = {"action": "submit", "assets": self.targets(assets), "source_job_id": job["id"], "source_job_generation": 0}
        invalid = [{key: value for key, value in payload.items() if key != missing}
                   for missing in ("source_job_id", "source_job_generation")]
        invalid += [{**payload, key: value} for key, value in (
            ("source_job_id", True), ("source_job_id", -1), ("source_job_id", 2**64),
            ("source_job_generation", True), ("source_job_generation", -1),
            ("source_job_generation", "0"), ("action", "approve"), ("action", "reject"))]
        for body in invalid:
            result = self.client.post(f"/api/projects/{project['id']}/review/batch", json=body)
            self.assertEqual(result.status_code, 422, result.text)
        self.assertEqual(one("SELECT COUNT(*) AS n FROM review_events")["n"], 0)

    def test_large_scope_can_be_submitted_in_batches_without_project_fallback(self):
        project, assets = self.fixture(502)
        job = self.job(project, assets[:501])
        scope = self.scope(project, job).json()
        self.assertEqual(scope["total"], 501)
        self.assertEqual(self.submit(project, scope).status_code, 422)
        first = self.submit(project, scope, assets=self.targets(assets[:500]))
        second = self.submit(project, scope, assets=self.targets(assets[500:501]))
        self.assertEqual(first.json()["counts"]["succeeded"], 500, first.text[:200])
        self.assertEqual(second.json()["counts"]["succeeded"], 1)
        self.assertEqual(one("SELECT review_status FROM assets WHERE id=?", (assets[501],))["review_status"], "draft")


if __name__ == "__main__":
    unittest.main()
