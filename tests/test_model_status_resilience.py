from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.annotation_models import preparation, sam2_model_path
from app.auth import Actor, get_actor
from app.config import settings
from app.main import app
from app.providers import local, qwen_local, registry


class ModelStatusResilienceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cvsprout-model-status-")
        self.root = Path(self.temp.name)
        changes = {"models_dir": self.root, "worker_url": "",
                   "sam2_checkpoint": self.root / "sam2/sam2.1_t.pt",
                   "sam3_checkpoint": self.root / "sam3/sam3.pt"}
        self.previous = {key: getattr(settings, key) for key in changes}
        for key, value in changes.items():
            object.__setattr__(settings, key, value)
        self.overrides = dict(app.dependency_overrides)
        app.dependency_overrides[get_actor] = lambda: Actor("owner", "admin")
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self.overrides)
        for key, value in self.previous.items():
            object.__setattr__(settings, key, value)
        self.temp.cleanup()

    def row(self, name, provider, path):
        return {"id": name, "name": name, "family": "test", "provider_type": provider,
                "description": "", "endpoint": "local://test", "model_name": str(path),
                "api_key_env": "", "capabilities_json": "[]", "modes_json": "[]",
                "config_json": "{}", "enabled": 1, "builtin": 1, "timeout_seconds": 180,
                "last_health_status": "unknown", "last_health_error": "", "last_health_at": None}

    def listing(self, rows):
        with patch.object(registry, "all_rows", return_value=rows):
            response = self.client.get("/api/models")
        self.assertEqual(response.status_code, 200, response.text)
        return {item["id"]: item for item in response.json()}

    def test_looping_qwen_directory_does_not_break_listing_or_remote_configuration(self):
        path = self.root / "qwen3.5-0.8b"
        path.symlink_to(path.name, target_is_directory=True)
        remote = self.row("qwen3.5-35b-a3b", "openai_vision", "administrator-model")
        remote["endpoint"] = "https://example.invalid/v1"
        rows = self.listing([self.row("qwen3.5-0.8b", "local_qwen", path), remote])
        self.assertFalse(rows["qwen3.5-0.8b"]["configured"])
        self.assertIn("不可用", rows["qwen3.5-0.8b"]["availability_error"])
        self.assertIsNone(rows["qwen3.5-0.8b"]["preparation"])
        self.assertTrue(rows["qwen3.5-35b-a3b"]["configured"])
        self.assertEqual(rows["qwen3.5-35b-a3b"]["endpoint"], remote["endpoint"])
        self.assertEqual(rows["qwen3.5-35b-a3b"]["model_name"], "administrator-model")

    def test_unreadable_manual_paths_are_individual_errors_not_list_failures(self):
        records = [self.row("sam2-assist", "local_sam2", settings.sam2_checkpoint),
                   self.row("sam3", "local_sam3", settings.sam3_checkpoint)]
        original = Path.stat

        def denied(path, *args, **kwargs):
            if path.is_relative_to(self.root):
                raise PermissionError("model mount is not accessible")
            return original(path, *args, **kwargs)

        with patch.object(Path, "stat", new=denied):
            self.assertEqual(sam2_model_path(), settings.sam2_checkpoint)
            rows = self.listing(records)
        for item in rows.values():
            self.assertFalse(item["configured"])
            self.assertIn("权限", item["availability_error"])
            self.assertFalse(item["preparation"]["installed"])
            self.assertTrue(item["preparation"]["missing_files"])

    def test_unreadable_directory_checks_do_not_import_inference_dependencies(self):
        with patch.object(Path, "is_dir", side_effect=PermissionError("no traversal")), \
                patch.object(qwen_local, "_runtime_support", side_effect=AssertionError("must stop before dependencies")):
            for status, args in ((local.local_runtime_status, (str(self.root),)),
                                 (local.local_sam2_runtime_status, (str(self.root),)),
                                 (qwen_local.local_qwen_runtime_status, (str(self.root),))):
                with self.subTest(status=status.__name__):
                    available, reason = status(*args)
                    self.assertFalse(available)
                    self.assertTrue(reason)

    def test_sam_checkpoint_probe_reports_stat_and_read_failures(self):
        path = self.root / "sam3.pt"
        path.write_bytes(b"fixture")
        with patch.object(Path, "is_file", side_effect=PermissionError("denied")):
            self.assertEqual(local._checkpoint_status(path, "SAM3")[0], False)
        with path.open("wb") as stream:
            stream.truncate(1024 * 1024)
        with patch.object(Path, "open", side_effect=PermissionError("denied")):
            available, reason = local._checkpoint_status(path, "SAM3")
        self.assertFalse(available)
        self.assertIn("不可读", reason)

    def test_sam3_unreadable_optional_sdk_path_is_reported(self):
        with patch.object(local, "_checkpoint_status", return_value=(True, "")), \
                patch.object(local, "_ultralytics_sam_status", return_value=(False, "missing native runtime")), \
                patch.object(local, "_ensure_sam3_import_path", side_effect=PermissionError("SDK inaccessible")):
            available, reason = local.local_sam3_runtime_status(str(self.root / "sam3.pt"), str(self.root / "sdk"))
        self.assertFalse(available)
        self.assertIn("权限", reason)

    def test_legacy_sam_preparation_requires_processor_config(self):
        path = self.root / "sam2.1-hiera-tiny"
        path.mkdir()
        for name in ("config.json", "model.safetensors", "preprocessor_config.json"):
            (path / name).write_bytes(b"fixture")
        row = self.row("sam2-assist", "local_sam2", path)
        plan = preparation(row)
        self.assertFalse(plan["installed"])
        self.assertEqual(plan["missing_files"], ["processor_config.json"])
        (path / "processor_config.json").write_bytes(b"{}")
        self.assertTrue(preparation(row)["installed"])

    def test_web_status_delegates_without_importing_heavy_dependencies(self):
        code = """
import importlib.abc
import sys
from unittest.mock import patch
class RejectHeavy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'torch', 'torchvision', 'transformers', 'ultralytics', 'safetensors'}:
            raise AssertionError('web imported inference dependency: ' + fullname)
sys.meta_path.insert(0, RejectHeavy())
from app.config import settings
from app.providers import local, qwen_local
object.__setattr__(settings, 'worker_url', 'http://worker.invalid')
with patch('app.worker_client.call', return_value=(False, 'worker fixture')) as rpc:
    assert qwen_local.local_qwen_runtime_status('/missing') == (False, 'worker fixture')
    assert local.local_sam2_runtime_status('/missing') == (False, 'worker fixture')
    assert local.local_sam3_runtime_status('/missing', '/missing') == (False, 'worker fixture')
    assert local.local_runtime_status('/missing') == (False, 'worker fixture')
    assert rpc.call_count == 4
"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
