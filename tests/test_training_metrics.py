from __future__ import annotations

import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


class TrainingMetricsTest(unittest.TestCase):
    def setUp(self):
        global training, settings, db, one, all_rows, now_iso
        from app.config import settings
        from app.database import db, one, all_rows, now_iso
        training = importlib.import_module("app.training")
        main = importlib.import_module("app.main")
        self.temp = tempfile.TemporaryDirectory(prefix="mdp-training-metrics-")
        values = dict(data_dir=Path(self.temp.name), embedded_worker=False,
                      embedded_pipeline_worker=False, enable_demo_model=False, auth_mode="disabled")
        self.previous = {key: getattr(settings, key) for key in values}
        for key, value in values.items():
            object.__setattr__(settings, key, value)
        self.client = TestClient(main.app, headers={"X-MDP-Client": "browser"}).__enter__()
        self.model_path = Path(self.temp.name) / "stub.pt"
        self.model_path.write_bytes(b"no-model-execution")

    def tearDown(self):
        self.client.__exit__(None, None, None)
        for key, value in self.previous.items():
            object.__setattr__(settings, key, value)
        self.temp.cleanup()

    def fixture(self, task="detect", *, generation=1, current_epoch=0, status="running"):
        config = {"device": "cpu", "resource_profile": "balanced", "base_model_sha256": "test-hash",
                  "epochs": 3, "image_size": 64, "batch_size": 1, "workers": 0, "patience": 3, "amp": False}
        with db() as conn:
            project = conn.execute("INSERT INTO projects(name,task_type,created_at,updated_at) VALUES (?,?,?,?)",
                                   ("Metric fixture", task, now_iso(), now_iso())).lastrowid
            version = conn.execute("""INSERT INTO dataset_versions
                (project_id,version_number,name,train_ratio,valid_ratio,test_ratio,content_hash,
                 asset_count,annotation_count,task_type,created_by,created_at)
                VALUES (?,1,'metrics',0.8,0.2,0,'fixture',2,2,?,'test',?)""", (project, task, now_iso())).lastrowid
            run = conn.execute("""INSERT INTO training_runs
                (project_id,version_id,name,model_arch,task,status,total_epochs,settings_json,
                 claimed_by,claim_generation,current_epoch,created_by,created_at)
                VALUES (?,?,'metrics','stub.pt',?,?,3,?,'test-worker',?,?,'test',?)""",
                (project, version, task, status, json.dumps(config), generation, current_epoch, now_iso())).lastrowid
        return run

    def history(self, run):
        response = self.client.get(f"/api/training-runs/{run}/metrics")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def insert_history(self, run, snapshots):
        with db() as conn:
            conn.executemany("""INSERT INTO training_metrics(run_id,epoch,progress,metrics_json,created_at)
                VALUES (?,?,?,?,?)""", [(run, epoch, min(0.99, epoch / 3), json.dumps(metrics), now_iso())
                                        for epoch, metrics in snapshots])

    def execute_stub(self, run, task="detect", *, epochs=(0, 1), generation=1, before_epoch=None, on_start=None, resume=False):
        owner = self
        components = {
            "detect": ("box_loss", "cls_loss", "dfl_loss"),
            "segment": ("box_loss", "seg_loss", "cls_loss", "dfl_loss", "sem_loss"),
            "pose": ("box_loss", "pose_loss", "kobj_loss", "cls_loss", "dfl_loss", "rle_loss"),
            "classify": ("loss",),
        }[task]
        metric_key = {"detect": "metrics/mAP50-95(B)", "segment": "metrics/mAP50-95(M)",
                      "pose": "metrics/mAP50-95(P)", "classify": "metrics/accuracy_top1"}[task]
        observed = []

        class StubYOLO:
            def __init__(self, *args, **kwargs):
                self.callbacks = {}
                self.trainer = types.SimpleNamespace(epoch=-1, data={}, model=types.SimpleNamespace(), ema=None)

            def add_callback(self, event, callback):
                self.callbacks[event] = callback

            def train(self, **kwargs):
                owner.assertTrue(kwargs["val"])
                if on_start:
                    on_start()
                if "on_pretrain_routine_end" in self.callbacks:
                    self.callbacks["on_pretrain_routine_end"](self.trainer)
                def label_loss_items(values, prefix="train"):
                    values = [values] if isinstance(values, float) else values
                    return {f"{prefix}/{name}": value for name, value in zip(components, values)}
                self.trainer.label_loss_items = label_loss_items
                for epoch in epochs:
                    if before_epoch:
                        before_epoch(epoch)
                    self.trainer.epoch = epoch
                    values = [float(index + 1) / (epoch + 1) for index in range(len(components))]
                    self.trainer.tloss = values[0] if task == "classify" else values
                    self.trainer.metrics = {f"val/{name}": value * 1.5 for name, value in zip(components, values)}
                    self.trainer.metrics[metric_key] = (epoch + 1) / 10
                    self.trainer.fitness = (epoch + 1) / 10
                    self.callbacks["on_train_epoch_end"](self.trainer)
                    self.callbacks["on_fit_epoch_end"](self.trainer)
                    observed.append(owner.history(run))
                # Real Ultralytics invokes the same fit callback for final
                # best-model evaluation, now without per-epoch validation loss.
                self.trainer.metrics = {metric_key: 0.95}
                self.callbacks["on_fit_epoch_end"](self.trainer)
                best = Path(kwargs["project"]) / "ultralytics" / "weights" / "best.pt"
                best.parent.mkdir(parents=True, exist_ok=True)
                best.write_bytes(b"stub-trained-artifact")
                self.trainer.best = best

        def dataset(*args):
            root = Path(args[2]) / "dataset"
            root.mkdir()
            (root / "classes.json").write_text('[{"index":0,"name":"object"}]')
            (root / "data.yaml").write_text("path: .")
            return root if task == "classify" else root / "data.yaml"

        ultra = types.ModuleType("ultralytics")
        ultra.YOLO = StubYOLO
        engine = types.ModuleType("ultralytics.engine")
        trainer_module = types.ModuleType("ultralytics.engine.trainer")
        trainer_module.check_amp = lambda *_: False
        engine.trainer = trainer_module
        matplotlib = types.ModuleType("matplotlib")
        matplotlib.use = lambda *_args, **_kwargs: None
        torch = types.ModuleType("torch")
        torch.load = lambda *_args, **_kwargs: {"epoch": 0, "optimizer": {}, "train_args": {"epochs": 3}}
        torch.save = lambda _checkpoint, path: Path(path).write_bytes(b"resume-stub")
        if resume:
            old = settings.training_dir / f"run-{run}" / "attempt-1" / "ultralytics" / "weights" / "last.pt"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"checkpoint-stub")
        with (
            patch.dict(sys.modules, {"ultralytics": ultra, "ultralytics.engine": engine,
                                    "ultralytics.engine.trainer": trainer_module, "matplotlib": matplotlib, "torch": torch}),
            patch.object(training, "_acquire_resource_lease", return_value={"resolved_device": "cpu"}),
            patch.object(training, "_resolve_base_model", return_value=(self.model_path, {"sha256": "test-hash"})),
            patch.object(training, "_prepare_training_dataset", side_effect=dataset),
            # This fixture replaces both training and plotting libraries;
            # real offline font behavior is covered by test_open_source.
            patch("app.plot_fonts.prepare_training_fonts"),
        ):
            training.run_training(run, "test-worker", generation)
        return observed, components, metric_key

    def test_all_four_tasks_persist_live_epoch_loss_and_final_evaluation_cannot_overwrite_it(self):
        for task in ("detect", "segment", "pose", "classify"):
            with self.subTest(task=task):
                run = self.fixture(task)
                observed, components, key = self.execute_stub(run, task)
                self.assertEqual(len(observed), 2)
                for index, snapshot in enumerate(observed, 1):
                    self.assertEqual(snapshot["status"], "running")
                    self.assertEqual(snapshot["current_epoch"], index)
                    self.assertEqual(len(snapshot["history"]), index)
                    row = snapshot["history"][-1]
                    self.assertEqual(row["epoch"], index)
                    self.assertEqual(set(row["losses"]["train_components"]), set(components))
                    self.assertAlmostEqual(row["losses"]["train"], sum(range(1, len(components) + 1)) / index)
                    self.assertAlmostEqual(row["losses"]["val"], row["losses"]["train"] * 1.5)
                    self.assertEqual(row["metrics"][key], index / 10)
                final = self.history(run)
                self.assertEqual(final["status"], "completed", one("SELECT error FROM training_runs WHERE id=?", (run,)))
                self.assertEqual(final["history"], observed[-1]["history"])
                self.assertEqual(final["latest"][key], 0.95)
                self.assertEqual(final["latest_epoch_metrics"][key], 0.2)
                self.assertEqual(final["latest_losses"], observed[-1]["latest_losses"])
                self.assertEqual(one("SELECT COUNT(*) AS n FROM training_run_logs WHERE run_id=? AND event='epoch_end'", (run,))["n"], 2)

    def test_legacy_missing_and_non_finite_loss_are_not_fabricated_or_mislabeled(self):
        run = self.fixture()
        self.insert_history(run, [(1, {"map50": 0.1}),
                                  (2, {"train_loss": [1.0, 2.0, 3.0], "val/box_loss": 0.0}),
                                  (3, {"train_loss": [1.0, None], "val/box_loss": float("nan"), "val/cls_loss": 2.0})])
        before = all_rows("SELECT * FROM training_metrics WHERE run_id=?", (run,))
        result = self.history(run)
        first, second, third = result["history"]
        self.assertIsNone(first["losses"]["train"])
        self.assertIsNone(first["losses"]["val"])
        self.assertEqual(second["losses"]["train"], 6.0)
        self.assertEqual(second["losses"]["train_components"], {"component_1": 1.0, "component_2": 2.0, "component_3": 3.0})
        self.assertEqual(second["losses"]["val"], 0.0)
        self.assertIsNone(third["losses"]["train"])
        self.assertIsNone(third["losses"]["val"])
        self.assertIsNone(third["metrics"]["val/box_loss"])
        self.assertEqual(all_rows("SELECT * FROM training_metrics WHERE run_id=?", (run,)), before)

    def test_empty_history_remains_empty_and_reports_current_state(self):
        run = self.fixture(status="queued")
        result = self.history(run)
        self.assertEqual((result["status"], result["current_epoch"], result["total_epochs"]), ("queued", 0, 3))
        self.assertEqual(result["history"], [])
        self.assertIsNone(result["latest_epoch"])
        self.assertIsNone(result["latest_losses"]["train"])
        self.assertIsNone(result["latest_losses"]["val"])
        self.assertEqual(self.client.get("/api/training-runs/999999/metrics").status_code, 404)

    def test_restart_without_checkpoint_removes_old_curve_before_first_new_epoch(self):
        run = self.fixture(generation=2, current_epoch=3)
        self.insert_history(run, [(1, {"train_loss": 99}), (2, {"train_loss": 98}), (3, {"train_loss": 97})])
        def started():
            result = self.history(run)
            self.assertEqual((result["history"], result["current_epoch"], result["latest"]), ([], 0, {}))
        self.execute_stub(run, epochs=(0,), generation=2, on_start=started)
        result = self.history(run)
        self.assertEqual([row["epoch"] for row in result["history"]], [1])
        self.assertEqual(result["latest_losses"]["train"], 6)

    def test_checkpoint_resume_keeps_only_curve_prefix_represented_by_checkpoint(self):
        run = self.fixture(generation=2, current_epoch=3)
        self.insert_history(run, [(1, {"train_loss": 99}), (2, {"train_loss": 98}), (3, {"train_loss": 97})])
        def started():
            result = self.history(run)
            self.assertEqual([row["epoch"] for row in result["history"]], [1])
            self.assertEqual(result["current_epoch"], 1)
        self.execute_stub(run, epochs=(1, 2), generation=2, on_start=started, resume=True)
        result = self.history(run)
        self.assertEqual([row["epoch"] for row in result["history"]], [1, 2, 3])
        self.assertEqual(result["history"][0]["losses"]["train"], 99)
        self.assertEqual(result["history"][1]["losses"]["train"], 3)
        self.assertEqual(result["history"][2]["losses"]["train"], 2)

    def test_stale_worker_cannot_append_or_trim_current_metrics(self):
        run = self.fixture()
        def change_owner(epoch):
            if epoch == 1:
                with db() as conn:
                    conn.execute("UPDATE training_runs SET claimed_by='new-worker',claim_generation=2 WHERE id=?", (run,))
        self.execute_stub(run, before_epoch=change_owner)
        result = self.history(run)
        self.assertEqual([row["epoch"] for row in result["history"]], [1])
        self.assertEqual(result["status"], "running")
        training._trim_epoch_history(run, "test-worker", 1, 0)
        self.assertEqual(self.history(run)["history"], result["history"])


if __name__ == "__main__":
    unittest.main()
