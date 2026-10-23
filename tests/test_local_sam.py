from __future__ import annotations

import asyncio
import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from app.providers import local
from app.providers.base import ModelInfo


class LocalSamStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cvsprout-sam-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def checkpoint(self, name="sam3.pt"):
        path = self.root / name
        # Container-only fixture; a genuine torch.save round-trip is covered
        # below without permitting the status check to call torch.load.
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("archive/data.pkl", b"\x80\x02}q\x00.")
            archive.writestr("archive/version", b"3\n")
            archive.writestr("archive/data/0", b"\x00\x00\x00\x00")
        return path

    def test_missing_and_empty_weights_are_manual_setup_errors(self):
        for name, status in (
            ("sam2.1_t.pt", local.local_sam2_runtime_status),
            ("sam3.pt", lambda path: local.local_sam3_runtime_status(path, "/missing-sdk")),
        ):
            with self.subTest(name=name), patch.object(local, "_ultralytics_sam_status") as dependencies:
                path = self.root / name
                available, error = status(str(path))
                self.assertFalse(available)
                self.assertIn("手动放置", error)
                path.touch()
                available, error = status(str(path))
                self.assertFalse(available)
                self.assertIn("为空或不完整", error)
                dependencies.assert_not_called()

    def test_sam2_accepts_ultralytics_and_original_meta_tiny_filenames(self):
        with patch.object(local, "_ultralytics_sam_status", return_value=(True, "")):
            for name in ("sam2.1_t.pt", "sam2.1_hiera_tiny.pt"):
                self.assertEqual(local.local_sam2_runtime_status(str(self.checkpoint(name))), (True, ""))
            available, error = local.local_sam2_runtime_status(str(self.checkpoint("sam2.1_large.pt")))
            self.assertFalse(available)
            self.assertIn("单文件需命名", error)

    @unittest.skipUnless(importlib.util.find_spec("safetensors"), "requires safetensors")
    def test_sam2_hf_directory_requires_processor_files_and_keeps_local_fallback(self):
        directory = self.root / "sam2-hf"
        directory.mkdir()
        (directory / "config.json").write_text("{}")
        header = json.dumps({"weight": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode()
        (directory / "model.safetensors").write_bytes(len(header).to_bytes(8, "little") + header + b"\0" * 4)
        available, error = local.local_sam2_runtime_status(str(directory))
        self.assertFalse(available)
        self.assertIn("preprocessor_config.json", error)
        self.assertIn("processor_config.json", error)
        for name in ("preprocessor_config.json", "processor_config.json"):
            (directory / name).write_text("{}")
        with patch.object(local, "local_runtime_status", return_value=(True, "")) as fallback:
            self.assertEqual(local.local_sam2_runtime_status(str(directory)), (True, ""))
            fallback.assert_called_once_with(str(directory))

    def test_sam3_native_does_not_need_official_sdk_source(self):
        checkpoint = self.checkpoint()
        with patch.object(local, "_ultralytics_sam_status", return_value=(True, "")), \
             patch.object(local, "_ensure_sam3_import_path") as source:
            self.assertEqual(local.local_sam3_runtime_status(str(checkpoint), "/missing-sdk"), (True, ""))
            source.assert_not_called()

    def test_native_sam3_status_requires_packaged_tokenizer_but_no_clip_weights(self):
        clip = self.root / "clip"
        clip.mkdir()
        spec = SimpleNamespace(origin=str(clip / "__init__.py"))
        with patch.object(local, "find_spec", return_value=spec), \
             patch.object(local.importlib.metadata, "version", return_value="8.4.38"):
            available, error = local._ultralytics_sam_status(semantic=True)
            self.assertFalse(available)
            self.assertIn("词表缺失", error)
            (clip / "bpe_simple_vocab_16e6.txt.gz").write_bytes(b"fixture")
            self.assertEqual(local._ultralytics_sam_status(semantic=True), (True, ""))

    def test_sam3_keeps_complete_official_sdk_fallback(self):
        checkpoint = self.checkpoint()
        sdk = self.root / "sam3"
        (sdk / "assets").mkdir(parents=True)
        (sdk / "assets" / "bpe_simple_vocab_16e6.txt.gz").write_bytes(b"fixture")
        spec = SimpleNamespace(origin=str(sdk / "__init__.py"))
        with patch.object(local, "_ultralytics_sam_status", return_value=(False, "缺少 clip")), \
             patch.object(local, "_ensure_sam3_import_path"), \
             patch.object(local, "find_spec", return_value=spec):
            self.assertEqual(local.local_sam3_runtime_status(str(checkpoint), str(sdk)), (True, ""))
            (sdk / "assets" / "bpe_simple_vocab_16e6.txt.gz").unlink()
            available, error = local.local_sam3_runtime_status(str(checkpoint), str(sdk))
            self.assertFalse(available)
            self.assertIn("旧 SAM3 SDK 缺少", error)

    def test_large_zero_file_and_unrelated_zip_are_not_configured(self):
        path = self.root / "sam3.pt"
        with path.open("wb") as stream:
            stream.truncate(2 * 1024 * 1024)
        for contents in (None, "unrelated-zip"):
            if contents:
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("README.txt", "not a model")
            with self.subTest(contents=contents), patch.object(local, "_ultralytics_sam_status") as runtime:
                available, error = local.local_sam3_runtime_status(str(path), "/missing-sdk")
                self.assertFalse(available)
                self.assertIn("格式错误或不完整", error)
                runtime.assert_not_called()

    def test_truncated_zip_and_broken_local_header_are_rejected(self):
        path = self.checkpoint()
        original = path.read_bytes()
        with zipfile.ZipFile(path) as archive:
            offset = archive.getinfo("archive/data/0").header_offset
        broken_header = original[:offset] + b"FAIL" + original[offset + 4:]
        for contents in (original[:-22], original[:80], broken_header):
            with self.subTest(size=len(contents)):
                path.write_bytes(contents)
                available, error = local._checkpoint_status(path, "SAM3")
                self.assertFalse(available)
                self.assertIn("格式错误或不完整", error)

    def test_plain_legacy_pickle_is_explicitly_unsupported_without_execution(self):
        path = self.root / "sam3.pt"
        path.write_bytes(b"\x80\x02" + b"\0" * 128)
        available, error = local._checkpoint_status(path, "SAM3")
        self.assertFalse(available)
        self.assertIn("旧 pickle/tar", error)

    @unittest.skipUnless(importlib.util.find_spec("safetensors"), "requires safetensors")
    def test_safetensors_checks_metadata_and_truncation_without_getting_tensors(self):
        from safetensors import safe_open
        path = self.root / "model.safetensors"
        header = json.dumps({"weight": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode()
        valid = len(header).to_bytes(8, "little") + header + b"\0" * 4
        path.write_bytes(valid)
        with patch.object(safe_open, "get_tensor", side_effect=AssertionError("status must not allocate tensors")):
            self.assertEqual(local._checkpoint_status(path, "SAM2"), (True, ""))
            for contents in (valid[:-1], b"\0" * (2 * 1024 * 1024), b"\xff" * 8 + header):
                with self.subTest(size=len(contents)):
                    path.write_bytes(contents)
                    available, error = local._checkpoint_status(path, "SAM2")
                    self.assertFalse(available)
                    self.assertIn("格式错误或不完整", error)


class Sam2PromptTests(unittest.TestCase):
    def test_flat_rough_rectangle_and_polygon_become_box(self):
        for shape, points in (("rectangle", [25, 20, 5, 3]), ("polygon", [5, 3, 25, 4, 22, 20])):
            with self.subTest(shape=shape):
                self.assertEqual(local._sam2_prompts({"shape_type": shape, "points": points}),
                                 {"bboxes": [[5.0, 3.0, 25.0, 20.0]]})

    def test_positive_and_negative_points_belong_to_same_object(self):
        result = local._sam2_prompts({"points": [{"x": 4, "y": 5}, {"x": 20, "y": 21, "label": 0}]})
        self.assertEqual(result, {"points": [[[4.0, 5.0], [20.0, 21.0]]], "labels": [[1, 0]]})

    def test_bad_or_empty_prompt_fails_before_inference(self):
        for hints in ({}, {"points": [1, 2]}, {"shape_type": "rectangle", "points": [1, 2, 3]},
                      {"points": [{"x": float("nan"), "y": 1}]}, {"points": [{"x": 1, "y": 2, "label": 9}]}):
            with self.subTest(hints=hints), self.assertRaisesRegex(RuntimeError, "有效的正负点或粗框"):
                local._sam2_prompts(hints)


@unittest.skipUnless(importlib.util.find_spec("torch"), "requires model dependencies")
class LocalSamInferenceTests(unittest.TestCase):
    def setUp(self):
        import torch
        self.torch = torch
        self.temp = tempfile.TemporaryDirectory(prefix="cvsprout-sam-inference-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / "image.png"
        Image.new("RGB", (40, 30)).save(self.image)
        self.info = ModelInfo("sam-test", "SAM", "SAM", "", ["segment"], ["assist"], True)

    def test_real_minimal_torch_save_is_checked_without_deserializing_weights(self):
        path = self.root / "sam3.pt"
        self.torch.save({"model": {"detector.weight": self.torch.ones(1)}}, path)
        self.assertLess(path.stat().st_size, 1024 * 1024)
        with patch.object(self.torch, "load", side_effect=AssertionError("status must not unpickle")):
            self.assertEqual(local._checkpoint_status(path, "SAM3"), (True, ""))
            with path.open("r+b") as stream:
                stream.truncate(path.stat().st_size - 22)
            self.assertFalse(local._checkpoint_status(path, "SAM3")[0])

    def test_sam2_hf_numeric_box_runs_inference_and_returns_best_polygon(self):
        torch = self.torch
        masks = torch.zeros((1, 2, 30, 40), dtype=torch.bool)
        masks[0, 1, 4:20, 6:26] = True
        processor = Mock(return_value={"original_sizes": torch.tensor([[30, 40]]), "pixel_values": torch.zeros(1)})
        processor.post_process_masks.return_value = [masks]
        model = Mock(return_value=SimpleNamespace(pred_masks=masks, iou_scores=torch.tensor([0.1, 0.9])))
        provider = local.LocalSam2Provider(self.info, "hf-fixture", "cpu")
        with patch.object(local, "_load_sam2", return_value=(processor, model, "cpu")):
            result = provider._predict_sync(self.image, 40, 30, "object", {"shape_type": "rectangle", "points": [5, 3, 30, 25]})
        self.assertEqual(processor.call_args.kwargs["input_boxes"], [[[5.0, 3.0, 30.0, 25.0]]])
        self.assertNotIn("input_points", processor.call_args.kwargs)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].shape_type, "polygon")
        self.assertAlmostEqual(result[0].confidence, 0.9)

    def test_sam2_native_resets_image_and_preserves_negative_points(self):
        predictor = Mock()
        masks = self.torch.ones((1, 30, 40), dtype=self.torch.bool)
        predictor.inference_features.return_value = masks, self.torch.tensor([[0, 0, 40, 30, 0.8, 0]])
        processor = local._UltralyticsSam2Processor(predictor)
        provider = local.LocalSam2Provider(self.info, "sam2.1_t.pt", "cpu")
        hints = {"points": [{"x": 4, "y": 5, "label": 1}, {"x": 20, "y": 21, "label": 0}]}
        with patch.object(local, "_load_sam2", return_value=(processor, None, "cpu")):
            result = provider._predict_sync(self.image, 40, 30, "object", hints)
        self.assertEqual(len(result), 1)
        predictor.reset_image.assert_called_once()
        predictor.set_image.assert_called_once()
        self.assertEqual(predictor.inference_features.call_args.kwargs["points"], [[[4.0, 5.0], [20.0, 21.0]]])
        self.assertEqual(predictor.inference_features.call_args.kwargs["labels"], [[1, 0]])

    def test_sam2_missing_checkpoint_raises_before_loading_or_download(self):
        provider = local.LocalSam2Provider(self.info, str(self.root / "sam2.1_t.pt"), "cpu")
        with patch.object(local, "_load_sam2") as loader, self.assertRaisesRegex(RuntimeError, "手动放置"):
            asyncio.run(provider.predict(self.image, 40, 30, "", ["object"], {"points": [{"x": 1, "y": 1}]}))
        loader.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec("ultralytics"), "requires Ultralytics")
    def test_sam2_original_filename_uses_tiny_builder_and_caches_predictor(self):
        from ultralytics.models.sam import build, predict
        path = self.root / "sam2.1_hiera_tiny.pt"
        path.touch()
        predictor = Mock()
        with patch.dict(local._MODEL_CACHE, {}, clear=True), \
             patch.object(local, "local_sam2_runtime_status", return_value=(True, "")), \
             patch.object(build, "build_sam2_t") as builder, \
             patch.object(predict, "SAM2Predictor", return_value=predictor):
            first = local._load_sam2(str(path), "cpu")
            second = local._load_sam2(str(path), "cpu")
        builder.assert_called_once_with(checkpoint=str(path))
        predictor.setup_model.assert_called_once_with(model=builder.return_value, verbose=False)
        self.assertIsInstance(first[0], local._UltralyticsSam2Processor)
        self.assertIs(first[0], second[0])

    @unittest.skipUnless(importlib.util.find_spec("ultralytics"), "requires Ultralytics")
    def test_sam3_prefers_native_loader_and_cache_without_importing_sdk(self):
        from ultralytics.models.sam import predict
        path = self.root / "sam3.pt"
        predictor = Mock()
        fake_tokenizer = SimpleNamespace(SimpleTokenizer=Mock())
        with patch.dict(local._MODEL_CACHE, {}, clear=True), \
             patch.dict("sys.modules", {"clip": SimpleNamespace(), "clip.simple_tokenizer": fake_tokenizer}), \
             patch.object(local, "_checkpoint_status", return_value=(True, "")), \
             patch.object(local, "_ultralytics_sam_status", return_value=(True, "")), \
             patch.object(local, "_ensure_sam3_import_path", side_effect=AssertionError("must not import SDK")), \
             patch.object(predict, "SAM3SemanticPredictor", return_value=predictor) as factory:
            first = local._load_sam3(str(path), "/missing-sdk", "cpu")
            second = local._load_sam3(str(path), "/missing-sdk", "cpu")
        predictor.setup_model.assert_called_once_with(verbose=False)
        self.assertEqual(factory.call_args.kwargs["overrides"]["imgsz"], 1008)
        self.assertIsInstance(first[0], local._UltralyticsSam3Processor)
        self.assertIs(first[0], second[0])

    def test_local_checkpoint_loader_accepts_meta_state_and_never_calls_upstream(self):
        original = Mock(side_effect=AssertionError("must never download"))
        module = SimpleNamespace(_load_checkpoint=original)
        path = self.root / "sam2.1_hiera_tiny.pt"
        self.torch.save({"model": {"weight": self.torch.ones(1)}}, path)
        model = Mock()
        with local._local_sam_loader(module):
            self.assertIs(module._load_checkpoint(model, path), model)
            path.unlink()
            with self.assertRaises(FileNotFoundError):
                module._load_checkpoint(model, path)
        self.assertIs(module._load_checkpoint, original)
        original.assert_not_called()
        self.assertEqual(list(model.load_state_dict.call_args.args[0]), ["weight"])

    def test_sam3_bad_or_incomplete_weights_do_not_silently_run_random_model(self):
        original = Mock()
        module = SimpleNamespace(_load_checkpoint=original)
        path = self.root / "sam3.pt"
        model = Mock()
        self.torch.save({"weight": self.torch.ones(1)}, path)
        with self.assertRaisesRegex(RuntimeError, "不包含 detector"), local._local_sam_loader(module, semantic=True):
            module._load_checkpoint(model, path)
        self.assertIs(module._load_checkpoint, original)
        self.torch.save({"detector.weight": self.torch.ones(1), "tracker.weight": self.torch.ones(1)}, path)
        model.load_state_dict.return_value = SimpleNamespace(missing_keys=["backbone.weight"])
        with self.assertRaisesRegex(RuntimeError, "权重不完整"), local._local_sam_loader(module, semantic=True):
            module._load_checkpoint(model, path)
        self.assertEqual(list(model.load_state_dict.call_args.args[0]), ["weight"])
        self.assertIs(module._load_checkpoint, original)

    def test_sam3_native_reuses_image_for_labels_handles_empty_masks_and_low_threshold(self):
        predictor = Mock()
        predictor.args = SimpleNamespace(conf=0.5)
        predictor.inference_features.return_value = None, self.torch.zeros((0, 6))
        processor = local._UltralyticsSam3Processor(predictor)
        provider = local.LocalSam3Provider(self.info, "sam3.pt", "/missing-sdk", "cpu")
        with patch.object(local, "_load_sam3", return_value=(processor, None, "cpu")):
            result = provider._predict_sync(self.image, 40, 30, ["cat", "dog"], 0.01)
        self.assertEqual(result, [])
        self.assertEqual(predictor.args.conf, 0.01)
        predictor.reset_image.assert_called_once()
        predictor.set_image.assert_called_once()
        self.assertEqual(predictor.reset_prompts.call_count, 2)
        self.assertEqual([call.kwargs["text"] for call in predictor.inference_features.call_args_list], [["cat"], ["dog"]])
        self.assertTrue(all(call.args[1] == (30, 40) for call in predictor.inference_features.call_args_list))


if __name__ == "__main__":
    unittest.main()
