from __future__ import annotations

import errno
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import anyio
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


class PinnedFileResponseTest(unittest.TestCase):
    """Exercise delayed ASGI consumption without a database or business data."""

    def setUp(self):
        from app.config import settings
        from app.file_responses import pinned_file_response

        self.settings = settings
        self.respond = pinned_file_response
        self.temp = tempfile.TemporaryDirectory(prefix="cvsprout-download-")
        self.previous_data_dir = settings.data_dir
        object.__setattr__(settings, "data_dir", Path(self.temp.name) / "data")
        self.addCleanup(self.restore_settings)
        self.source = settings.training_dir / "run-1/attempt-1/best.pt"
        self.source.parent.mkdir(parents=True)
        self.content = bytes(range(256)) * 1024
        self.source.write_bytes(self.content)
        self.downloads = settings.training_dir / ".downloads"

    def restore_settings(self):
        object.__setattr__(self.settings, "data_dir", self.previous_data_dir)
        self.temp.cleanup()

    def assert_clean(self):
        self.assertFalse(self.downloads.exists() and list(self.downloads.iterdir()))

    def test_deleted_source_before_full_and_range_response_are_consumed(self):
        for headers, status, expected in [({}, 200, self.content),
                                          ({"Range": "bytes=17-65554"}, 206, self.content[17:65555])]:
            with self.subTest(headers=headers):
                self.source.parent.mkdir(parents=True, exist_ok=True)
                self.source.write_bytes(self.content)
                app = FastAPI()

                @app.get("/artifact")
                def artifact():
                    response = self.respond(self.source, filename="中文模型.pt")
                    shutil.rmtree(self.settings.training_dir / "run-1")
                    return response

                with TestClient(app) as client:
                    response = client.get("/artifact", headers=headers)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.content, expected)
                self.assertEqual(response.headers["content-length"], str(len(expected)))
                self.assertEqual(response.headers["accept-ranges"], "bytes")
                self.assertIn("filename*=utf-8''", response.headers["content-disposition"])
                self.assert_clean()

    def test_source_deleted_during_full_and_range_transmission(self):
        for headers, status, expected in [([], 200, self.content),
                                          ([(b"range", b"bytes=17-196608")], 206, self.content[17:196609])]:
            with self.subTest(headers=headers):
                self.source.parent.mkdir(parents=True, exist_ok=True)
                self.source.write_bytes(self.content)
                response = self.respond(self.source, filename="model.pt")
                messages = []

                async def receive():
                    # The request body has already been consumed. A real ASGI
                    # server waits for another event instead of spinning.
                    await anyio.sleep_forever()
                    return {"type": "http.disconnect"}

                async def send(message):
                    messages.append(message)
                    if message["type"] == "http.response.body" and self.source.exists():
                        shutil.rmtree(self.settings.training_dir / "run-1")

                scope = {"type": "http", "method": "GET", "headers": headers,
                         "asgi": {"version": "3.0", "spec_version": "2.4"}}
                anyio.run(response, scope, receive, send)
                self.assertEqual(messages[0]["status"], status)
                self.assertEqual(b"".join(m.get("body", b"") for m in messages), expected)
                self.assert_clean()

    @unittest.skipIf(os.name == "nt", "Windows uses the copy path instead of hard links")
    def test_link_race_returns_conflict_and_removes_temporary_directory(self):
        original_link = os.link

        def remove_then_link(source, target, **kwargs):
            Path(source).unlink()
            return original_link(source, target, **kwargs)

        with patch("app.file_responses.os.link", side_effect=remove_then_link):
            with self.assertRaises(HTTPException) as error:
                self.respond(self.source, filename="model.pt")
        self.assertEqual(error.exception.status_code, 409)
        self.assert_clean()

    def test_copy_fallback_pins_open_source_and_preserves_bytes(self):
        original_copy = shutil.copyfileobj

        def copy_after_unlink(reader, writer, **kwargs):
            # Windows does not allow deleting an open source; the caller unlinks
            # it after the completed snapshot in that case.
            if os.name != "nt":
                self.source.unlink()
            return original_copy(reader, writer, **kwargs)

        with patch("app.file_responses.os.link", side_effect=OSError(errno.EXDEV, "other device")), \
                patch("app.file_responses.shutil.copyfileobj", side_effect=copy_after_unlink):
            response = self.respond(self.source, filename="model.pt")
        self.source.unlink(missing_ok=True)
        app = FastAPI()
        app.add_api_route("/artifact", lambda: response)
        with TestClient(app) as client:
            result = client.get("/artifact", headers={"Range": "bytes=5-100"})
        self.assertEqual(result.status_code, 206)
        self.assertEqual(result.content, self.content[5:101])
        self.assert_clean()

    def test_failed_copy_cleans_partial_file_and_returns_unavailable(self):
        def failed_copy(reader, writer, **kwargs):
            writer.write(reader.read(100))
            raise OSError(errno.ENOSPC, "full disk")

        with patch("app.file_responses.os.link", side_effect=OSError(errno.EXDEV, "other device")), \
                patch("app.file_responses.shutil.copyfileobj", side_effect=failed_copy):
            with self.assertRaises(HTTPException) as error:
                self.respond(self.source, filename="model.pt")
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(self.source.read_bytes(), self.content)
        self.assert_clean()

    def test_missing_source_returns_conflict(self):
        self.source.unlink()
        with self.assertRaises(HTTPException) as error:
            self.respond(self.source, filename="model.pt")
        self.assertEqual(error.exception.status_code, 409)
        self.assert_clean()

    def test_redirected_download_directory_is_rejected(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        marker = outside / "keep"
        marker.write_text("keep")
        try:
            self.downloads.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Directory symlinks unavailable")
        with self.assertRaises(HTTPException) as error:
            self.respond(self.source, filename="model.pt")
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(list(outside.iterdir()), [marker])

    def test_invalid_ranges_release_the_snapshot(self):
        app = FastAPI()
        app.add_api_route("/artifact", lambda: self.respond(self.source, filename="model.pt"))
        with TestClient(app) as client:
            for value, status in [("invalid", 400), ("bytes=999999999-", 416)]:
                with self.subTest(range=value):
                    result = client.get("/artifact", headers={"Range": value})
                    self.assertEqual(result.status_code, status)
                    self.assert_clean()

    def test_failed_send_releases_the_snapshot(self):
        response = self.respond(self.source, filename="model.pt")

        async def receive():
            await anyio.sleep_forever()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                raise OSError("client disconnected")

        # ASGI 2.4 reports disconnects through send() raising OSError. Pin that
        # protocol so old and new Starlette exercise the same failure contract.
        scope = {"type": "http", "method": "GET", "headers": [],
                 "asgi": {"version": "3.0", "spec_version": "2.4"}}
        with self.assertRaisesRegex(OSError, "client disconnected"):
            anyio.run(response, scope, receive, send)
        self.assert_clean()


if __name__ == "__main__":
    unittest.main()
