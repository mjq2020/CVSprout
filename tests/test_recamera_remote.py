"""Simulate public App Center responses; no SSH or device hardware is used."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import recamera_remote as remote
from tests.test_recamera_packages import minimal_manifest


def operation(kind="install", status="succeeded", **values):
    return {"id": "operation-" + kind, "app_id": "cvsprout-inference", "type": kind, "status": status, **values}


class FakeAppCenter:
    def __init__(self, installed_root, config):
        self.root, self.config = installed_root, config
        self.calls = []
        self.existing = None
        self.installed = False
        self.limit = 200 * 1024 * 1024
        self.receipt = {"upload_id": "upload-1", "preflight": {"manifest": copy.deepcopy(config["manifest"]),
            "release_id": config["release_id"], "checks": [{"name": "signature-policy", "passed": True}],
            "install_context": {"mode": "new", "confirmation_required": []}}}
        self.install_result = {"operation": operation()}
        self.start_result = {"operation": operation("start")}
        self.operations = []
        self.running = {"id": "cvsprout-inference", "version": config["manifest"]["version"], "running": True,
            "runtime": {"observed_state": "ready"}, "instance": {"id": "instance-new", "generation": 2}}
        self.installed_release_id = config["release_id"]

    def request(self, method, path, body=None, package=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if (method, path) == ("GET", "/policy"):
            return {"upload": {"max_package_bytes": self.limit}}
        if (method, path) == ("GET", "/apps"):
            return {"apps": [self.running] if self.installed else ([self.existing] if self.existing else [])}
        if (method, path) == ("POST", "/uploads"):
            assert package.is_file()
            return copy.deepcopy(self.receipt)
        if method == "DELETE" and path.startswith("/uploads/"):
            return {"deleted": True}
        if (method, path) == ("POST", "/apps"):
            if isinstance(self.install_result, BaseException):
                raise self.install_result
            self.installed = True
            self.root.mkdir(exist_ok=True)
            (self.root / "manifest.json").write_text(json.dumps(self.config["manifest"]))
            (self.root / "release.lock.json").write_text(json.dumps({"release_id": self.installed_release_id}))
            return self.install_result
        if (method, path) == ("POST", "/apps/cvsprout-inference/start"):
            if isinstance(self.start_result, BaseException):
                raise self.start_result
            return self.start_result
        if (method, path) == ("GET", "/operations"):
            return {"operations": self.operations}
        raise AssertionError(f"Unexpected device request {method} {path}")


class RemoteDeploymentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cvsprout-remote-test-")
        self.root = Path(self.temp.name)
        self.package = self.root / "package.tar.gz"
        self.package.write_bytes(b"transport-only test bytes; archive validation belongs to package tests")
        self.config = {"manifest": minimal_manifest(), "release_id": "release-1", "replace": False, "start": True,
                       "size": self.package.stat().st_size, "sha256": hashlib.sha256(self.package.read_bytes()).hexdigest()}
        self.api = FakeAppCenter(self.root / "installed", self.config)
        self.report = {}
        self.path_patch = patch.object(remote, "Path", side_effect=self.remap_path)
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.temp.cleanup()

    def remap_path(self, value):
        if str(value) == "/userdata/local/apps/cvsprout-inference":
            return self.root / "installed"
        if str(value) == "/userdata/appstage":
            return self.root / "stage"
        return Path(value)

    def install(self):
        remote.install(self.api, self.package, self.config, self.report)

    def mutations(self):
        return [call for call in self.api.calls if call[0] == "POST" and call[1] != "/uploads"]

    def run_wire(self, config=None, payload=None):
        config = self.config if config is None else config
        payload = self.package.read_bytes() if payload is None else payload
        stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(config).encode() + b"\n" + payload))
        stdout = io.StringIO()
        with patch.object(remote.sys, "stdin", stdin), patch.object(remote.sys, "stdout", stdout), \
                patch.object(remote, "AppCenter", return_value=self.api), patch.object(remote.time, "sleep"):
            remote.main()
        line = stdout.getvalue().strip()
        self.assertTrue(line.startswith("CVSPROUT_DEPLOY="), line)
        self.assertFalse(list((self.root / "stage").glob("cvsprout-*")))
        return json.loads(line.split("=", 1)[1])

    def test_exact_permissions_manifest_release_and_running_identity_are_verified(self):
        self.install()
        self.assertEqual(self.report["status"], "succeeded")
        self.assertEqual(self.report["stage"], "running")
        self.assertEqual(self.report["instance_id"], "instance-new")
        self.assertEqual(self.report["generation"], 2)
        self.assertEqual(self.report["results"], "not_run")
        self.assertEqual(self.report["task_quality"], "not_run")
        install_body = self.mutations()[0][2]
        self.assertEqual(install_body, {"upload_id": "upload-1", "permissions_confirmed": True,
                                       "permissions": self.config["manifest"]["permissions"]})
        self.assertEqual(len(self.mutations()), 2)

    def test_existing_app_requires_explicit_replace_before_upload(self):
        self.api.existing = {"id": "cvsprout-inference", "version": "0.0.1"}
        with self.assertRaisesRegex(RuntimeError, "明确选择替换"):
            self.install()
        self.assertFalse(any(method == "POST" for method, _, _ in self.api.calls))

    def test_explicit_replace_passes_only_required_supported_confirmations(self):
        self.config["replace"] = True
        self.api.existing = {"id": "cvsprout-inference", "version": "0.0.1", "instance": {"id": "instance-old", "generation": 1}}
        self.api.receipt["preflight"]["install_context"] = {"mode": "upgrade", "confirmation_required": ["running_upgrade_confirmed"]}
        self.install()
        body = self.mutations()[0][2]
        self.assertTrue(body["running_upgrade_confirmed"])
        self.assertNotIn("force_reinstall_confirmed", body)
        self.assertEqual(self.report["previous_version"], "0.0.1")

    def test_preflight_mismatch_failed_checks_and_unexpected_confirmations_prevent_install(self):
        original = copy.deepcopy(self.api.receipt)
        changes = [
            lambda p: p.update(release_id="wrong-release"),
            lambda p: p["manifest"].update(version="9.9.9"),
            lambda p: p.update(checks=[{"passed": False}]),
            lambda p: p.update(checks=[]),
            lambda p: p.update(install_context={"mode": "upgrade", "confirmation_required": []}),
            lambda p: p.update(install_context={"mode": "new", "confirmation_required": ["disable_signature_verification"]}),
        ]
        for change in changes:
            self.api.calls.clear()
            self.api.receipt = copy.deepcopy(original)
            change(self.api.receipt["preflight"])
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                self.install()
            self.assertEqual(self.mutations(), [])
            self.assertTrue(any(call[:2] == ("DELETE", "/uploads/upload-1") for call in self.api.calls))

    def test_policy_limit_and_wrong_app_are_rejected_before_upload(self):
        self.api.limit = 1
        with self.assertRaisesRegex(RuntimeError, "大小限制"):
            self.install()
        self.assertFalse(any(method == "POST" for method, _, _ in self.api.calls))
        self.config["manifest"]["id"] = "unrelated-app"
        with self.assertRaisesRegex(RuntimeError, "不支持"):
            self.install()

    def test_installed_release_mismatch_never_starts(self):
        self.api.installed_release_id = "unexpected-release"
        with self.assertRaisesRegex(RuntimeError, "完整性不匹配"):
            self.install()
        self.assertEqual(len(self.mutations()), 1)
        self.assertFalse(self.report["mutation_pending"])

    def test_installed_without_start_reports_exact_limited_validation(self):
        self.config["start"] = False
        self.install()
        self.assertEqual(self.report["stage"], "installed")
        self.assertEqual(self.report["results"], "not_run")
        self.assertNotIn("running", self.report)
        self.assertEqual(len(self.mutations()), 1)

    def test_mismatched_or_disappeared_operation_is_uncertain_without_resubmit(self):
        for submitted in ({}, {"operation": operation(app_id="other")}, {"operation": operation("start")}):
            with self.subTest(submitted=submitted), self.assertRaises(remote.Uncertain):
                remote.wait_operation(self.api, submitted, "install", {})
        self.api.operations = []
        with patch.object(remote.time, "sleep"), self.assertRaises(remote.Uncertain):
            remote.wait_operation(self.api, {"operation": operation(status="running")}, "install", {})
        self.assertEqual(self.mutations(), [])

    def test_matching_operation_is_polled_by_list_and_failure_clears_pending_flag(self):
        self.api.operations = [operation()]
        report = {"mutation_pending": True}
        with patch.object(remote.time, "sleep"):
            remote.wait_operation(self.api, {"operation": operation(status="running")}, "install", report)
        self.assertEqual(self.api.calls[-1][:2], ("GET", "/operations"))
        self.assertEqual(report["operation_id"], "operation-install")
        with self.assertRaisesRegex(RuntimeError, "设备操作失败"):
            remote.wait_operation(self.api, {"operation": operation(status="failed", error="resource busy")}, "install", report)
        self.assertFalse(report["mutation_pending"])

    def test_pending_operation_timeout_is_uncertain_and_never_reposts(self):
        with self.assertRaises(remote.Uncertain):
            remote.wait_operation(self.api, {"operation": operation(status="running")}, "install", {}, timeout=0)
        self.assertEqual(self.mutations(), [])

    def test_wire_digest_mismatch_and_short_payload_fail_before_api_mutations(self):
        report = self.run_wire({**self.config, "sha256": "0" * 64})
        self.assertEqual(report["status"], "failed")
        self.assertIn("SHA-256", report["error"])
        self.assertEqual(self.api.calls, [])
        report = self.run_wire(payload=b"short")
        self.assertEqual(report["status"], "failed")
        self.assertIn("未传输完整", report["error"])
        self.assertEqual(self.api.calls, [])

    def test_unknown_mutation_response_is_reported_unknown_and_not_retried(self):
        self.api.install_result = ConnectionError("connection lost after acceptance")
        report = self.run_wire()
        self.assertEqual(report["status"], "unknown")
        self.assertEqual(report["stage"], "installing")
        self.assertTrue(report["mutation_pending"])
        self.assertEqual(len(self.mutations()), 1)
        self.assertFalse(any(method == "DELETE" for method, _, _ in self.api.calls))

    def test_explicit_failed_operation_is_failed_instead_of_unknown(self):
        self.api.install_result = {"operation": operation(status="failed", error="insufficient resources")}
        report = self.run_wire()
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["mutation_pending"])
        self.assertEqual(len(self.mutations()), 1)

    def test_lost_runtime_query_after_successful_start_keeps_outcome_unknown(self):
        original = self.api.request

        def lose_runtime_query(method, path, *args, **kwargs):
            if (method, path) == ("GET", "/apps") and self.api.installed:
                raise ConnectionError("device no longer reachable")
            return original(method, path, *args, **kwargs)

        with patch.object(self.api, "request", side_effect=lose_runtime_query):
            report = self.run_wire()
        self.assertEqual(report["status"], "unknown")
        self.assertTrue(report["installed"])
        self.assertEqual(report["stage"], "starting")
        self.assertEqual(len(self.mutations()), 2)

    def test_old_running_instance_does_not_prove_replacement_started(self):
        self.config["replace"] = True
        self.api.existing = {"id": "cvsprout-inference", "version": "0.0.1", "instance": {"id": "instance-new", "generation": 2}}
        self.api.receipt["preflight"]["install_context"]["mode"] = "upgrade"
        with self.assertRaisesRegex(RuntimeError, "升级前的实例"):
            self.install()


if __name__ == "__main__":
    unittest.main()
