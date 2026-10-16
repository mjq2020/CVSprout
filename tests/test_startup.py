"""Exercise the shipped launchers without changing the host Docker services."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PWSH = os.environ.get("CVSPROUT_TEST_PWSH") or shutil.which("pwsh")
FAKE_DOCKER = r'''
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
root = Path.cwd()
state_path = root / "docker-state.json"
state = json.loads(state_path.read_text()) if state_path.exists() else {"ups": 0}
settings = dict(line.split("=", 1) for line in (root / ".env").read_text().splitlines()
                if "=" in line and not line.startswith("#")) if (root / ".env").exists() else {}
port = os.environ.get("MDP_PORT", settings.get("MDP_PORT", "8000"))
with (root / "docker-calls.jsonl").open("a") as log:
    log.write(json.dumps({"args": args, "port": port}) + "\n")
if args[0] == "info":
    sys.exit(0)
args = args[1:]
while args[:1] == ["-f"]:
    args = args[2:]
command = args[0]
if command == "config" and "--images" in args:
    print("test/web:v1")
elif command == "up":
    state["ups"] += 1
    state_path.write_text(json.dumps(state))
    scenario = os.environ.get("FAKE_DOCKER_SCENARIO", "success")
    if scenario == "unhealthy":
        print("container worker is unhealthy", file=sys.stderr)
        sys.exit(1)
    if scenario in ("conflict", "retry_failure") and state["ups"] == 1:
        print(os.environ.get("FAKE_DOCKER_ERROR", "failed to bind host port: address already in use"), file=sys.stderr)
        sys.exit(1)
    if scenario == "retry_failure":
        print("container web is unhealthy", file=sys.stderr)
        sys.exit(1)
    state["port"] = "49177" if port == "0" else port
    state_path.write_text(json.dumps(state))
elif command == "port":
    print(settings.get("MDP_BIND_ADDRESS", "127.0.0.1") + ":" + state.get("port", port))
elif command not in ("version", "config", "build", "pull", "run", "exec", "ps", "stop"):
    sys.exit("unexpected Docker command: " + repr(args))
'''


class StartupCases:
    powershell = False

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cvsprout-launcher-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("start.sh", "start.ps1", ".env.example"):
            shutil.copy2(ROOT / name, self.root / name)
        self.original = "# custom settings\nMDP_PORT=8000\nMDP_BIND_ADDRESS=127.0.0.1\nCUSTOM_VALUE='keep $literal text'\n"
        self.env_file = self.root / ".env"
        self.env_file.write_text(self.original)
        binary = self.root / "bin"
        binary.mkdir()
        docker = binary / "docker"
        docker.write_text(f"#!{sys.executable}\n" + FAKE_DOCKER)
        docker.chmod(0o755)
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith(("MDP_", "COMPOSE_", "FAKE_DOCKER_"))}
        self.environment["PATH"] = str(binary) + os.pathsep + self.environment["PATH"]

    def launch(self, *options, **environment):
        if self.powershell:
            mapping = {"--cpu": "-Cpu", "--port": "-Port", "--strict-port": "-StrictPort",
                       "--status": "-Status", "--check": "-Check", "--stop": "-Stop"}
            command = [PWSH, "-NoLogo", "-NoProfile", "-File", str(self.root / "start.ps1"), "-Cpu"]
            command += [mapping.get(option, option) for option in options]
        else:
            command = ["bash", str(self.root / "start.sh"), "--cpu", *options]
        return subprocess.run(command, cwd=self.root, env={**self.environment, **environment},
                              capture_output=True, text=True, timeout=30)

    def up_ports(self):
        log = self.root / "docker-calls.jsonl"
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return [call["port"] for call in calls if "up" in call["args"]]

    def assert_success(self, result, port):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"http://127.0.0.1:{port}", result.stdout)
        self.assertEqual(self.env_file.read_text(), self.original.replace("MDP_PORT=8000", f"MDP_PORT={port}"))

    def test_explicit_port_overrides_environment_and_preserves_settings(self):
        self.assert_success(self.launch("--port", "8080", MDP_PORT="9000"), 8080)
        self.assertEqual(self.up_ports(), ["8080"])

    def test_environment_overrides_dotenv(self):
        self.assert_success(self.launch(MDP_PORT="9000"), 9000)

    def test_default_port_is_used_when_available(self):
        self.assert_success(self.launch(), 8000)
        self.assertEqual(self.up_ports(), ["8000"])

    def test_conflict_uses_dynamic_port_and_next_start_reuses_it(self):
        self.assert_success(self.launch(FAKE_DOCKER_SCENARIO="conflict"), 49177)
        self.assertEqual(self.up_ports(), ["8000", "0"])
        self.assert_success(self.launch(), 49177)
        self.assertEqual(self.up_ports(), ["8000", "0", "49177"])

    def test_explicit_occupied_port_falls_back(self):
        self.assert_success(self.launch("--port", "8080", FAKE_DOCKER_SCENARIO="conflict"), 49177)
        self.assertEqual(self.up_ports(), ["8080", "0"])

    def test_docker_and_windows_bind_errors_are_recoverable(self):
        for error in ("Bind for 127.0.0.1:8000 failed: port is already allocated",
                      "Only one usage of each socket address (protocol/network address/port) is normally permitted"):
            with self.subTest(error=error):
                state = self.root / "docker-state.json"
                state.unlink(missing_ok=True)
                self.assert_success(self.launch(FAKE_DOCKER_SCENARIO="conflict", FAKE_DOCKER_ERROR=error), 49177)

    def test_zero_requests_dynamic_allocation(self):
        self.assert_success(self.launch("--port", "0"), 49177)
        self.assertEqual(self.up_ports(), ["0"])

    def test_strict_port_failure_preserves_configuration(self):
        result = self.launch("--port", "8080", "--strict-port", FAKE_DOCKER_SCENARIO="conflict")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.up_ports(), ["8080"])
        self.assertEqual(self.env_file.read_text(), self.original)

    def test_unrelated_failure_does_not_retry_or_save(self):
        result = self.launch("--port", "8080", FAKE_DOCKER_SCENARIO="unhealthy")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.up_ports(), ["8080"])
        self.assertEqual(self.env_file.read_text(), self.original)

    def test_failed_retry_does_not_loop_or_save(self):
        result = self.launch(FAKE_DOCKER_SCENARIO="retry_failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.up_ports(), ["8000", "0"])
        self.assertEqual(self.env_file.read_text(), self.original)

    def test_invalid_ports_rejected_before_docker(self):
        for value in ("-1", "65536", "abc"):
            with self.subTest(value=value):
                self.assertNotEqual(self.launch("--port", value).returncode, 0)
        self.assertFalse((self.root / "docker-calls.jsonl").exists())
        self.assertEqual(self.env_file.read_text(), self.original)

    def test_maximum_port(self):
        self.assert_success(self.launch("--port", "65535"), 65535)

    def test_check_does_not_start_or_save(self):
        result = self.launch("--check", "--port", "8080")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.up_ports(), [])
        self.assertEqual(self.env_file.read_text(), self.original)

    def test_status_reports_actual_port_without_starting_or_saving(self):
        (self.root / "docker-state.json").write_text('{"port": "49177"}')
        result = self.launch("--status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("http://127.0.0.1:49177", result.stdout)
        self.assertEqual(self.up_ports(), [])
        self.assertEqual(self.env_file.read_text(), self.original)

    def test_missing_dotenv_is_created_and_port_is_saved(self):
        self.env_file.unlink()
        result = self.launch("--port", "8080")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("MDP_PORT=8080\n", self.env_file.read_text())

    def test_port_is_appended_if_not_in_dotenv(self):
        self.env_file.write_text("CUSTOM_VALUE=keep\n")
        result = self.launch("--port", "8080")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.env_file.read_text(), "CUSTOM_VALUE=keep\nMDP_PORT=8080\n")


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "requires Bash on POSIX")
class BashStartupTest(StartupCases, unittest.TestCase):
    def test_equals_syntax_and_leading_zeros(self):
        self.assert_success(self.launch("--port=08080"), 8080)

    def test_empty_missing_and_oversized_values_rejected(self):
        for options in (("--port",), ("--port=",), ("--port", ""), ("--port", "123456789012345678901234")):
            with self.subTest(options=options):
                self.assertEqual(self.launch(*options).returncode, 2)
        self.assertFalse((self.root / "docker-calls.jsonl").exists())


@unittest.skipUnless(os.name == "posix" and PWSH, "requires PowerShell and POSIX fake Docker")
class PowerShellStartupTest(StartupCases, unittest.TestCase):
    powershell = True

    def test_caller_environment_is_restored_after_fallback(self):
        result = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-Command",
             "$env:MDP_PORT = '9000'; & ./start.ps1 -Cpu; Write-Output ('RESTORED=' + $env:MDP_PORT)"],
            cwd=self.root, env={**self.environment, "FAKE_DOCKER_SCENARIO": "conflict"},
            capture_output=True, text=True, timeout=30)
        self.assert_success(result, 49177)
        self.assertIn("RESTORED=9000", result.stdout)


if __name__ == "__main__":
    unittest.main()
