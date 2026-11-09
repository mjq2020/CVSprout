"""Check named backend tasks against the shipped Connector without hardware."""
from __future__ import annotations

import json
from pathlib import Path
import secrets
import subprocess
import sys
import unittest

from tests import test_edge_models


class ModelNameConnectorTest(unittest.TestCase):
    def test_api_names_reach_existing_connector_upload_and_metadata_routes(self):
        fixture = test_edge_models.EdgeSystemModelsTest()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        release = fixture.release()
        template = Path(__file__).resolve().parents[1] / "deploy/recamera/templates/cvsprout-capture"
        for index, name in enumerate(("", "factory_yolo11-v2", "A" * 64)):
            with self.subTest(name=name or "default"):
                if index:
                    fixture.device = fixture.create(name=f"Camera {index}")
                    fixture.token = fixture.device["device_token"]
                self.assertEqual(fixture.status().status_code, 200)
                response = fixture.queue(release, model_name=name)
                self.assertEqual(response.status_code, 202, response.text)
                queued = response.json()
                claimed = fixture.claim(lease_token=secrets.token_urlsafe(32))
                self.assertEqual(claimed.status_code, 200, claimed.text)
                task = claimed.json()["task"]
                self.assertEqual(task["filename"], queued["filename"])
                if name:
                    self.assertEqual(task["filename"], f"{name}-r{release['id']}.rknn")
                else:
                    self.assertTrue(task["filename"].startswith("yolov8-run"))
                script = """
import json, sys, urllib.parse
from system_models import SystemModelClient, validate_task, running
task = validate_task(json.load(sys.stdin))
calls = []
client = object.__new__(SystemModelClient)
client.request = lambda path, *args, **kwargs: calls.append((path, args))
client.finalize('123', task)
client.configure(task)
assert len(calls) == 2
for path, args in calls:
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)['File-name'] == [task['filename']]
assert calls[1][1][1] == task['model_info']
current = dict(sModel=task['filename'], iEnable=1, sStatus='running', iFPS=task['fps'], iActualFPS=10, bExternalHold=False)
assert running(current, task['filename'], task['fps'])
assert not running(current, 'another-model.rknn', task['fps'])
"""
                completed = subprocess.run([sys.executable, "-c", script], cwd=template,
                                           input=json.dumps(task), capture_output=True, text=True, timeout=10)
                self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
