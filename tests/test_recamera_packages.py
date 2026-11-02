"""Exercise the real pinned App Center builder and fail-closed model contracts."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import recamera_packages as packages


def model_fixture():
    deployment = {
        "target_platform": "rv1126b", "task": "detect", "precision": "int8",
        "labels": [{"name": "person", "category_index": 0}],
        "source_onnx": {"inputs": [{"shape": [1, 3, 640, 640], "dtype": "FLOAT"}],
                        "outputs": [{"shape": [1, 5, 8400]}], "contains_non_max_suppression": False},
        "postprocessing": {"family": "ultralytics_original_graph", "end2end": False,
                           "nms_in_graph": False, "box_format": "xywh"},
        "preprocessing": {"input_color": "rgb", "mean_values": [0, 0, 0], "std_values": [255, 255, 255],
                          "normalization_location": "rknn_input_preprocessing", "padding_value": 114,
                          "auto_padding": False, "resize": "letterbox", "image_size": [640, 640]},
    }
    conversion = {"id": 9, "status": "completed", "format": "rknn",
                  "artifact_metadata_json": json.dumps({"sha256": "1" * 64, "deployment": deployment})}
    run = {"id": 7, "task": "detect", "artifact_metadata_json": json.dumps({"model_arch": "yolo26"})}
    return conversion, run, deployment


def minimal_manifest():
    return {
        "manifest_version": 2, "id": "cvsprout-inference", "name": "CVSprout test package",
        "version": "0.1.0", "type": "self-hosted", "entry": "app.py",
        "release": {"sequence": 1, "channel": "stable"},
        "compatibility": {"platform_profile": "rv1126b-linux-gnu-cp311-rknn232-v1", "arch": "aarch64",
                          "python": "==3.11.*", "kit_api": ">=0.3,<1", "sdk_abi": ">=1.3,<2"},
        "python": {"runtime_profile": "recamera-ai-cp311-v1", "isolation": "per-release", "wheels": [], "imports": []},
        "artifacts": [], "models": [], "capabilities": [],
        "config_schema": {"revision": 1, "groups": []},
        "resources": {"claims": [], "limits": {"memory_mb": 128}},
        "permissions": {"sdk": [], "filesystem": {"read": ["app"], "write": ["appdata", "tmp"]},
                        "network": {"listen": [], "outbound": []}},
        "health": {"protocol": "kit-health-v1", "startup_timeout_sec": 30, "stabilization_sec": 2,
                   "liveness_interval_sec": 10, "liveness_failures": 3,
                   "restart": {"policy": "on-failure", "max_attempts": 3, "window_sec": 60, "backoff_sec": [1, 2, 5]}},
        "instances": {"max": 1, "config_scope": "app", "data_scope": "app", "endpoint_mode": "allocated"},
    }


class ModelContractTest(unittest.TestCase):
    def test_yolo26_decoded_output_admitted_by_tensor_contract(self):
        conversion, run, _ = model_fixture()
        contract = packages.model_contract(conversion, run)
        self.assertEqual(contract["classes"], ["person"])
        self.assertEqual(contract["input_size"], 640)
        self.assertEqual(contract["source_output_shape"], [1, 5, 8400])
        self.assertEqual(contract["output_layout"], "yolo_decoded_xywh")
        self.assertFalse(contract["board_validated"])
        self.assertEqual(contract["training_run_id"], 7)

    def assert_rejects(self, update):
        conversion, run, deployment = model_fixture()
        update(conversion, run, deployment)
        metadata = json.loads(conversion["artifact_metadata_json"])
        metadata["deployment"] = deployment
        conversion["artifact_metadata_json"] = json.dumps(metadata)
        with self.assertRaises(ValueError):
            packages.model_contract(conversion, run)

    def test_wrong_chip_incomplete_conversion_and_wrong_task_rejected(self):
        for update in (
            lambda c, r, d: d.update(target_platform="rk3576"),
            lambda c, r, d: c.update(status="running"),
            lambda c, r, d: c.update(format="onnx"),
            lambda c, r, d: r.update(task="segment"),
            lambda c, r, d: d.update(task="classify"),
        ):
            with self.subTest(update=update):
                self.assert_rejects(update)

    def test_end_to_end_nms_raw_or_transposed_tensor_rejected(self):
        for update in (
            lambda c, r, d: d["postprocessing"].update(end2end=True),
            lambda c, r, d: d["postprocessing"].update(nms_in_graph=True),
            lambda c, r, d: d["postprocessing"].update(box_format="xyxy"),
            lambda c, r, d: d["postprocessing"].update(family="rtdetr"),
            lambda c, r, d: d["source_onnx"].update(contains_non_max_suppression=True),
            lambda c, r, d: d["source_onnx"].update(outputs=[{"shape": [1, 8400, 5]}]),
            lambda c, r, d: d["source_onnx"].update(outputs=[{"shape": [1, 6, 8400]}]),
            lambda c, r, d: d["source_onnx"].update(outputs=[{"shape": [1, 5, "anchors"]}]),
            lambda c, r, d: d["source_onnx"].update(outputs=[{"shape": [1, 64, 80, 80]}, {"shape": [1, 1, 80, 80]}]),
        ):
            with self.subTest(update=update):
                self.assert_rejects(update)

    def test_preprocessing_dynamic_inputs_and_class_contract_rejected(self):
        for update in (
            lambda c, r, d: d["preprocessing"].update(input_color="bgr"),
            lambda c, r, d: d["preprocessing"].update(normalization_location="application"),
            lambda c, r, d: d["preprocessing"].update(std_values=[1, 1, 1]),
            lambda c, r, d: d["preprocessing"].update(padding_value=0),
            lambda c, r, d: d["preprocessing"].update(auto_padding=True),
            lambda c, r, d: d["preprocessing"].update(resize="stretch"),
            lambda c, r, d: d["preprocessing"].update(image_size=[320, 320]),
            lambda c, r, d: d["source_onnx"].update(inputs=[{"shape": [1, 3, "height", "width"]}]),
            lambda c, r, d: d["source_onnx"].update(inputs=[{"shape": [1, 3, 640, 480]}]),
            lambda c, r, d: d["source_onnx"].update(inputs=[{"shape": [2, 3, 640, 640]}]),
            lambda c, r, d: d.update(labels=[]),
            lambda c, r, d: d.update(labels=[{"name": "cat\ndog", "category_index": 0}]),
            lambda c, r, d: d.update(labels=[{"name": "person", "category_index": 1}]),
        ):
            with self.subTest(update=update):
                self.assert_rejects(update)


class OfficialPackageTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cvsprout-official-package-test-")
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.manifest = minimal_manifest()
        (self.source / "manifest.json").write_text(json.dumps(self.manifest))
        (self.source / "app.py").write_text("from kit.app import App\nclass PackageTestApp(App):\n    pass\nAPP = PackageTestApp\n")

    def tearDown(self):
        self.temp.cleanup()

    def build(self):
        return packages.build_package(self.source, self.root / "dist")

    def rewrite(self, path, replacement=None, extra=None):
        """Construct intentionally damaged archives only for rejection tests."""
        rewritten = self.root / "damaged.tar.gz"
        with tarfile.open(path, "r:gz") as source, tarfile.open(rewritten, "w:gz") as output:
            for member in source:
                data = source.extractfile(member).read()
                if replacement and member.name in replacement:
                    data = replacement[member.name]
                    member.size = len(data)
                output.addfile(member, io.BytesIO(data))
            if extra:
                member, data = extra
                output.addfile(member, io.BytesIO(data) if data is not None else None)
        return rewritten

    def test_real_official_builder_produces_verified_release_and_hash_inventory(self):
        archive = self.build()
        inspected = packages.inspect_archive(archive)
        self.assertEqual(inspected["manifest"], self.manifest)
        self.assertEqual(inspected["sha256"], hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertEqual(inspected["size"], archive.stat().st_size)
        self.assertTrue(inspected["release_id"])
        with tarfile.open(archive) as bundle:
            self.assertTrue({"manifest.json", "release.lock.json", "files.sha256", "app.py"}.issubset(bundle.getnames()))

    def test_capture_download_template_runs_without_device_sqlite_modules(self):
        # Exercise the actual template shipped by the server, not an SDK checkout
        # or the host's complete standard library. App Center firmware may omit
        # sqlite3 and its native extension even though both exist on a developer PC.
        archive = packages.build_package(
            packages.ROOT / "templates/cvsprout-capture", self.root / "capture-dist")
        inspected = packages.inspect_archive(archive)
        self.assertEqual(inspected["manifest"]["id"], "cvsprout-capture")
        self.assertEqual(inspected["manifest"]["name"], "CVSprout Connector")
        self.assertEqual(inspected["manifest"]["version"], "1.1.0")
        self.assertEqual(inspected["manifest"]["release"]["sequence"], 4)
        self.assertEqual(inspected["manifest"]["models"], [])
        self.assertEqual(inspected["manifest"]["artifacts"], [])
        self.assertFalse(any("npu" in claim["name"].lower() for claim in inspected["manifest"]["resources"]["claims"]))
        payload = self.root / "capture-payload"
        with tarfile.open(archive, "r:gz") as bundle:
            self.assertIn("system_models.py", bundle.getnames())
            bundle.extractall(payload, filter="data")
        probe = r'''
import importlib
import sys
from pathlib import Path

sys.modules["sqlite3"] = None
sys.modules["_sqlite3"] = None
for name in ("sqlite3", "_sqlite3"):
    try:
        importlib.import_module(name)
    except ModuleNotFoundError:
        pass
    else:
        raise AssertionError("sqlite module blocker not active")
sys.path.insert(0, sys.argv[1])
from transport import Spool
import system_models
assert system_models.APP_VERSION == "1.1.0"
assert system_models.ModelDeploymentWorker
assert sys.modules["sqlite3"] is None and sys.modules["_sqlite3"] is None
path = Path(sys.argv[2])
queue = Spool(path, max_bytes=1024, max_images=2)
capture_id = queue.enqueue(b"capture-payload", "original-device", "2026-11-02T00:00:00Z")
assert capture_id
del queue
queue = Spool(path, max_bytes=1024, max_images=2)
assert queue.oldest("different-device") is None
capture = queue.oldest("original-device")
assert capture["capture_id"] == capture_id
assert capture["jpeg"] == b"capture-payload"
queue.acknowledge(capture, {"received": True, "capture_id": capture_id, "asset_id": 1})
assert queue.stats() == {"pending_images": 0, "pending_bytes": 0}
'''
        result = subprocess.run(
            [sys.executable, "-I", "-c", probe, str(payload), str(self.root / "capture-spool")],
            capture_output=True, text=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_public_capture_download_route_builds_current_connector(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.auth import Actor, get_actor
        from app.edge_deployment import router

        # Exercise the actual public route and official builder, with an isolated
        # authenticated app so no production database or worker is started.
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_actor] = lambda: Actor("package-test", "admin")
        with TestClient(app) as client:
            response = client.get("/api/edge/apps/capture/download")
        self.assertEqual(response.status_code, 200, response.text if response.status_code != 200 else "")
        self.assertEqual(response.headers["content-type"], "application/gzip")
        self.assertIn("1.1.0", response.headers["content-disposition"])
        archive = self.root / "downloaded-connector.tar.gz"
        archive.write_bytes(response.content)
        inspected = packages.inspect_archive(archive)
        self.assertEqual(inspected["manifest"]["id"], "cvsprout-capture")
        self.assertEqual(inspected["manifest"]["version"], "1.1.0")
        self.assertEqual(inspected["manifest"]["release"]["sequence"], 4)
        with tarfile.open(archive, "r:gz") as bundle:
            self.assertTrue({"system_models.py", "transport.py", "app.py", "release.lock.json", "files.sha256"}.issubset(bundle.getnames()))

    def test_hash_mismatch_duplicate_traversal_and_symlink_rejected_without_extracting(self):
        archive = self.build()
        damaged = self.rewrite(archive, replacement={"app.py": b"print('tampered')\n"})
        with self.assertRaises(ValueError):
            packages.inspect_archive(damaged)
        for name, kind in (("app.py", tarfile.REGTYPE), ("../escaped.txt", tarfile.REGTYPE), ("link", tarfile.SYMTYPE)):
            member = tarfile.TarInfo(name)
            member.type = kind
            member.linkname = "app.py" if kind == tarfile.SYMTYPE else ""
            data = b"" if kind == tarfile.REGTYPE else None
            damaged = self.rewrite(archive, extra=(member, data))
            with self.subTest(name=name), self.assertRaises(ValueError):
                packages.inspect_archive(damaged)
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_tampered_official_builder_is_rejected_before_subprocess(self):
        with patch.object(packages, "sha256", return_value="0" * 64), patch.object(packages.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "完整性"):
                self.build()
            run.assert_not_called()

    def test_model_staging_uses_bound_artifact_labels_input_size_and_digest(self):
        template_root = self.root / "templates"
        template = template_root / "templates" / "cvsprout-inference"
        template.mkdir(parents=True)
        model = self.root / "trained.rknn"
        model.write_bytes(b"isolated model bytes; not an executable hardware fixture")
        manifest = copy.deepcopy(self.manifest)
        manifest["models"] = [{"id": "det", "file": "models/model.rknn", "task": "detect"}]
        (template / "manifest.json").write_text(json.dumps(manifest))
        (template / "app.py").write_text("# fixture\n")
        conversion, run, _ = model_fixture()
        contract = packages.model_contract(conversion, run)
        contract["model_sha256"] = packages.sha256(model)
        destination = self.root / "staged"
        with patch.object(packages, "ROOT", template_root):
            packages.stage_inference(destination, model, contract, "1.2.3", 123)
        staged = json.loads((destination / "manifest.json").read_text())
        self.assertEqual(staged["version"], "1.2.3")
        self.assertEqual(staged["release"]["sequence"], 123)
        self.assertEqual(staged["models"][0]["input"], [1, 640, 640, 3])
        self.assertEqual(staged["models"][0]["classes"], ["person"])
        artifact = staged["artifacts"][0]
        self.assertEqual(artifact["file"], artifact["mount"])
        self.assertEqual(artifact["sha256"], packages.sha256(model))
        self.assertEqual(artifact["size"], model.stat().st_size)
        self.assertEqual((destination / "labels.txt").read_text(), "person\n")
        self.assertEqual(json.loads((destination / "model_contract.json").read_text()), contract)
        with patch.object(packages, "ROOT", template_root), self.assertRaisesRegex(ValueError, "发生变化"):
            packages.stage_inference(self.root / "bad-stage", model, {**contract, "model_sha256": "0" * 64}, "1.2.4", 124)


if __name__ == "__main__":
    unittest.main()
