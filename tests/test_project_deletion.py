from __future__ import annotations

import importlib
import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image


class ProjectDeletionTest(unittest.TestCase):
    """Delete real related rows and files only inside a temporary application."""

    def setUp(self):
        # Defer settings imports until after legacy test discovery establishes its
        # own isolated environment. Never initialize the repository's real DB.
        global Actor, settings, all_rows, db, one, now_iso, main, get_actor
        from app.auth import Actor, get_actor
        from app.config import settings
        from app.database import all_rows, db, now_iso, one

        main = importlib.import_module("app.main")
        self.temp = tempfile.TemporaryDirectory(prefix="cvsprout-project-delete-")
        root = Path(self.temp.name)
        values = dict(data_dir=root / "data", models_dir=root / "models",
                      training_base_model=root / "models/training/absent.pt",
                      training_segmentation_model=root / "models/training/absent-seg.pt",
                      training_model_dirs_raw=str(root / "models/training"),
                      embedded_worker=False, embedded_pipeline_worker=False,
                      auth_mode="disabled", enable_demo_model=True, worker_url="")
        self.previous = {key: getattr(settings, key) for key in values}
        for key, value in values.items():
            object.__setattr__(settings, key, value)
        self.addCleanup(self.restore_settings)
        self.app = FastAPI(lifespan=main.lifespan)
        self.app.include_router(main.app.router)
        self.actor = Actor("project-delete-admin", "admin")
        self.app.dependency_overrides[get_actor] = lambda: self.actor
        self.client = TestClient(self.app, headers={"X-MDP-Client": "browser"}).__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def restore_settings(self):
        for key, value in self.previous.items():
            object.__setattr__(settings, key, value)
        self.temp.cleanup()

    def fixture(self, count=0, *, name="项目删除测试", color_seed=10):
        result = self.client.post("/api/projects", json={"name": name, "task_type": "detect", "labels": ["object"]})
        self.assertEqual(result.status_code, 201, result.text)
        project = self.client.get(f"/api/projects/{result.json()['id']}").json()
        assets = []
        for index in range(count):
            content = io.BytesIO()
            Image.new("RGB", (64, 48), (color_seed + index, 35, 75)).save(content, "PNG")
            result = self.client.post(f"/api/projects/{project['id']}/assets", files={
                "files": (f"image-{index}.png", content.getvalue(), "image/png")})
            self.assertEqual(result.status_code, 201, result.text)
            asset_id = result.json()["asset_ids"][0]
            result = self.client.post(f"/api/assets/{asset_id}/annotations", json={
                "label_id": project["labels"][0]["id"], "shape_type": "rectangle", "points": [3, 3, 30, 30]})
            self.assertEqual(result.status_code, 201, result.text)
            assets.append(asset_id)
        return project, assets

    def delete(self, project, **kwargs):
        if not kwargs:
            kwargs["json"] = {"confirmation_name": project["name"]}
        return self.client.request("DELETE", f"/api/projects/{project['id']}", **kwargs)

    def version(self, project, *, augmented=False):
        payload = {"approved_only": False, "train_ratio": 1, "valid_ratio": 0, "test_ratio": 0}
        if augmented:
            payload["augmentation"] = {"copies_per_image": 1, "operations": [{"type": "noise"}]}
        result = self.client.post(f"/api/projects/{project['id']}/versions", json=payload)
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()["id"]

    def job(self, project, *, status="completed", cancel_requested=0):
        with db() as conn:
            return conn.execute("""INSERT INTO jobs
                (project_id,model_id,mode,prompt,status,cancel_requested,created_at)
                VALUES (?,'demo','all','object',?,?,?)""",
                (project["id"], status, cancel_requested, now_iso())).lastrowid

    def training(self, project, version_id, *, status="completed", cancel_requested=0):
        with db() as conn:
            return conn.execute("""INSERT INTO training_runs
                (project_id,version_id,name,model_arch,status,total_epochs,cancel_requested,created_by,created_at)
                VALUES (?,?,'test run','fixture.pt',?,1,?,?,?)""",
                (project["id"], version_id, status, cancel_requested, self.actor.id, now_iso())).lastrowid

    def conversion(self, run_id, *, status="completed", cancel_requested=0):
        with db() as conn:
            return conn.execute("""INSERT INTO model_conversions
                (training_run_id,format,status,cancel_requested,created_by,created_at)
                VALUES (?,'onnx',?,?,?,?)""",
                (run_id, status, cancel_requested, self.actor.id, now_iso())).lastrowid

    def resource_lease(self, task_type, task_id, *, future=True, resource_id="cpu:test"):
        expires = (datetime.now(UTC) + timedelta(minutes=5 if future else -5)).isoformat()
        with db() as conn:
            conn.execute("""INSERT INTO pipeline_resource_leases
                (resource_id,worker_id,task_type,task_id,claim_generation,leased_at,heartbeat_at,expires_at)
                VALUES (?,'test-worker',?,?,1,?,?,?)""", (resource_id, task_type, task_id, now_iso(), now_iso(), expires))

    def graph(self, *, name="完整项目", color_seed=10, augmented=False):
        project, assets = self.fixture(2, name=name, color_seed=color_seed)
        version_id = self.version(project, augmented=augmented)
        blocks = self.client.post(f"/api/projects/{project['id']}/work-blocks", json={"block_size": 1})
        self.assertEqual(blocks.status_code, 201, blocks.text)
        job_id = self.job(project)
        run_id = self.training(project, version_id)
        conversion_id = self.conversion(run_id)
        run_dir = settings.training_dir / f"run-{run_id}"
        conversion_dir = settings.training_dir / f"conversion-{conversion_id}"
        run_file = run_dir / "attempt-1/weights/best.pt"
        conversion_file = conversion_dir / "attempt-1/model.onnx"
        for path in (run_file, conversion_file):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"artifact: {project['id']} {path.name}".encode())
        with db() as conn:
            conn.execute("INSERT INTO job_asset_results (job_id,asset_id,created_count) VALUES (?,?,1)", (job_id, assets[0]))
            conn.execute("""INSERT INTO review_events (asset_id,from_status,to_status,actor,created_at)
                VALUES (?,'draft','approved',?,?)""", (assets[0], self.actor.id, now_iso()))
            conn.execute("UPDATE training_runs SET artifact_path=? WHERE id=?", (str(run_file), run_id))
            conn.execute("UPDATE model_conversions SET artifact_path=? WHERE id=?", (str(conversion_file), conversion_id))
            conn.execute("""INSERT INTO training_run_logs (run_id,level,event,message,created_at)
                VALUES (?,'info','completed','test',?)""", (run_id, now_iso()))
            conn.execute("INSERT INTO training_metrics (run_id,epoch,progress,created_at) VALUES (?,1,100,?)", (run_id, now_iso()))
            conn.execute("""INSERT INTO conversion_logs (conversion_id,level,event,message,created_at)
                VALUES (?,'info','completed','test',?)""", (conversion_id, now_iso()))
        for asset_id in assets:
            result = self.client.get(f"/api/assets/{asset_id}/thumbnail")
            self.assertEqual(result.status_code, 200, result.text)
        snapshot_asset = one("SELECT id FROM dataset_version_assets WHERE version_id=? ORDER BY id", (version_id,))["id"]
        result = self.client.get(f"/api/versions/{version_id}/assets/{snapshot_asset}/thumbnail")
        self.assertEqual(result.status_code, 200, result.text)
        files = [settings.media_dir / row["storage_name"] for row in all_rows("SELECT storage_name FROM assets WHERE project_id=?", (project["id"],))]
        directories = [settings.versions_dir / f"version-{version_id}", run_dir, conversion_dir]
        return dict(project=project, assets=assets, version=version_id, run=run_id,
                    conversion=conversion_id, job=job_id, files=files, directories=directories)

    def assert_intact(self, project, assets=()):
        self.assertIsNotNone(one("SELECT * FROM projects WHERE id=?", (project["id"],)))
        for asset_id in assets:
            asset = one("SELECT * FROM assets WHERE id=?", (asset_id,))
            self.assertIsNotNone(asset)
            self.assertTrue((settings.media_dir / asset["storage_name"]).is_file())

    def test_admin_only_and_anonymous_requests_cannot_delete(self):
        project, assets = self.fixture(1)
        for role in ("annotator", "reviewer"):
            with self.subTest(role=role):
                self.actor = Actor(f"test-{role}", role)
                result = self.delete(project)
                self.assertEqual(result.status_code, 403, result.text)
                self.assert_intact(project, assets)
        self.app.dependency_overrides.pop(get_actor)
        object.__setattr__(settings, "auth_mode", "accounts")
        result = self.delete(project)
        self.assertEqual(result.status_code, 401, result.text)
        self.assert_intact(project, assets)

    def test_confirmation_is_required_and_must_match_the_complete_unicode_name(self):
        project, assets = self.fixture(1, name='中文项目 / 空格 <>&"\' 🐈')
        invalid = [None, {}, {"confirmation_name": ""}, {"confirmation_name": " "},
                   {"confirmation_name": "错误项目"}, {"confirmation_name": project["name"] + " "},
                   {"confirmation_name": " " + project["name"]}, {"confirmation_name": project["name"][:-1]},
                   {"confirmation_name": None}, {"confirmation_name": 123},
                   {"confirmation_name": project["name"], "force": True}]
        for payload in invalid:
            with self.subTest(payload=payload):
                result = self.delete(project, json=payload)
                self.assertEqual(result.status_code, 422, result.text)
                self.assert_intact(project, assets)
        result = self.delete(project)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["deleted"])
        self.assertEqual((result.json()["project_id"], result.json()["name"]), (project["id"], project["name"]))

    def test_account_privileges_and_session_are_rechecked_inside_the_delete_transaction(self):
        project, assets = self.fixture(1)
        with db() as conn:
            user_id = conn.execute("""INSERT INTO users (username,display_name,password_hash,role,created_at)
                VALUES ('changed-admin','Admin','unused','reviewer',?)""", (now_iso(),)).lastrowid
            conn.execute("""INSERT INTO user_sessions (token_hash,user_id,expires_at,session_id)
                VALUES ('test-session-hash',?,?,'test-session')""",
                (user_id, (datetime.now(UTC) + timedelta(minutes=5)).isoformat()))
        # Dependency resolution may have happened before an administrator was
        # demoted or logged out. That stale Actor must not authorize deletion.
        self.actor = Actor("stale-admin", "admin", user_id=user_id, session_id="test-session")
        result = self.delete(project)
        self.assertEqual(result.status_code, 403, result.text)
        self.assert_intact(project, assets)
        with db() as conn:
            conn.execute("UPDATE users SET role='admin' WHERE id=?", (user_id,))
            conn.execute("DELETE FROM user_sessions WHERE user_id=?", (user_id,))
        result = self.delete(project)
        self.assertEqual(result.status_code, 401, result.text)
        self.assert_intact(project, assets)

    def test_nonexistent_project_returns_404(self):
        result = self.delete({"id": 999999, "name": "不存在"})
        self.assertEqual(result.status_code, 404, result.text)

    def test_empty_and_archived_projects_can_be_deleted(self):
        for archived in (False, True):
            with self.subTest(archived=archived):
                project, _ = self.fixture()
                if archived:
                    with db() as conn:
                        conn.execute("UPDATE projects SET archived_at=? WHERE id=?", (now_iso(), project["id"]))
                result = self.delete(project)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["cleanup"]["status"], "completed")
                self.assertEqual(self.client.get(f"/api/projects/{project['id']}").status_code, 404)

    def test_complete_graph_and_owned_files_are_deleted_without_touching_other_projects(self):
        target = self.graph(name="删除对象", augmented=True)
        kept = self.graph(name="保留对象", color_seed=50)
        self.resource_lease("training", target["run"], future=False, resource_id="cpu:deleted")
        self.resource_lease("training", kept["run"], future=True, resource_id="cpu:kept")
        weights = settings.models_dir / "training/shared.pt"
        resource_lock = settings.training_dir / "resource-locks/cuda-0.lock"
        for path in (weights, resource_lock):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"global resource must survive")
        # Shared catalog configuration, users, workers and historical audits are
        # global, even when their IDs happen to resemble a deleted project's ID.
        with db() as conn:
            conn.execute("""INSERT INTO users (username,display_name,password_hash,role,created_at)
                VALUES ('remaining-admin','Admin','unused','admin',?)""", (now_iso(),))
            conn.execute("""INSERT INTO workers (id,started_at,last_seen_at,status)
                VALUES ('remaining-worker',?,?,'offline')""", (now_iso(), now_iso()))
        audit_ids = {row["id"] for row in all_rows("SELECT id FROM audit_logs")}
        global_rows = {table: all_rows(f"SELECT * FROM {table}") for table in ("users", "workers", "model_services")}
        tables = ("projects", "labels", "assets", "annotations", "work_blocks", "work_block_assets", "work_block_events",
                  "jobs", "job_asset_results", "review_events", "dataset_versions", "dataset_version_labels",
                  "dataset_version_assets", "dataset_version_annotations", "training_runs", "training_run_logs",
                  "training_metrics", "model_conversions", "conversion_logs", "pipeline_resource_leases")
        # Every primary-key row belonging to the second graph should remain byte
        # for byte equal. Capture those IDs through its owning rows before DELETE.
        kept_ids = {
            "projects": ("id", [kept["project"]["id"]]), "labels": ("project_id", [kept["project"]["id"]]),
            "assets": ("project_id", [kept["project"]["id"]]), "annotations": ("asset_id", kept["assets"]),
            "work_blocks": ("project_id", [kept["project"]["id"]]), "work_block_assets": ("asset_id", kept["assets"]),
            "work_block_events": ("block_id", [row["id"] for row in all_rows("SELECT id FROM work_blocks WHERE project_id=?", (kept["project"]["id"],))]),
            "jobs": ("id", [kept["job"]]), "job_asset_results": ("job_id", [kept["job"]]), "review_events": ("asset_id", kept["assets"]),
            "dataset_versions": ("id", [kept["version"]]), "dataset_version_labels": ("version_id", [kept["version"]]),
            "dataset_version_assets": ("version_id", [kept["version"]]), "dataset_version_annotations": ("version_id", [kept["version"]]),
            "training_runs": ("id", [kept["run"]]), "training_run_logs": ("run_id", [kept["run"]]),
            "training_metrics": ("run_id", [kept["run"]]), "model_conversions": ("id", [kept["conversion"]]),
            "conversion_logs": ("conversion_id", [kept["conversion"]]), "pipeline_resource_leases": ("resource_id", ["cpu:kept"]),
        }
        remaining_rows = {}
        for table, (column, ids) in kept_ids.items():
            remaining_rows[table] = all_rows(f"SELECT * FROM {table} WHERE {column} IN ({','.join('?' for _ in ids)})", tuple(ids))
            self.assertTrue(remaining_rows[table], table)
        kept_files = {path: path.read_bytes() for path in kept["files"] + [weights, resource_lock]}
        for directory in kept["directories"]:
            kept_files.update({path: path.read_bytes() for path in directory.rglob("*") if path.is_file()})
        kept_thumbnails = set(settings.thumbnail_dir.glob(f"version-{kept['version']}-*.webp"))
        for asset_id in kept["assets"]:
            sha = one("SELECT sha256 FROM assets WHERE id=?", (asset_id,))["sha256"]
            kept_thumbnails.update(settings.thumbnail_dir.glob(f"{sha}-*.webp"))
        kept_files.update({path: path.read_bytes() for path in kept_thumbnails})
        target_thumbnails = set(settings.thumbnail_dir.glob("*.webp")) - kept_thumbnails

        result = self.delete(target["project"])
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["cleanup"]["status"], "completed")
        self.assertEqual(result.json()["counts"], {"assets": 2, "annotations": 2, "labels": 1,
                         "work_blocks": 2, "jobs": 1, "versions": 1, "training_runs": 1, "conversions": 1})
        for table in tables:
            self.assertEqual(all_rows(f"SELECT * FROM {table}"), remaining_rows[table], table)
        for table, rows in global_rows.items():
            self.assertEqual(all_rows(f"SELECT * FROM {table}"), rows, table)
        for path in target["files"] + target["directories"] + list(target_thumbnails):
            self.assertFalse(path.exists(), str(path))
        for path, content in kept_files.items():
            self.assertEqual(path.read_bytes(), content, str(path))
        self.assertEqual(all_rows("PRAGMA foreign_key_check"), [])
        self.assertTrue(audit_ids.issubset({row["id"] for row in all_rows("SELECT id FROM audit_logs")}))
        audit = one("SELECT * FROM audit_logs WHERE action='delete' AND resource_type='project' AND project_id=?", (target["project"]["id"],))
        self.assertIsNotNone(audit)
        self.assertEqual(audit["actor"], self.actor.id)
        self.assertEqual(json.loads(audit["payload_json"])["cleanup"]["status"], "completed")

    def test_shared_thumbnail_and_external_artifact_paths_are_preserved(self):
        target = self.graph()
        kept, kept_assets = self.fixture(1)  # Same bytes => shared SHA cache key.
        shared_asset = one("SELECT * FROM assets WHERE id=?", (kept_assets[0],))
        shared_thumbnail = settings.thumbnail_dir / f"{shared_asset['sha256']}-320.webp"
        self.assertTrue(shared_thumbnail.is_file())
        shared_weights = settings.models_dir / "training/shared.pt"
        shared_weights.parent.mkdir(parents=True, exist_ok=True)
        shared_weights.write_bytes(b"shared training checkpoint")
        with db() as conn:
            conn.execute("UPDATE training_runs SET artifact_path=? WHERE id=?", (str(shared_weights), target["run"]))
            conn.execute("UPDATE model_conversions SET artifact_path=? WHERE id=?", (str(shared_weights), target["conversion"]))
        result = self.delete(target["project"])
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(shared_thumbnail.is_file())
        self.assertEqual(shared_weights.read_bytes(), b"shared training checkpoint")
        self.assert_intact(kept, kept_assets)

    def test_thumbnail_reused_after_delete_commit_is_retained_during_cleanup(self):
        project, assets = self.fixture(1)
        response = self.client.get(f"/api/assets/{assets[0]}/thumbnail")
        self.assertEqual(response.status_code, 200, response.text)
        sha = one("SELECT sha256 FROM assets WHERE id=?", (assets[0],))["sha256"]
        thumbnail = settings.thumbnail_dir / f"{sha}-320.webp"
        original_thumbnail = thumbnail.read_bytes()
        deletion = importlib.import_module("app.project_deletion")
        cleanup = deletion._cleanup_files
        kept = []

        def upload_then_cleanup(items, audit_id):
            # The target's transaction has committed, but physical cleanup has
            # not run yet. Another project can now upload the same image bytes.
            self.assertIsNone(one("SELECT * FROM projects WHERE id=?", (project["id"],)))
            kept.extend(self.fixture(1, name="删除提交后导入相同图片"))
            return cleanup(items, audit_id)

        with patch.object(deletion, "_cleanup_files", side_effect=upload_then_cleanup):
            response = self.delete(project)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["cleanup"]["status"], "completed")
        self.assertEqual(response.json()["cleanup"]["retained_count"], 1)
        self.assertEqual(thumbnail.read_bytes(), original_thumbnail)
        self.assert_intact(kept[0], kept[1])

    def test_other_project_artifact_and_base_model_references_prevent_owned_directory_removal(self):
        target = self.graph()
        other, other_assets = self.fixture(1, name="依赖训练产物的项目", color_seed=90)
        other_run = self.training(other, self.version(other))
        other_conversion = self.conversion(other_run)
        target_artifact = one("SELECT artifact_path FROM training_runs WHERE id=?", (target["run"],))["artifact_path"]
        for reference in ("training_artifact", "conversion_artifact", "base_model"):
            with self.subTest(reference=reference):
                with db() as conn:
                    conn.execute("UPDATE training_runs SET artifact_path=NULL,settings_json='{}' WHERE id=?", (other_run,))
                    conn.execute("UPDATE model_conversions SET artifact_path=NULL WHERE id=?", (other_conversion,))
                    if reference == "training_artifact":
                        conn.execute("UPDATE training_runs SET artifact_path=? WHERE id=?", (target_artifact, other_run))
                    elif reference == "conversion_artifact":
                        conn.execute("UPDATE model_conversions SET artifact_path=? WHERE id=?", (target_artifact, other_conversion))
                    else:
                        conn.execute("UPDATE training_runs SET settings_json=? WHERE id=?",
                                     (json.dumps({"base_model": target_artifact}), other_run))
                result = self.delete(target["project"])
                self.assertEqual(result.status_code, 409, result.text)
                self.assert_intact(target["project"], target["assets"])
                self.assert_intact(other, other_assets)
                self.assertTrue(Path(target_artifact).is_file())

    def test_shared_configured_checkpoint_inside_owned_directory_prevents_deletion(self):
        target = self.graph()
        artifact = Path(one("SELECT artifact_path FROM training_runs WHERE id=?", (target["run"],))["artifact_path"])
        object.__setattr__(settings, "training_base_model", artifact)
        result = self.delete(target["project"])
        self.assertEqual(result.status_code, 409, result.text)
        self.assert_intact(target["project"], target["assets"])
        self.assertTrue(artifact.is_file())

    def test_missing_owned_files_do_not_prevent_database_deletion(self):
        project, assets = self.fixture(1)
        source = settings.media_dir / one("SELECT storage_name FROM assets WHERE id=?", (assets[0],))["storage_name"]
        source.unlink()
        result = self.delete(project)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["cleanup"]["status"], "completed")
        self.assertEqual(result.json()["cleanup"]["missing_count"], 1)
        self.assertIsNone(one("SELECT * FROM projects WHERE id=?", (project["id"],)))

    def test_foreign_version_reference_rolls_back_every_database_change_and_keeps_files(self):
        target = self.graph()
        other, other_assets = self.fixture(1, name="异常跨项目版本引用", color_seed=90)
        self.training(other, target["version"])
        before = {table: all_rows(f"SELECT * FROM {table}") for table in
                  ("projects", "training_runs", "dataset_versions", "dataset_version_annotations", "audit_logs")}
        result = self.delete(target["project"])
        self.assertEqual(result.status_code, 409, result.text)
        for table, rows in before.items():
            self.assertEqual(all_rows(f"SELECT * FROM {table}"), rows, table)
        self.assert_intact(target["project"], target["assets"])
        self.assert_intact(other, other_assets)
        for directory in target["directories"]:
            self.assertTrue(directory.is_dir())

    def test_foreign_annotation_and_job_result_links_cannot_cascade_into_another_project(self):
        target = self.graph()
        other = self.graph(name="异常关联的保留项目", color_seed=90)
        target_asset = one("SELECT id FROM dataset_version_assets WHERE version_id=?", (target["version"],))["id"]
        other_asset = one("SELECT id FROM dataset_version_assets WHERE version_id=?", (other["version"],))["id"]
        other_annotation = one("SELECT id FROM dataset_version_annotations WHERE version_id=?", (other["version"],))["id"]
        for link in ("version_annotation", "job_result"):
            with self.subTest(link=link):
                with db() as conn:
                    if link == "version_annotation":
                        conn.execute("UPDATE dataset_version_annotations SET version_asset_id=? WHERE id=?", (target_asset, other_annotation))
                    else:
                        conn.execute("UPDATE dataset_version_annotations SET version_asset_id=? WHERE id=?", (other_asset, other_annotation))
                        conn.execute("INSERT INTO job_asset_results (job_id,asset_id) VALUES (?,?)", (other["job"], target["assets"][0]))
                before = {table: all_rows(f"SELECT * FROM {table}") for table in
                          ("dataset_version_annotations", "job_asset_results", "audit_logs")}
                response = self.delete(target["project"])
                self.assertEqual(response.status_code, 409, response.text)
                for table, rows in before.items():
                    self.assertEqual(all_rows(f"SELECT * FROM {table}"), rows, table)
                self.assert_intact(target["project"], target["assets"])
                self.assert_intact(other["project"], other["assets"])

    def test_queued_and_running_annotation_jobs_block_deletion_even_after_cancel_request(self):
        for status in ("queued", "running"):
            for cancel_requested in (0, 1):
                with self.subTest(status=status, cancel_requested=cancel_requested):
                    project, assets = self.fixture(1)
                    self.job(project, status=status, cancel_requested=cancel_requested)
                    result = self.delete(project)
                    self.assertEqual(result.status_code, 409, result.text)
                    self.assert_intact(project, assets)

    def test_queued_and_running_training_or_conversions_block_deletion(self):
        for task_type in ("training", "conversion"):
            for status in ("queued", "running"):
                for cancel_requested in (0, 1):
                    with self.subTest(task_type=task_type, status=status, cancel_requested=cancel_requested):
                        project, assets = self.fixture(1)
                        version_id = self.version(project)
                        run = self.training(project, version_id, status=status if task_type == "training" else "completed",
                                            cancel_requested=cancel_requested if task_type == "training" else 0)
                        if task_type == "conversion":
                            self.conversion(run, status=status, cancel_requested=cancel_requested)
                        result = self.delete(project)
                        self.assertEqual(result.status_code, 409, result.text)
                        self.assert_intact(project, assets)

    def test_effective_edit_leases_block_deletion_including_the_admins_own_lease(self):
        for owner in ("another-annotator", self.actor.id):
            with self.subTest(owner=owner):
                project, assets = self.fixture(1)
                with db() as conn:
                    conn.execute("UPDATE assets SET lock_owner=?,lock_token='test-token',lock_expires_at=? WHERE id=?",
                                 (owner, (datetime.now(UTC) + timedelta(minutes=5)).isoformat(), assets[0]))
                result = self.delete(project)
                self.assertEqual(result.status_code, 409, result.text)
                self.assert_intact(project, assets)

    def test_expired_edit_leases_and_terminal_tasks_allow_deletion(self):
        project, assets = self.fixture(1)
        version_id = self.version(project)
        for status in ("completed", "failed", "cancelled"):
            self.job(project, status=status)
            run = self.training(project, version_id, status=status)
            conversion = self.conversion(run, status=status)
            self.resource_lease("conversion", conversion, future=False, resource_id=f"cpu:{status}")
        with db() as conn:
            conn.execute("UPDATE assets SET lock_owner='former-user',lock_token='old',lock_expires_at=? WHERE id=?",
                         ((datetime.now(UTC) - timedelta(minutes=5)).isoformat(), assets[0]))
        result = self.delete(project)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(all_rows("SELECT * FROM pipeline_resource_leases"), [])

    def test_live_pipeline_resource_leases_block_terminal_runs_and_conversions(self):
        for task_type in ("training", "conversion"):
            with self.subTest(task_type=task_type):
                project, assets = self.fixture(1)
                run = self.training(project, self.version(project))
                task_id = self.conversion(run) if task_type == "conversion" else run
                self.resource_lease(task_type, task_id, resource_id=f"cpu:{task_type}")
                result = self.delete(project)
                self.assertEqual(result.status_code, 409, result.text)
                self.assert_intact(project, assets)

    def test_invalid_media_path_is_rejected_before_any_database_or_file_deletion(self):
        for storage_name in ("../protected.txt", str(Path(self.temp.name) / "protected.txt")):
            with self.subTest(storage_name=storage_name):
                project, assets = self.fixture(1)
                protected = Path(self.temp.name) / "protected.txt"
                protected.write_bytes(b"keep outside media")
                with db() as conn:
                    conn.execute("UPDATE assets SET storage_name=? WHERE id=?", (storage_name, assets[0]))
                result = self.delete(project)
                self.assertEqual(result.status_code, 409, result.text)
                self.assertIsNotNone(one("SELECT * FROM projects WHERE id=?", (project["id"],)))
                self.assertIsNotNone(one("SELECT * FROM assets WHERE id=?", (assets[0],)))
                self.assertEqual(protected.read_bytes(), b"keep outside media")

    def test_top_level_owned_directory_symlink_is_rejected_without_touching_its_target(self):
        project, _ = self.fixture(1)
        version = self.version(project)
        run = self.training(project, version)
        protected = Path(self.temp.name) / "outside-owned-directory"
        protected.mkdir()
        (protected / "checkpoint.pt").write_bytes(b"outside checkpoint")
        (settings.training_dir / f"run-{run}").symlink_to(protected, target_is_directory=True)
        result = self.delete(project)
        self.assertEqual(result.status_code, 409, result.text)
        self.assertIsNotNone(one("SELECT * FROM projects WHERE id=?", (project["id"],)))
        self.assertEqual((protected / "checkpoint.pt").read_bytes(), b"outside checkpoint")

    def test_symlinks_inside_owned_directories_do_not_delete_their_targets(self):
        graph = self.graph()
        protected = Path(self.temp.name) / "outside-symlink-target"
        protected.mkdir()
        (protected / "keep.pt").write_bytes(b"outside checkpoint")
        (settings.training_dir / f"run-{graph['run']}" / "external").symlink_to(protected, target_is_directory=True)
        result = self.delete(graph["project"])
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse((settings.training_dir / f"run-{graph['run']}").exists())
        self.assertEqual((protected / "keep.pt").read_bytes(), b"outside checkpoint")

    def test_cleanup_failure_is_reported_and_audited_after_database_commit(self):
        project, assets = self.fixture(1)
        source = settings.media_dir / one("SELECT storage_name FROM assets WHERE id=?", (assets[0],))["storage_name"]
        deletion = importlib.import_module("app.project_deletion")
        with patch.object(deletion, "_remove_path", side_effect=PermissionError("simulated cleanup failure")):
            result = self.delete(project)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["deleted"])
        self.assertEqual(result.json()["cleanup"]["status"], "pending")
        self.assertGreater(result.json()["cleanup"]["pending_count"], 0)
        self.assertIsNone(one("SELECT * FROM projects WHERE id=?", (project["id"],)))
        self.assertTrue(source.exists())
        audit = one("SELECT * FROM audit_logs WHERE action='delete' AND resource_type='project' AND project_id=?", (project["id"],))
        details = json.loads(audit["payload_json"])
        self.assertEqual(details["cleanup"]["status"], "pending")
        self.assertIn(source.name, audit["payload_json"])

    def test_two_concurrent_delete_requests_remove_the_project_exactly_once(self):
        project, _ = self.fixture(1)
        ready = threading.Barrier(2)

        def request_delete():
            ready.wait(timeout=10)
            return self.delete(project)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(request_delete) for _ in range(2)]
            responses = [future.result(timeout=20) for future in futures]
        self.assertEqual(sorted(response.status_code for response in responses), [200, 404],
                         [response.text for response in responses])
        audits = all_rows("SELECT * FROM audit_logs WHERE action='delete' AND resource_type='project' AND project_id=?", (project["id"],))
        self.assertEqual(len(audits), 1)
        self.assertEqual(all_rows("PRAGMA foreign_key_check"), [])


if __name__ == "__main__":
    unittest.main()
