from __future__ import annotations

import hashlib
import importlib
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from fastapi.testclient import TestClient


class ConversionTest(unittest.TestCase):
    def setUp(self):
        global settings, database, training, support
        from app.config import settings
        from app import database
        training = importlib.import_module("app.training")
        support = importlib.import_module("app.conversion_support")
        self.temp = tempfile.TemporaryDirectory(prefix="mdp-conversions-")
        changes = {"data_dir": Path(self.temp.name), "embedded_worker": False, "embedded_pipeline_worker": False, "auth_mode": "disabled", "enable_demo_model": False}
        self.previous = {name: getattr(settings, name) for name in changes}
        for name, value in changes.items(): object.__setattr__(settings, name, value)
        self.main = importlib.import_module("app.main")
        self.client = TestClient(self.main.app, headers={"X-MDP-Client": "browser"}).__enter__()
        self.available = patch.object(training, "training_availability", return_value={"conversion_formats": {name: {"available": True, "reason": ""} for name in ("onnx", "engine", "rknn", "torchscript")}, "cuda_available": True})
        self.resources = patch.object(training, "training_resources", return_value={"cuda_available": True, "devices": [{"id": "cpu", "available": True}, {"id": "cuda:0", "available": True}]})
        self.available.start(); self.resources.start()
        self.project = self.client.post("/api/projects", json={"name": "Conversion fixture", "labels": ["object"]}).json()["id"]
        for index in range(6):
            image = Image.new("RGB", (80, 40), (20 + index * 20, 100, 180)); stream = io.BytesIO(); image.save(stream, "PNG")
            response = self.client.post(f"/api/projects/{self.project}/assets", files={"files": (f"sample-{index}.png", stream.getvalue(), "image/png")})
            self.assertEqual(response.status_code, 201, response.text)
        response = self.client.post(f"/api/projects/{self.project}/versions", json={"name": "Frozen calibration", "approved_only": False, "train_ratio": .7, "valid_ratio": .2, "test_ratio": .1})
        self.assertEqual(response.status_code, 201, response.text)
        self.version = response.json()["id"]
        source = settings.training_dir / "fixture.pt"; source.write_bytes(b"fixture checkpoint")
        with database.db() as conn:
            self.run = conn.execute("""INSERT INTO training_runs(project_id,version_id,name,model_arch,task,status,total_epochs,artifact_path,created_by,created_at)
                VALUES (?,?,'Source','fixture.pt','detect','completed',1,?,'test',?)""", (self.project, self.version, str(source), database.now_iso())).lastrowid

    def tearDown(self):
        self.main.app.dependency_overrides.clear()
        self.available.stop(); self.resources.stop(); self.client.__exit__(None, None, None)
        for name, value in self.previous.items(): object.__setattr__(settings, name, value)
        self.temp.cleanup()

    def create(self, format="rknn", **extra):
        payload = {"format": format, "image_size": 32, **({"rknn": {"target_platform": "rv1126b"}} if format == "rknn" else {}), **extra}
        response = self.client.post(f"/api/training-runs/{self.run}/conversions", json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def claim(self, item):
        with database.db() as conn:
            conn.execute("UPDATE model_conversions SET status='running',claimed_by='test-worker',claim_generation=7 WHERE id=?", (item["id"],))

    def execute(self, item, runner):
        self.claim(item)
        with patch.object(training, "_acquire_resource_lease", return_value={"resolved_device": "cpu"}), patch.object(support, "run_converter_process", side_effect=runner):
            training.run_conversion(item["id"], "test-worker", 7)
        return self.client.get(f"/api/conversions/{item['id']}").json()

    def test_schema_rejects_incompatible_settings(self):
        bad = [dict(format="rknn"), dict(format="rknn", rknn={"target_platform": "rv1126"}),
               dict(format="rknn", rknn={"target_platform": "rv1106"}), dict(format="rknn", rknn={}, dynamic=True),
               dict(format="rknn", rknn={}, half=True), dict(format="rknn", rknn={}, batch=2),
               dict(format="rknn", rknn={"std_values": [0, 255, 255]}), dict(format="rknn", rknn={"input_color": "bgr"}),
               dict(format="rknn", rknn={"calibration_count": True}), dict(format="onnx", workspace=2),
               dict(format="onnx", rknn={}), dict(format="onnx", image_size=33),
               dict(format="engine", device="cpu"), dict(format="engine", workspace=0), dict(format="engine", batch=65)]
        for payload in bad:
            with self.subTest(payload=payload):
                response = self.client.post(f"/api/training-runs/{self.run}/conversions", json=payload)
                self.assertEqual(response.status_code, 422, response.text)

    def test_options_and_quantization_count_use_training_snapshot(self):
        options = self.client.get(f"/api/training-runs/{self.run}/conversion-options").json()
        count = len(support.calibration_assets(self.version))
        self.assertEqual(options["calibration"]["available_count"], count)
        self.assertEqual(options["calibration"]["version_id"], self.version)
        response = self.client.post(f"/api/training-runs/{self.run}/conversions", json={"format": "rknn", "rknn": {"quantized": True, "calibration_count": count + 1}})
        self.assertEqual(response.status_code, 422)
        item = self.create(rknn={"quantized": True, "calibration_count": count})
        self.assertEqual(item["resource"]["requested_device"], "cpu")

    def test_annotation_role_cannot_enqueue(self):
        from app.auth import Actor, get_actor
        self.main.app.dependency_overrides[get_actor] = lambda: Actor("test", "annotator")
        response = self.client.post(f"/api/training-runs/{self.run}/conversions", json={"format": "onnx"})
        self.assertEqual(response.status_code, 403)

    def test_frozen_calibration_survives_original_deletion_and_detects_tampering(self):
        with database.db() as conn: conn.execute("DELETE FROM assets WHERE project_id=?", (self.project,))
        for path in settings.media_dir.rglob("*"):
            if path.is_file(): path.unlink()
        attempt = Path(self.temp.name) / "calibration-attempt"; attempt.mkdir()
        path, manifest = support.prepare_calibration(self.version, "detect", 32, 2, attempt, lambda: None)
        self.assertEqual(manifest["count"], 2)
        train_ids = {item["id"] for item in support.calibration_assets(self.version)}
        self.assertTrue(set(manifest["version_asset_ids"]).issubset(train_ids))
        for filename in path.read_text().splitlines():
            with Image.open(filename) as image:
                self.assertEqual(image.size, (32, 32)); self.assertEqual(image.mode, "RGB")
                self.assertEqual(image.getpixel((0, 0)), (114, 114, 114))
        row = support.calibration_assets(self.version)[0]
        frozen = settings.versions_dir / f"version-{self.version}" / row["storage_name"]
        frozen.chmod(0o644); frozen.write_bytes(b"damaged snapshot")
        other = Path(self.temp.name) / "damaged-attempt"; other.mkdir()
        with self.assertRaisesRegex(RuntimeError, "校验失败"):
            support.prepare_calibration(self.version, "detect", 32, 1, other, lambda: None)

    def test_rknn_worker_stages_artifact_manifest_and_download(self):
        calls = []
        def runner(python, request, attempt, stage, check, log):
            check(); calls.append(request); Path(request["output"]).write_bytes(b"RKNNstub")
            return {"artifact": request["output"], "metadata": {"format": "rknn", "preprocessing": request.get("preprocessing"), "compatibility": {"verified_on_target": False}}}
        item = self.create(rknn={"target_platform": "rv1126b", "quantized": True, "calibration_count": 2})
        result = self.execute(item, runner)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual([item["operation"] for item in calls], ["ultralytics", "rknn"])
        self.assertEqual(calls[0]["kwargs"]["batch"], 1); self.assertFalse(calls[0]["kwargs"]["end2end"])
        self.assertTrue(Path(calls[1]["dataset_path"]).is_file())
        self.assertTrue(result["manifest_available"])
        manifest = self.client.get(result["manifest_url"])
        self.assertEqual(manifest.status_code, 200)
        self.assertEqual(manifest.json()["deployment"]["calibration"]["count"], 2)
        self.assertNotIn(str(settings.data_dir), manifest.text)
        download = self.client.get(result["artifact"]["download_url"])
        self.assertEqual(download.content, b"RKNNstub"); self.assertIn(".rknn", download.headers["content-disposition"])

    def test_tensorrt_real_export_arguments_are_preserved(self):
        calls = []
        def runner(python, request, *args):
            calls.append(request); Path(request["output"]).write_bytes(b"engine stub")
            return {"artifact": request["output"], "metadata": {"format": "engine"}}
        item = self.create("engine", batch=4, workspace=2.5, dynamic=True, half=True, device="cuda:0", simplify=False)
        result = self.execute(item, runner)
        self.assertEqual(result["status"], "completed")
        self.assertEqual({key: calls[0]["kwargs"][key] for key in ("batch", "workspace", "dynamic", "half", "simplify")}, dict(batch=4, workspace=2.5, dynamic=True, half=True, simplify=False))
        # A fixed TopK traced by an end-to-end head can exceed the number of
        # candidates at smaller dynamic sizes. The deployment app performs NMS.
        self.assertFalse(calls[0]["kwargs"]["end2end"])
        self.assertFalse(calls[0]["kwargs"]["nms"])

    def test_retry_keeps_rknn_config_and_rejects_gpu_override(self):
        item = self.create(rknn={"target_platform": "rv1126b", "quantized": True, "calibration_count": 2, "quantized_algorithm": "mmse"})
        self.client.post(f"/api/conversions/{item['id']}/cancel")
        bad = self.client.post(f"/api/conversions/{item['id']}/retry", json={"device": "cuda:0"})
        self.assertEqual(bad.status_code, 422)
        response = self.client.post(f"/api/conversions/{item['id']}/retry")
        self.assertEqual(response.status_code, 202, response.text)
        retry = response.json(); self.assertEqual(retry["settings"]["rknn"], item["settings"]["rknn"])
        self.assertEqual(retry["parent_conversion_id"], item["id"])

    def test_cancel_and_lost_generation_never_publish(self):
        for lost in (False, True):
            with self.subTest(lost=lost):
                item = self.create("onnx")
                def runner(python, request, *args):
                    Path(request["output"]).write_bytes(b"unpublished")
                    with database.db() as conn:
                        conn.execute("UPDATE model_conversions SET claimed_by='next',claim_generation=8 WHERE id=?" if lost else "UPDATE model_conversions SET cancel_requested=1 WHERE id=?", (item["id"],))
                    return {"artifact": request["output"], "metadata": {}}
                result = self.execute(item, runner)
                self.assertEqual(result["status"], "running" if lost else "cancelled")
                self.assertIsNone(result["artifact"]); self.assertFalse(result["manifest_available"])

    def test_cancel_between_hashing_and_publication_is_atomic(self):
        item = self.create("onnx")
        original = training._build_artifact_metadata
        def metadata(*args, **kwargs):
            result = original(*args, **kwargs)
            with database.db() as conn: conn.execute("UPDATE model_conversions SET cancel_requested=1 WHERE id=?", (item["id"],))
            return result
        def runner(python, request, *args):
            Path(request["output"]).write_bytes(b"unpublished")
            return {"artifact": request["output"], "metadata": {}}
        with patch.object(training, "_build_artifact_metadata", side_effect=metadata): result = self.execute(item, runner)
        self.assertEqual(result["status"], "cancelled"); self.assertIsNone(result["artifact"])

    def test_converter_subprocess_is_stopped_on_cancel_or_lost_claim(self):
        executable = Path(self.temp.name) / "slow-converter"
        executable.write_text("#!/usr/bin/env python3\nimport os,time\nprint('PID='+str(os.getpid()),flush=True)\nprint('\\x1b[34m转换进度 10%\\x1b[0m\\r转换进度 20%',flush=True)\ntime.sleep(30)\n")
        executable.chmod(0o755)
        for lost in (False, True):
            start = time.monotonic(); output = []; attempt = Path(self.temp.name) / f"process-{lost}"; attempt.mkdir()
            def check():
                if time.monotonic() - start > .4: raise support.ConversionStopped(lost)
            with self.assertRaises(support.ConversionStopped): support.run_converter_process(str(executable), {}, attempt, "test", check, output.append)
            self.assertLess(time.monotonic() - start, 5)
            self.assertIn("转换进度 10%", output); self.assertIn("转换进度 20%", output)
            self.assertNotIn("\x1b", "".join(output)); self.assertNotIn("\r", "".join(output))
            pid = int(next(line[4:] for line in output if line.startswith("PID=")))
            with self.assertRaises(ProcessLookupError): os.kill(pid, 0)

    def test_exited_converter_cannot_leave_descendant_holding_stdout(self):
        executable = Path(self.temp.name) / "orphaning-converter"
        executable.write_text("#!/usr/bin/env python3\nimport subprocess,sys\nchild=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)'])\nprint('CHILD='+str(child.pid),flush=True)\nsys.exit(2)\n")
        executable.chmod(0o755)
        output = []; attempt = Path(self.temp.name) / "orphan-process"; attempt.mkdir()
        start = time.monotonic()
        def check():
            if time.monotonic() - start > 3: raise support.ConversionStopped()
        with self.assertRaisesRegex(RuntimeError, "转换工具执行失败"):
            support.run_converter_process(str(executable), {}, attempt, "test", check, output.append)
        self.assertLess(time.monotonic() - start, 3)
        pid = int(next(line[6:] for line in output if line.startswith("CHILD=")))
        process_status = Path(f"/proc/{pid}/stat")
        if process_status.exists():
            self.assertEqual(process_status.read_text().split()[2], "Z", "Descendant is still running")


class ConversionMigrationTest(unittest.TestCase):
    def setUp(self):
        from app import database
        self.database = database
        self.temp = tempfile.TemporaryDirectory(prefix="mdp-conversion-migration-")
        self.path = Path(self.temp.name) / "old.sqlite"
        with sqlite3.connect(self.path) as conn:
            conn.executescript(database.SCHEMA.replace("'engine', 'rknn'", "'engine'"))
            conn.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES(1,'Old','date','date')")
            conn.execute("""INSERT INTO dataset_versions(id,project_id,version_number,name,train_ratio,valid_ratio,test_ratio,content_hash,asset_count,annotation_count,created_by,created_at)
                VALUES(1,1,1,'Old',1,0,0,'hash',1,1,'test','date')""")
            conn.execute("INSERT INTO training_runs(id,project_id,version_id,name,model_arch,total_epochs,created_by,created_at) VALUES(1,1,1,'Run','old.pt',1,'test','date')")
            for id,parent in ((10,None),(11,10),(99,None)):
                conn.execute("INSERT INTO model_conversions(id,training_run_id,format,parent_conversion_id,root_conversion_id,attempt,created_by,created_at) VALUES(?,1,'onnx',?,10,?,'test','date')", (id,parent,1 if parent else 0))
            conn.execute("DELETE FROM model_conversions WHERE id=99")
            conn.execute("INSERT INTO conversion_logs(conversion_id,level,event,message,created_at) VALUES(10,'info','old','keep me','date')")
            conn.execute("INSERT INTO conversion_logs(conversion_id,level,event,message,created_at) VALUES(11,'info','child','keep child','date')")

    def tearDown(self): self.temp.cleanup()

    def test_lossless_constraint_migration_sequence_foreign_keys_and_idempotence(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("PRAGMA foreign_keys=OFF"); conn.execute("BEGIN IMMEDIATE")
            self.database._migrate_conversion_formats(conn)
            self.database._migrate_conversion_formats(conn)
            self.assertEqual(conn.execute("SELECT id,parent_conversion_id,root_conversion_id FROM model_conversions ORDER BY id").fetchall(), [(10,None,10),(11,10,10)])
            self.assertEqual(conn.execute("SELECT message FROM conversion_logs ORDER BY id").fetchall(), [("keep me",),("keep child",)])
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            id = conn.execute("INSERT INTO model_conversions(training_run_id,format,created_by,created_at) VALUES(1,'rknn','test','date')").lastrowid
            self.assertEqual(id, 100)
            self.assertIsNotNone(conn.execute("SELECT name FROM sqlite_master WHERE name='idx_conversions_run_status'").fetchone())

    def test_failure_mid_rebuild_rolls_back_original_rows_and_schema(self):
        class FailingConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql.startswith("ALTER TABLE model_conversions_rknn RENAME"):
                    raise sqlite3.OperationalError("injected rename failure")
                return super().execute(sql, *args)
        with self.assertRaises(sqlite3.OperationalError):
            with sqlite3.connect(self.path, factory=FailingConnection) as conn:
                conn.execute("PRAGMA foreign_keys=OFF"); conn.execute("BEGIN IMMEDIATE")
                self.database._migrate_conversion_formats(conn)
        with sqlite3.connect(self.path) as conn:
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='model_conversions'").fetchone()[0]
            self.assertNotIn("'rknn'", sql)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM model_conversions").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversion_logs").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT seq FROM sqlite_sequence WHERE name='model_conversions'").fetchone()[0], 99)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_empty_table_retains_deleted_id_high_water_mark(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM conversion_logs"); conn.execute("DELETE FROM model_conversions")
        with sqlite3.connect(self.path) as conn:
            conn.execute("PRAGMA foreign_keys=OFF"); conn.execute("BEGIN IMMEDIATE")
            self.database._migrate_conversion_formats(conn)
            self.assertEqual(conn.execute("SELECT seq FROM sqlite_sequence WHERE name='model_conversions'").fetchone()[0], 99)
            self.assertEqual(conn.execute("INSERT INTO model_conversions(training_run_id,format,created_by,created_at) VALUES(1,'rknn','test','date')").lastrowid, 100)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_init_db_applies_migration_once_and_preserves_all_values(self):
        from app.config import settings
        isolated = replace(settings, data_dir=Path(self.temp.name))
        with sqlite3.connect(self.path) as source, sqlite3.connect(isolated.db_path) as destination:
            source.backup(destination)
        with sqlite3.connect(isolated.db_path) as conn:
            original = conn.execute("SELECT * FROM model_conversions ORDER BY id").fetchall()
            logs = conn.execute("SELECT * FROM conversion_logs ORDER BY id").fetchall()
        with patch.object(self.database, "settings", isolated):
            self.database.init_db(); self.database.init_db()
        with sqlite3.connect(isolated.db_path) as conn:
            self.assertEqual(conn.execute("SELECT * FROM model_conversions ORDER BY id").fetchall(), original)
            self.assertEqual(conn.execute("SELECT * FROM conversion_logs ORDER BY id").fetchall(), logs)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=9").fetchone()[0], 1)
            self.assertEqual(conn.execute("INSERT INTO model_conversions(training_run_id,format,created_by,created_at) VALUES(1,'rknn','test','date')").lastrowid, 100)

    def test_foreign_key_verification_failure_rolls_back_rebuild(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO conversion_logs(conversion_id,level,event,message,created_at) VALUES(999,'info','invalid','preexisting invalid reference','date')")
        with self.assertRaisesRegex(RuntimeError, "Foreign-key verification failed"):
            with sqlite3.connect(self.path) as conn:
                conn.execute("PRAGMA foreign_keys=OFF"); conn.execute("BEGIN IMMEDIATE")
                self.database._migrate_conversion_formats(conn)
        with sqlite3.connect(self.path) as conn:
            self.assertNotIn("'rknn'", conn.execute("SELECT sql FROM sqlite_master WHERE name='model_conversions'").fetchone()[0])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM model_conversions").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM conversion_logs").fetchone()[0], 3)
            self.assertEqual(conn.execute("SELECT seq FROM sqlite_sequence WHERE name='model_conversions'").fetchone()[0], 99)
