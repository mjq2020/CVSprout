from __future__ import annotations

import concurrent.futures
import fcntl
import hashlib
import json
import tempfile
import threading
import unittest
from collections import Counter
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import httpx

from app import model_downloads
from app.config import settings
from app.training_models import (
    TrainingModelDownloadCancelled, builtin_training_models,
    ensure_pretrained_model, get_builtin_training_model,
)


class TrainingModelRegistryTest(unittest.TestCase):
    def test_official_models_are_listed_without_installed_weights_or_network(self):
        with tempfile.TemporaryDirectory() as root:
            previous = settings.models_dir
            object.__setattr__(settings, "models_dir", Path(root) / "absent")
            try:
                with patch("app.model_downloads.httpx.stream", side_effect=AssertionError("catalog must stay offline")):
                    models = builtin_training_models()
                self.assertEqual(len(models), 85)
                self.assertEqual(Counter(model["task"] for model in models),
                                 {"detect": 38, "segment": 17, "classify": 15, "pose": 15})
                self.assertEqual(len({model["id"] for model in models}), len(models))
                self.assertFalse(settings.models_dir.exists(), "Listing a catalog does not install or create model directories")
                for family in ("yolov8", "yolo11", "yolo26"):
                    for size, suffix in ((size, suffix) for size in "nsmlx" for suffix in ("", "-seg", "-cls", "-pose")):
                        model = get_builtin_training_model(f"{family}{size}{suffix}.pt")
                        self.assertIsNotNone(model)
                        self.assertEqual(model["id"], model["model_arch"])
                        self.assertEqual(model["architecture"], model["id"][:-3] + ".yaml")
                        self.assertEqual(model["path"], settings.models_dir / "training" / model["id"])
                        self.assertEqual(model["source"], "builtin")
                        self.assertTrue(model["supports_scratch"])
                        self.assertEqual(model["revision"], "v8.4.0")
                        self.assertIn("/releases/download/v8.4.0/", model["url"])
                        self.assertEqual(len(model["sha256"]), 64)
                        self.assertGreater(model["size_bytes"], 1_000_000)
                self.assertIsNone(get_builtin_training_model("unknown.pt"))
                self.assertIsNone(get_builtin_training_model("../yolo26n.pt"))
            finally:
                object.__setattr__(settings, "models_dir", previous)

    def test_additional_families_have_explicit_official_architectures(self):
        expected = {f"yolov5{size}u.pt": f"yolov5{size}.yaml" for size in "nsmlx"}
        for prefix, sizes in (("yolov9", "tsmce"), ("yolov10", "nsmblx"), ("yolo12", "nsmlx"), ("rtdetr-", "lx")):
            expected.update({f"{prefix}{size}.pt": f"{prefix}{size}.yaml" for size in sizes})
        expected.update({f"yolov9{size}-seg.pt": f"yolov9{size}-seg.yaml" for size in "ce"})
        for name, architecture in expected.items():
            with self.subTest(name=name):
                model = get_builtin_training_model(name)
                self.assertEqual(model["architecture"], architecture)
                self.assertEqual(model["task"], "segment" if "-seg" in name else "detect")
                self.assertEqual(model["url"], f"https://github.com/ultralytics/assets/releases/download/v8.4.0/{name}")
                self.assertTrue(model["supports_scratch"])

    def test_catalog_architecture_aliases_cannot_escape_the_packaged_whitelist(self):
        entry = next(item for item in model_downloads.catalog() if item["id"] == "yolov5nu")
        for architecture in ("yolov5s.yaml", "../yolov5n.yaml", "/tmp/custom.yaml", "https://example.invalid/a.yaml", "yolov5nu.yaml"):
            with self.subTest(architecture=architecture), patch("app.model_downloads.catalog", return_value=[{**entry, "architecture": architecture}]):
                with self.assertRaisesRegex(ValueError, "无效路径"):
                    builtin_training_models()

    def test_known_published_representative_digests_are_pinned(self):
        for name, size, digest in [
            ("yolov8n.pt", 6549796, "f59b3d833e2ff32e194b5bb8e08d211dc7c5bdf144b90d2c8412c47ccfc83b36"),
            ("yolo11n.pt", 5613764, "0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1"),
            ("yolo26n.pt", 5544453, "9b09cc8bf347f0fc8a5f7657480587f25db09b34bf33b0652110fb03a8ad4fef"),
        ]:
            model = get_builtin_training_model(name)
            self.assertEqual((model["size_bytes"], model["sha256"]), (size, digest))


class TrainingModelPreparationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cvsprout-training-model-test-")
        self.root = Path(self.temp.name)
        self.previous = settings.models_dir
        object.__setattr__(settings, "models_dir", self.root / "models")
        self.content = b"verified-official-model-fixture"
        self.entry = {"id": "yolo26n", "name": "Fixture YOLO26", "kind": "training", "family": "YOLO26",
                      "task": "detect", "architecture": "yolo26n.yaml", "revision": "v8.4.0", "license": "fixture",
                      "files": [{"path": "training/yolo26n.pt", "url": "https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo26n.pt",
                                 "sha256": hashlib.sha256(self.content).hexdigest(), "size": len(self.content)}]}
        self.catalog = patch("app.model_downloads.catalog", return_value=[self.entry]); self.catalog.start()
        self.model = get_builtin_training_model("yolo26n.pt")
        self.target = self.model["path"]
        self.partial = self.target.with_name(self.target.name + ".part")

    def tearDown(self):
        self.catalog.stop()
        object.__setattr__(settings, "models_dir", self.previous)
        self.temp.cleanup()

    def response(self, content=None, status=200, headers=None):
        return nullcontext(httpx.Response(status, content=self.content if content is None else content,
                                         headers=headers, request=httpx.Request("GET", self.model["url"])))

    def state(self):
        return json.loads((settings.models_dir / ".downloads/yolo26n.json").read_text())

    def test_download_verifies_hash_and_size_before_publishing(self):
        amounts = []
        with patch("app.model_downloads.httpx.stream", return_value=self.response()) as request:
            path = ensure_pretrained_model(self.model, progress=lambda current, total: amounts.append((current, total)))
        self.assertEqual(path, self.target)
        self.assertEqual(path.read_bytes(), self.content)
        self.assertFalse(self.partial.exists())
        self.assertEqual(amounts[-1], (len(self.content), len(self.content)))
        self.assertEqual(request.call_args.args[1], self.model["url"])
        self.assertEqual(self.state()["status"], "complete")

    def test_already_installed_verified_model_never_requests_network(self):
        self.target.parent.mkdir(parents=True); self.target.write_bytes(self.content)
        with patch("app.model_downloads.httpx.stream", side_effect=AssertionError("network forbidden")):
            self.assertEqual(ensure_pretrained_model(self.model), self.target)

    def test_existing_same_name_custom_weights_are_preserved(self):
        self.target.parent.mkdir(parents=True); self.target.write_bytes(b"my own checkpoint")
        with patch("app.model_downloads.httpx.stream", side_effect=AssertionError("network forbidden")):
            with self.assertRaisesRegex(ValueError, "内容不同"):
                ensure_pretrained_model(self.model)
        self.assertEqual(self.target.read_bytes(), b"my own checkpoint")

    def test_wrong_checksum_or_short_response_is_not_published(self):
        for body in [b"x" * len(self.content), self.content[:-1]]:
            with self.subTest(body=body), patch("app.model_downloads.httpx.stream", return_value=self.response(body)):
                with self.assertRaisesRegex(ValueError, "完整性校验"):
                    ensure_pretrained_model(self.model)
            self.assertFalse(self.target.exists()); self.assertFalse(self.partial.exists())
            self.assertEqual(self.state()["status"], "failed")

    def test_resume_uses_existing_offset_and_verifies_final_content(self):
        self.target.parent.mkdir(parents=True); self.partial.write_bytes(self.content[:8])
        response = self.response(self.content[8:], 206, {"Content-Range": f"bytes 8-{len(self.content)-1}/{len(self.content)}"})
        with patch("app.model_downloads.httpx.stream", return_value=response) as request:
            ensure_pretrained_model(self.model)
        self.assertEqual(request.call_args.kwargs["headers"], {"Range": "bytes=8-"})
        self.assertEqual(self.target.read_bytes(), self.content)

    def test_server_ignoring_range_restarts_download_without_appending(self):
        self.target.parent.mkdir(parents=True); self.partial.write_bytes(self.content[:8])
        with patch("app.model_downloads.httpx.stream", return_value=self.response()):
            ensure_pretrained_model(self.model)
        self.assertEqual(self.target.read_bytes(), self.content)

    def test_wrong_resume_offset_keeps_previous_partial(self):
        self.target.parent.mkdir(parents=True); self.partial.write_bytes(self.content[:8])
        with patch("app.model_downloads.httpx.stream", return_value=self.response(self.content[8:], 206, {"Content-Range": "bytes 0-1/2"})):
            with self.assertRaisesRegex(ValueError, "续传位置"):
                ensure_pretrained_model(self.model)
        self.assertEqual(self.partial.read_bytes(), self.content[:8]); self.assertFalse(self.target.exists())

    def test_frozen_metadata_changes_and_arbitrary_urls_are_rejected(self):
        for key, value in [("url", "http://127.0.0.1/private"), ("sha256", "0" * 64), ("size_bytes", 1),
                           ("model_arch", "unknown.pt"), ("download_id", "../escape"), ("relative_path", "../escape.pt"),
                           ("architecture", "evil.yaml"), ("task", "segment"), ("id", "other.pt")]:
            with self.subTest(key=key), patch("app.model_downloads.httpx.stream", side_effect=AssertionError("network forbidden")):
                with self.assertRaisesRegex(ValueError, "受信任清单"):
                    ensure_pretrained_model({**self.model, key: value})
        self.assertFalse(settings.models_dir.exists())

    def test_untrusted_absolute_path_in_snapshot_is_ignored(self):
        outside = self.root / "outside.pt"
        with patch("app.model_downloads.httpx.stream", return_value=self.response()):
            result = ensure_pretrained_model({**self.model, "path": outside})
        self.assertEqual(result, self.target); self.assertFalse(outside.exists())

    def test_cancel_before_download_does_not_create_files_or_network(self):
        with patch("app.model_downloads.httpx.stream", side_effect=AssertionError("network forbidden")):
            with self.assertRaises(TrainingModelDownloadCancelled):
                ensure_pretrained_model(self.model, cancelled=lambda: True)
        self.assertFalse(settings.models_dir.exists())

    def test_cancel_between_chunks_preserves_partial_for_resume(self):
        self.content = b"a" * (3 * 1024 * 1024 + 17)
        self.entry["files"][0].update(size=len(self.content), sha256=hashlib.sha256(self.content).hexdigest())
        self.model = get_builtin_training_model("yolo26n.pt")
        amount = [0]
        with patch("app.model_downloads.httpx.stream", return_value=self.response()):
            with self.assertRaises(TrainingModelDownloadCancelled):
                ensure_pretrained_model(self.model, progress=lambda current, total: amount.__setitem__(0, current), cancelled=lambda: amount[0] >= 1024 * 1024)
        self.assertEqual(self.partial.stat().st_size, 1024 * 1024); self.assertFalse(self.target.exists())
        self.assertEqual(self.state()["status"], "interrupted")
        start = self.partial.stat().st_size
        with patch("app.model_downloads.httpx.stream", return_value=self.response(self.content[start:], 206, {"Content-Range": f"bytes {start}-{len(self.content)-1}/{len(self.content)}"})):
            ensure_pretrained_model(self.model)
        self.assertEqual(self.target.read_bytes(), self.content)

    def test_shared_administrator_download_lock_wait_is_cancellable(self):
        lock = (model_downloads.state_dir() / "yolo26n.lock").open("a+")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        started = threading.Event(); cancel = threading.Event()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool, patch("app.model_downloads.httpx.stream", side_effect=AssertionError("network forbidden")):
                future = pool.submit(ensure_pretrained_model, self.model, lambda current, total: started.set(), cancel.is_set)
                self.assertTrue(started.wait(2)); self.assertFalse(future.done()); cancel.set()
                with self.assertRaises(TrainingModelDownloadCancelled):
                    future.result(timeout=3)
            self.assertFalse(self.target.exists())
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN); lock.close()

    def test_waiting_for_other_download_reuses_its_verified_result(self):
        lock = (model_downloads.state_dir() / "yolo26n.lock").open("a+")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX); started = threading.Event()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool, patch("app.model_downloads.httpx.stream", side_effect=AssertionError("network forbidden")):
                future = pool.submit(ensure_pretrained_model, self.model, lambda current, total: started.set())
                self.assertTrue(started.wait(2)); self.target.parent.mkdir(parents=True); self.target.write_bytes(self.content)
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                self.assertEqual(future.result(timeout=3), self.target)
        finally:
            lock.close()

    def test_two_training_jobs_share_one_download(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool, patch("app.model_downloads.httpx.stream", side_effect=lambda *args, **kwargs: self.response()) as request:
            results = list(pool.map(ensure_pretrained_model, [self.model, self.model]))
        self.assertEqual(results, [self.target, self.target]); self.assertEqual(request.call_count, 1)

    def test_lease_callback_exceptions_are_not_misclassified_as_user_cancellation(self):
        error = RuntimeError("worker lease lost")
        def lost(): raise error
        with self.assertRaises(RuntimeError) as caught:
            ensure_pretrained_model(self.model, cancelled=lost)
        self.assertIs(caught.exception, error)

    def test_user_checkpoint_created_during_transfer_is_preserved(self):
        def imported(amount, total):
            if amount: self.target.write_bytes(b"user checkpoint imported concurrently")
        with patch("app.model_downloads.httpx.stream", return_value=self.response()):
            with self.assertRaisesRegex(ValueError, "下载期间被创建"):
                ensure_pretrained_model(self.model, progress=imported)
        self.assertEqual(self.target.read_bytes(), b"user checkpoint imported concurrently")

    def test_symlinks_cannot_redirect_partial_target_or_download_state_writes(self):
        outside = self.root / "outside"; outside.mkdir(); protected = outside / "protected"; protected.write_bytes(b"keep")
        self.target.parent.mkdir(parents=True)
        for link, target in [(self.partial, protected), (self.target, protected), (settings.models_dir / ".downloads", outside)]:
            with self.subTest(link=link):
                if link.is_dir():
                    import shutil
                    shutil.rmtree(link)
                link.symlink_to(target, target_is_directory=target.is_dir())
                try:
                    with patch("app.model_downloads.httpx.stream", side_effect=AssertionError("network forbidden")):
                        with self.assertRaises((ValueError, OSError)):
                            ensure_pretrained_model(self.model)
                    self.assertEqual(protected.read_bytes(), b"keep")
                finally:
                    link.unlink()


if __name__ == "__main__":
    unittest.main()
