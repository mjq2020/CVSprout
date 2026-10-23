from __future__ import annotations

import errno
import fcntl
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app import model_downloads as downloads
from app.auth import Actor
from app.config import settings


class ModelDownloadResilienceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cvsprout-download-resilience-")
        self.root = Path(self.temp.name)
        self.previous = settings.models_dir
        object.__setattr__(settings, "models_dir", self.root / "models")
        self.content = b"official fixture"
        self.model = {"id": "fixture", "name": "Fixture", "files": [{
            "path": "fixture/model.safetensors", "url": "https://example.invalid/model",
            "size": len(self.content), "sha256": hashlib.sha256(self.content).hexdigest(),
        }]}
        self.catalog = patch.object(downloads, "catalog", return_value=[self.model])
        self.catalog.start()

    def tearDown(self):
        self.catalog.stop()
        object.__setattr__(settings, "models_dir", self.previous)
        self.temp.cleanup()

    @property
    def lock_path(self):
        return settings.models_dir / ".downloads/fixture.lock"

    def install(self, content=None):
        path = settings.models_dir / self.model["files"][0]["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.content if content is None else content)
        return path

    def assert_lock_free(self):
        with self.lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_missing_status_does_not_create_any_directory_or_start_network(self):
        with patch.object(Path, "mkdir", side_effect=AssertionError("GET created a directory")), \
                patch.object(downloads.httpx, "stream", side_effect=AssertionError("GET requested network")), \
                patch.object(downloads, "state_dir", side_effect=AssertionError("GET requested writable state")):
            self.assertEqual(downloads.download_status(self.model)["status"], "missing")
            rows = downloads.list_downloads(Actor("owner", "admin"))
        self.assertFalse(rows[0]["installed"])
        self.assertFalse(settings.models_dir.exists())

    def test_installed_readonly_models_need_no_writable_state_directory(self):
        self.install()
        original_open = Path.open

        def read_only(path, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                raise PermissionError("read-only mount")
            return original_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", new=read_only), \
                patch.object(Path, "mkdir", side_effect=PermissionError("read-only mount")):
            self.assertEqual(downloads.download_status(self.model)["status"], "complete")
            self.assertTrue(downloads.list_downloads(Actor("owner", "admin"))[0]["installed"])
        self.assertFalse((settings.models_dir / ".downloads").exists())

    def test_missing_lock_is_interrupted_without_creating_a_replacement(self):
        downloads.write_state("fixture", status="downloading", downloaded=2)
        self.assertEqual(downloads.download_status(self.model)["status"], "interrupted")
        self.assertFalse(self.lock_path.exists())

    def test_existing_lock_is_checked_readonly_and_existing_owner_is_preserved(self):
        downloads.write_state("fixture", status="downloading", downloaded=2)
        with self.lock_path.open("a+") as owner:
            fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            original_open = Path.open

            def read_only(path, mode="r", *args, **kwargs):
                self.assertNotRegex(mode, r"[wax+]")
                return original_open(path, mode, *args, **kwargs)

            with patch.object(Path, "open", new=read_only):
                self.assertEqual(downloads.download_status(self.model)["status"], "downloading")
            with self.lock_path.open("rb") as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(downloads.download_status(self.model)["status"], "interrupted")

    def test_unreadable_state_and_lock_are_model_failures_instead_of_list_failures(self):
        for error in (PermissionError(errno.EACCES, "permission denied"), OSError(errno.ENOSPC, "no space")):
            with self.subTest(error=error), patch.object(Path, "read_text", side_effect=error):
                result = downloads.list_downloads(Actor("owner", "admin"))[0]
                self.assertEqual(result["state"]["status"], "failed")
                self.assertIn("权限", result["state"]["error"])
        downloads.write_state("fixture", status="downloading")
        original_open = Path.open

        def fail_lock(path, *args, **kwargs):
            if path == self.lock_path:
                raise PermissionError("lock is unreadable")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", new=fail_lock):
            self.assertEqual(downloads.download_status(self.model)["status"], "failed")

    def test_corrupt_json_or_nonobject_state_is_treated_as_missing(self):
        path = downloads.state_dir() / "fixture.json"
        for content in ("{", "[]", "null", '"text"', "42"):
            path.write_text(content)
            self.assertEqual(downloads.download_status(self.model)["status"], "missing")
        path.write_bytes(b"\xff")
        self.assertEqual(downloads.download_status(self.model)["status"], "missing")

    def test_removed_file_between_state_read_and_stat_is_missing(self):
        path = self.install()
        downloads.write_state("fixture", status="complete")
        original_stat = Path.stat

        def disappearing(candidate, *args, **kwargs):
            if candidate == path and kwargs.get("follow_symlinks", True):
                raise FileNotFoundError("file disappeared before stat")
            return original_stat(candidate, *args, **kwargs)

        with patch.object(Path, "stat", new=disappearing):
            result = downloads.list_downloads(Actor("owner", "admin"))[0]
        self.assertFalse(result["installed"])
        self.assertEqual(result["state"]["status"], "missing")

    def test_unreadable_model_stat_is_reported_without_breaking_the_list(self):
        path = self.install()
        original_stat = Path.stat

        def unreadable(candidate, *args, **kwargs):
            if candidate == path and kwargs.get("follow_symlinks", True):
                raise PermissionError("model stat denied")
            return original_stat(candidate, *args, **kwargs)

        with patch.object(Path, "stat", new=unreadable):
            self.assertEqual(downloads.list_downloads(Actor("owner", "admin"))[0]["state"]["status"], "failed")

    def test_unsafe_path_is_reported_and_zero_byte_files_are_not_installed(self):
        path = self.install(b"")
        row = downloads.list_downloads(Actor("owner", "admin"))[0]
        self.assertFalse(row["installed"])
        self.assertEqual(row["state"]["status"], "missing")
        outside = self.root / "outside"; outside.write_bytes(b"do not change")
        path.unlink(); path.symlink_to(outside)
        self.assertEqual(downloads.download_status(self.model)["status"], "failed")
        self.assertEqual(outside.read_bytes(), b"do not change")

    def test_queue_directory_failures_return_controlled_503(self):
        for error in (PermissionError("readonly"), OSError(errno.ENOSPC, "full"), ValueError("unsafe path")):
            with self.subTest(error=error), patch.object(downloads, "state_dir", side_effect=error), \
                    patch.object(downloads.threading, "Thread") as thread:
                with self.assertRaises(HTTPException) as result:
                    downloads.queue_download("fixture")
                self.assertEqual(result.exception.status_code, 503)
                thread.assert_not_called()

    def test_queue_state_write_failure_closes_handle_and_allows_retry(self):
        original_open, handles = Path.open, []

        def track(path, *args, **kwargs):
            handle = original_open(path, *args, **kwargs)
            if path == self.lock_path:
                handles.append(handle)
            return handle

        with patch.object(Path, "open", new=track), \
                patch.object(downloads, "write_state", side_effect=OSError(errno.ENOSPC, "full")), \
                patch.object(downloads.threading, "Thread") as thread:
            with self.assertRaises(HTTPException) as result:
                downloads.queue_download("fixture")
        self.assertEqual(result.exception.status_code, 503)
        self.assertEqual(len(handles), 1); self.assertTrue(handles[0].closed)
        thread.assert_not_called()
        self.assert_lock_free()

    def test_thread_start_failure_closes_lock_and_reports_interrupted_next_time(self):
        with patch.object(downloads.threading, "Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("cannot start new thread")
            with self.assertRaises(HTTPException) as result:
                downloads.queue_download("fixture")
            handle = thread.call_args.kwargs["args"][1]
        self.assertEqual(result.exception.status_code, 503)
        self.assertTrue(handle.closed)
        self.assert_lock_free()
        self.assertEqual(downloads.download_status(self.model)["status"], "interrupted")

    def test_failed_lock_acquisition_closes_handle(self):
        with patch.object(downloads.fcntl, "flock", side_effect=OSError(errno.ENOSPC, "lock error")):
            with self.assertRaises(HTTPException) as result:
                downloads.queue_download("fixture")
        self.assertEqual(result.exception.status_code, 503)
        self.assert_lock_free()

    def test_busy_queue_remains_idempotent_or_409_without_releasing_owner(self):
        downloads.state_dir()
        with self.lock_path.open("a+") as owner:
            fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(downloads.threading, "Thread", side_effect=AssertionError("duplicate worker")):
                self.assertEqual(downloads.queue_download("fixture", idempotent=True)["status"], "downloading")
                with self.assertRaises(HTTPException) as result:
                    downloads.queue_download("fixture")
            self.assertEqual(result.exception.status_code, 409)
            with self.assertRaises(BlockingIOError):
                self.assert_lock_free()

    def test_thread_owns_lock_after_successful_handoff_and_closes_it_on_completion(self):
        with patch.object(downloads.threading, "Thread") as thread:
            self.assertEqual(downloads.queue_download("fixture")["status"], "downloading")
            handle = thread.call_args.kwargs["args"][1]
        self.assertFalse(handle.closed)
        with self.assertRaises(BlockingIOError):
            self.assert_lock_free()
        with patch.object(downloads, "download_file"), patch("app.providers.seed_builtin_models"):
            downloads._download(self.model, handle)
        self.assertTrue(handle.closed)
        self.assert_lock_free()

    def test_background_disk_failure_cannot_leave_lock_held_even_if_error_state_fails(self):
        with patch.object(downloads.threading, "Thread") as thread:
            downloads.queue_download("fixture")
            handle = thread.call_args.kwargs["args"][1]
        with patch.object(downloads, "download_file", side_effect=OSError(errno.ENOSPC, "full")), \
                patch.object(downloads, "write_state", side_effect=OSError(errno.ENOSPC, "full")), \
                self.assertLogs(downloads.logger, "ERROR"):
            downloads._download(self.model, handle)
        self.assertTrue(handle.closed)
        self.assert_lock_free()
        self.assertEqual(downloads.download_status(self.model)["status"], "interrupted")

    def test_model_collection_stays_available_when_download_state_is_unreadable(self):
        from app.main import app
        from fastapi.testclient import TestClient

        changes = dict(data_dir=self.root / "data", auth_mode="disabled", embedded_worker=False,
                       embedded_pipeline_worker=False, worker_url="", qwen_base_url="",
                       sam2_checkpoint=settings.models_dir / "sam2/sam2.1_t.pt",
                       sam3_checkpoint=settings.models_dir / "sam3/sam3.pt",
                       training_base_model=settings.models_dir / "training/missing.pt",
                       training_segmentation_model=settings.models_dir / "training/missing-seg.pt",
                       training_model_dirs_raw=str(settings.models_dir / "training"))
        previous = {key: getattr(settings, key) for key in changes}
        self.catalog.stop()
        try:
            for key, value in changes.items():
                object.__setattr__(settings, key, value)
            with TestClient(app, headers={"X-MDP-Client": "browser"}) as client:
                original_read = Path.read_text

                def unreadable(path, *args, **kwargs):
                    if ".downloads" in path.parts:
                        raise PermissionError("read-only status")
                    return original_read(path, *args, **kwargs)

                with patch.object(Path, "read_text", new=unreadable):
                    response = client.get("/api/models")
                    self.assertEqual(response.status_code, 200, response.text)
                    model = next(row for row in response.json() if row["id"] == "qwen3.5-0.8b")
                    self.assertEqual(model["preparation"]["state"]["status"], "failed")
                with patch.object(downloads, "write_state", side_effect=OSError(errno.ENOSPC, "full")), \
                        patch.object(downloads.threading, "Thread") as thread:
                    response = client.post("/api/models/qwen3.5-0.8b/prepare")
                    self.assertEqual(response.status_code, 503, response.text)
                    self.assertIn("磁盘空间", response.json()["detail"])
                    thread.assert_not_called()
        finally:
            for key, value in previous.items():
                object.__setattr__(settings, key, value)
            self.catalog.start()


if __name__ == "__main__":
    unittest.main()
