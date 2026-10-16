from __future__ import annotations

import hashlib
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


class TrainingNavigationTest(unittest.TestCase):
    def setUp(self):
        # Defer app imports so full test discovery can configure its own
        # isolated environment before importing application settings.
        global training, settings, db, all_rows, now_iso
        from app.config import settings
        from app.database import db, all_rows, now_iso
        training = importlib.import_module("app.training")
        main = importlib.import_module("app.main")
        self.temp = tempfile.TemporaryDirectory(prefix="mdp-training-navigation-")
        values = dict(data_dir=Path(self.temp.name), embedded_worker=False,
                      embedded_pipeline_worker=False, enable_demo_model=False, auth_mode="disabled")
        self.previous = {key: getattr(settings, key) for key in values}
        for key, value in values.items():
            object.__setattr__(settings, key, value)
        self.client = TestClient(main.app, headers={"X-MDP-Client": "browser"}).__enter__()
        self.availability = patch.object(training, "training_availability", return_value={"ready": True})
        self.availability.start()
        self.project, self.version = self.create_project("Main")
        self.other_project, self.other_version = self.create_project("Other")

    def tearDown(self):
        self.availability.stop()
        self.client.__exit__(None, None, None)
        for key, value in self.previous.items():
            object.__setattr__(settings, key, value)
        self.temp.cleanup()

    def create_project(self, name):
        with db() as conn:
            project = conn.execute(
                "INSERT INTO projects(name,task_type,created_at,updated_at) VALUES (?,'detect',?,?)",
                (name, now_iso(), now_iso()),
            ).lastrowid
            version = conn.execute("""INSERT INTO dataset_versions
                (project_id,version_number,name,train_ratio,valid_ratio,test_ratio,content_hash,
                 asset_count,annotation_count,task_type,created_by,created_at)
                VALUES (?,1,'navigation',0.8,0.2,0,'fixture',2,2,'detect','test',?)""",
                (project, now_iso()),
            ).lastrowid
        return project, version

    def add_run(self, name="Training", status="completed", task="detect", model="yolo.pt", *,
                other=False, artifact=False, finished_at=None):
        project, version = (self.other_project, self.other_version) if other else (self.project, self.version)
        with db() as conn:
            run = conn.execute("""INSERT INTO training_runs
                (project_id,version_id,name,model_arch,task,status,total_epochs,current_epoch,progress,
                 claimed_by,claim_generation,created_by,created_at,finished_at)
                VALUES (?,?,?,?,?,?,50,10,0.2,'fixture-worker',3,'test','2026-01-01T00:00:00+00:00',?)""",
                (project, version, name, model, task, status, finished_at),
            ).lastrowid
            if artifact:
                path = Path(self.temp.name) / f"run-{run}.pt"
                content = f"artifact-{run}".encode()
                path.write_bytes(content)
                metadata = {"filename": path.name, "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                conn.execute("UPDATE training_runs SET artifact_path=?,artifact_metadata_json=? WHERE id=?",
                             (str(path), json.dumps(metadata), run))
        return run

    def listing(self, *, project=None, **params):
        response = self.client.get(f"/api/projects/{project or self.project}/training-runs", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def summary(self, project=None):
        response = self.client.get(f"/api/projects/{project or self.project}/training-summary")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_multiple_pages_stable_order_legacy_list_and_boundary_clamping(self):
        ids = [self.add_run(f"Trial {index}") for index in range(23)]
        self.add_run("Other project", other=True)
        pages = [self.listing(page=page) for page in (1, 2, 3)]
        self.assertEqual([len(page["runs"]) for page in pages], [10, 10, 3])
        self.assertEqual([run["id"] for page in pages for run in page["runs"]], ids[::-1])
        self.assertEqual(pages[1]["pagination"], {"page": 2, "page_size": 10, "total": 23, "total_pages": 3})
        self.assertEqual(pages[0]["total"], 23)
        self.assertEqual(self.listing(page=2147483647)["pagination"]["page"], 3)
        self.assertEqual(len(self.listing(page=1, page_size=50)["runs"]), 23)
        legacy = self.listing()
        self.assertNotIn("pagination", legacy)
        self.assertNotIn("locate_found", legacy)
        self.assertEqual(legacy["availability"], {"ready": True})
        self.assertEqual([run["id"] for run in legacy["runs"]], ids[::-1])
        detail = self.client.get(f"/api/training-runs/{ids[0]}").json()
        self.assertEqual(detail["project_id"], self.project)
        self.assertEqual(detail["id"], ids[0])

    def test_availability_is_opt_out_and_lightweight_lists_preserve_response_data(self):
        ids = [self.add_run(f"Trial {index}", status="running" if index % 2 else "completed", artifact=True)
               for index in range(14)]
        self.add_run("Other project", other=True)
        requests = ({}, {"page": 2, "page_size": 4},
                    {"page": 1, "page_size": 3, "status": "active", "task": "detect", "q": "Trial", "locate_id": ids[1]},
                    {"page": 9, "q": "absent"})
        tables = ("training_runs", "training_run_logs", "training_metrics", "pipeline_resource_leases", "audit_logs")
        before = {table: all_rows(f"SELECT * FROM {table}") for table in tables}
        with patch.object(training, "training_availability", return_value={"ready": True}) as availability:
            defaults = [self.listing(**params) for params in requests]
            self.assertEqual(availability.call_count, len(requests))
            self.assertEqual(self.listing(include_availability=True), defaults[0])
            self.assertEqual(availability.call_count, len(requests) + 1)
        with (
            patch.object(training, "training_availability", side_effect=AssertionError("poll probes availability")),
            patch.object(training, "model_catalog", side_effect=AssertionError("poll discovers models")),
            patch.object(training, "training_resources", side_effect=AssertionError("poll queries devices")),
            patch.object(training.conversion_tools, "format_catalog", side_effect=AssertionError("poll discovers conversions")),
            patch.object(training.conversion_tools, "_probe_tensorrt", side_effect=AssertionError("poll starts TensorRT probe")),
            patch.object(training.conversion_tools, "_probe_toolkit", side_effect=AssertionError("poll starts RKNN probe")),
        ):
            for params, expected in zip(requests, defaults):
                with self.subTest(params=params):
                    result = self.listing(include_availability=False, **params)
                    self.assertNotIn("availability", result)
                    self.assertEqual(result, {key: value for key, value in expected.items() if key != "availability"})
        self.assertEqual({table: all_rows(f"SELECT * FROM {table}") for table in tables}, before)

    def test_lightweight_list_keeps_counts_location_and_rows_in_one_snapshot(self):
        first = self.add_run("Trial queued", status="queued")
        counts = training._training_counts

        def complete_during_read(conn, project):
            result = counts(conn, project)
            # WAL permits a worker completion/new run between the reader's
            # counts and row queries. All response fields must keep its snapshot.
            with db() as writer:
                writer.execute("UPDATE training_runs SET status='completed' WHERE id=?", (first,))
            self.add_run("Trial new", status="queued")
            return result

        with (
            patch.object(training, "_training_counts", side_effect=complete_during_read),
            patch.object(training, "training_availability", side_effect=AssertionError("snapshot poll probes environment")),
        ):
            result = self.listing(page=1, page_size=1, status="queued", q="Trial", locate_id=first,
                                  include_availability=False)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["counts"]["queued"], 1)
        self.assertEqual(result["counts"]["completed"], 0)
        self.assertTrue(result["locate_found"])
        self.assertEqual(result["pagination"], {"page": 1, "page_size": 1, "total": 1, "total_pages": 1})
        self.assertEqual([(row["id"], row["status"]) for row in result["runs"]], [(first, "queued")])
        current = self.listing(page=1, include_availability=False)
        self.assertEqual(current["total"], 2)
        self.assertEqual((current["counts"]["queued"], current["counts"]["completed"]), (1, 1))

    def test_filters_combine_but_project_counts_remain_unfiltered(self):
        statuses = ("queued", "running", "completed", "failed", "cancelled")
        tasks = ("detect", "segment", "pose", "classify")
        ids = {(status, task): self.add_run(f"{status} {task}", status, task)
               for status in statuses for task in tasks}
        self.add_run("Other project", "failed", other=True)
        expected = {**{status: 4 for status in statuses}, "all": 20, "active": 8}
        active = self.listing(page=1, status="active")
        self.assertEqual(active["pagination"]["total"], 8)
        self.assertEqual({run["status"] for run in active["runs"]}, {"queued", "running"})
        for task in tasks:
            result = self.listing(page=1, status="completed", task=task, q="COMPLETED")
            self.assertEqual([run["id"] for run in result["runs"]], [ids[("completed", task)]])
            self.assertEqual(result["pagination"]["total"], 1)
            self.assertEqual(result["total"], 20)
            self.assertEqual(result["counts"], expected)
        empty = self.listing(page=99, q="no such run")
        self.assertEqual(empty["runs"], [])
        self.assertEqual(empty["pagination"], {"page": 1, "page_size": 10, "total": 0, "total_pages": 1})
        self.assertEqual(empty["counts"], expected)

    def test_search_matches_name_or_model_and_treats_like_metacharacters_literally(self):
        percent = self.add_run("accuracy 90%")
        underscore = self.add_run("Trial", model="model_segment.pt")
        slash = self.add_run(r"folder\trial")
        self.add_run("accuracy 90x")
        self.add_run("Trial", model="modelXsegment.pt")
        self.add_run("folder/trial")
        for term, expected in (("%", percent), ("_", underscore), ("\\", slash), ("90%", percent), ("MODEL_SEGMENT", underscore)):
            with self.subTest(term=term):
                result = self.listing(page=1, q=term)
                self.assertEqual([run["id"] for run in result["runs"]], [expected])
        self.assertEqual(self.listing(page=1, q="' OR 1=1 --")["pagination"]["total"], 0)

    def test_locate_uses_filtered_order_and_cannot_reveal_other_projects(self):
        ids = [self.add_run(f"Trial {index}", status="failed" if index % 2 else "completed") for index in range(27)]
        other = self.add_run("Private", other=True)
        result = self.listing(page=1, locate_id=ids[0])
        self.assertTrue(result["locate_found"])
        self.assertEqual(result["pagination"]["page"], 3)
        self.assertEqual([run["id"] for run in result["runs"]], ids[:7][::-1])
        result = self.listing(page=1, page_size=5, status="failed", locate_id=ids[1])
        self.assertTrue(result["locate_found"])
        self.assertEqual(result["pagination"]["page"], 3)
        self.assertIn(ids[1], [run["id"] for run in result["runs"]])
        for target in (other, 999999, ids[1]):
            with self.subTest(target=target):
                result = self.listing(page=2, status="completed", locate_id=target)
                self.assertFalse(result["locate_found"])
                self.assertEqual(result["pagination"]["page"], 2)
                self.assertTrue(all(run["project_id"] == self.project for run in result["runs"]))

    def test_summary_is_lightweight_and_selects_only_project_history_without_writes(self):
        first = self.add_run("Most recently completed", finished_at="2026-01-03T00:00:00+00:00", artifact=True)
        second = self.add_run("Older completion", finished_at="2026-01-02T00:00:00+00:00", artifact=True)
        third = self.add_run("Waiting", "queued", artifact=True)
        fourth = self.add_run("Running", "running", artifact=True)
        self.add_run("Other completion", other=True, finished_at="2026-12-01T00:00:00+00:00", artifact=True)
        tables = ("training_runs", "training_run_logs", "training_metrics", "pipeline_resource_leases", "audit_logs")
        before = {table: all_rows(f"SELECT * FROM {table}") for table in tables}
        with (
            patch.object(training, "training_availability", side_effect=AssertionError("summary discovers models")),
            patch.object(training, "model_catalog", side_effect=AssertionError("summary loads model catalog")),
            patch.object(training, "serialize_training_run", side_effect=AssertionError("summary serializes full run")),
            patch.object(training, "_artifact_metadata", side_effect=AssertionError("summary reads artifacts")),
            patch.object(training, "_file_sha256", side_effect=AssertionError("summary hashes artifacts")),
        ):
            result = self.summary()
        self.assertEqual((result["project_id"], result["total"]), (self.project, 4))
        self.assertEqual(result["counts"], {"all": 4, "active": 2, "completed": 2, "queued": 1,
                                           "running": 1, "failed": 0, "cancelled": 0})
        self.assertEqual([run["id"] for run in result["recent"]], [fourth, third, second])
        self.assertEqual(result["latest_completed"]["id"], first)
        for run in [*result["recent"], result["latest_completed"]]:
            self.assertEqual(run["project_id"], self.project)
            self.assertEqual((run["current_epoch"], run["total_epochs"], run["progress"]), (10, 50, 0.2))
            self.assertNotIn("artifact_path", run)
            self.assertNotIn("settings_json", run)
        after = {table: all_rows(f"SELECT * FROM {table}") for table in tables}
        self.assertEqual(after, before)

    def test_paginated_list_only_checks_artifacts_for_rows_on_the_requested_page(self):
        ids = [self.add_run(f"Trial {index}", artifact=True) for index in range(25)]
        self.add_run("Other artifact", other=True, artifact=True)
        with patch.object(training, "_file_sha256", wraps=training._file_sha256) as hashes:
            result = self.listing(page=2)
        expected = ids[::-1][10:20]
        self.assertEqual([run["id"] for run in result["runs"]], expected)
        self.assertEqual([call.args[0].name for call in hashes.call_args_list], [f"run-{run}.pt" for run in expected])
        self.assertTrue(all(run["artifact"]["integrity"] == "verified" for run in result["runs"]))

    def test_empty_missing_project_invalid_filters_and_authentication(self):
        summary = self.summary()
        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["recent"], [])
        self.assertIsNone(summary["latest_completed"])
        self.assertTrue(all(value == 0 for value in summary["counts"].values()))
        self.assertEqual(self.listing(page=1)["pagination"]["total_pages"], 1)
        invalid = ({"include_availability": "bad"}, {"page": 0}, {"page": -1}, {"page": 2147483648}, {"page": "bad"},
                   {"page_size": 0}, {"page_size": 51}, {"status": "success"}, {"task": "obb"},
                   {"q": "x" * 201}, {"locate_id": 1}, {"page": 1, "locate_id": 0},
                   {"page": 1, "locate_id": 9223372036854775808})
        for params in invalid:
            with self.subTest(params=params):
                response = self.client.get(f"/api/projects/{self.project}/training-runs", params=params)
                self.assertEqual(response.status_code, 422, response.text)
        for endpoint in ("training-runs", "training-summary"):
            self.assertEqual(self.client.get(f"/api/projects/999999/{endpoint}").status_code, 404)
        object.__setattr__(settings, "auth_mode", "accounts")
        for endpoint in ("training-runs", "training-summary"):
            self.assertEqual(self.client.get(f"/api/projects/{self.project}/{endpoint}").status_code, 401)


if __name__ == "__main__":
    unittest.main()
