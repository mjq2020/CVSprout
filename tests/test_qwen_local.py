from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image
from safetensors.numpy import save_file

from app.config import settings
from app.providers import local, qwen_local
from app.providers.base import ModelInfo
from app.providers.execution import unload_annotation_models
from app.providers.qwen import annotation_instruction


class QwenLocalTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import torch

        self.torch = torch
        self.temp = tempfile.TemporaryDirectory(prefix="cvsprout-qwen-local-")
        self.root = Path(self.temp.name)
        self.model_path = self.root / "model"
        self.model_path.mkdir()
        self.write_json("config.json", {"model_type": "qwen3_5"})
        self.write_json("preprocessor_config.json", {"image_processor_type": "Qwen2VLImageProcessor"})
        self.write_json("tokenizer_config.json", {"tokenizer_class": "Qwen2Tokenizer"})
        self.write_json("tokenizer.json", {"test": "not loaded"})
        (self.model_path / "chat_template.jinja").write_text("fixture")
        save_file({"test.weight": np.ones((2,), dtype=np.float32)}, self.model_path / "model.safetensors")
        self.image_path = self.root / "image.png"
        Image.new("RGB", (200, 100), "red").save(self.image_path)
        self.worker_url = settings.worker_url
        object.__setattr__(settings, "worker_url", "")
        self.info = ModelInfo("qwen-local-test", "Qwen fixture", "qwen", "", [], ["auto"], True)
        self.provider = qwen_local.LocalQwenProvider(self.info, str(self.model_path), "cpu")
        self.support = patch.object(qwen_local, "_runtime_support"); self.support.start()
        self.cached_models = dict(local._MODEL_CACHE)
        self.cached_devices = dict(local._MODEL_DEVICES)
        local._MODEL_CACHE.clear(); local._MODEL_DEVICES.clear()

    def tearDown(self):
        self.support.stop()
        local._MODEL_CACHE.clear(); local._MODEL_CACHE.update(self.cached_models)
        local._MODEL_DEVICES.clear(); local._MODEL_DEVICES.update(self.cached_devices)
        object.__setattr__(settings, "worker_url", self.worker_url)
        self.temp.cleanup()

    def write_json(self, name, value):
        (self.model_path / name).write_text(json.dumps(value))

    def models(self, response=None, *, input_tokens=4, output_tokens=None, generate=None):
        owner = self
        self.output_text = json.dumps(response if response is not None else {"objects": []})

        class Processor:
            def apply_chat_template(self, messages, **kwargs):
                owner.template_kwargs = kwargs
                owner.messages = messages
                owner.image_size = messages[0]["content"][0]["image"].size
                return {"input_ids": owner.torch.ones((1, input_tokens), dtype=owner.torch.long),
                        "pixel_values": owner.torch.ones((1, 3))}

            def decode(self, tokens, **kwargs):
                owner.decoded_ids = tokens.tolist()
                owner.decode_kwargs = kwargs
                return owner.output_text

        class Model:
            generation_config = types.SimpleNamespace(eos_token_id=2)

            def to(self, device):
                owner.loaded_device = device
                return self

            def eval(self):
                return self

            def generate(self, **kwargs):
                owner.generate_kwargs = kwargs
                if generate:
                    generate()
                tail = output_tokens if output_tokens is not None else [7, 8, 2]
                return owner.torch.cat([kwargs["input_ids"], owner.torch.tensor([tail])], dim=1)

        return Processor(), Model(), "cpu"

    async def predict(self, payload, hints=None, **options):
        with patch.object(qwen_local, "_load_qwen", return_value=self.models(payload, **options)):
            return await self.provider.predict(self.image_path, 200, 100, "标注目标", ["Car", "Other"], hints)

    async def test_mixed_project_keeps_default_detection_after_task_hint_validation(self):
        from app.annotation_inference import inference_hints, prepare_inference

        context = prepare_inference({"task_type": "mixed"}, [{"name": "Car"}],
                                    {"hints": {"task_type": "segment"}}, "Car")
        hints = inference_hints(context, {"id": 1}, 1)
        predictions = await self.predict({"objects": [{"label": "Car", "bbox": [100, 200, 300, 600]}]}, hints)
        self.assertEqual(len(predictions), 1)
        self.assertEqual(predictions[0].shape_type, "rectangle")
        self.assertEqual(predictions[0].points, [20, 20, 60, 60])

    def test_status_checks_local_files_without_constructing_or_downloading_models(self):
        with patch.object(qwen_local, "_load_qwen", side_effect=AssertionError("status loaded weights")):
            self.assertEqual(qwen_local.local_qwen_runtime_status(str(self.model_path)), (True, ""))

    def test_status_checks_every_shard_and_rejects_truncation(self):
        (self.model_path / "model.safetensors").unlink()
        names = ["model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors"]
        for index, name in enumerate(names):
            save_file({f"weight.{index}": np.zeros((2,), dtype=np.float32)}, self.model_path / name)
        self.write_json("model.safetensors.index.json", {"weight_map": {f"weight.{i}": name for i, name in enumerate(names)}})
        self.assertTrue(qwen_local.local_qwen_runtime_status(str(self.model_path))[0])
        target = self.model_path / names[1]
        target.write_bytes(target.read_bytes()[:-1])
        self.assertFalse(qwen_local.local_qwen_runtime_status(str(self.model_path))[0])
        target.unlink()
        available, error = qwen_local.local_qwen_runtime_status(str(self.model_path))
        self.assertFalse(available); self.assertIn(names[1], error)

    def test_status_rejects_missing_templates_and_unknown_model_types(self):
        (self.model_path / "chat_template.jinja").unlink()
        self.assertIn("模板", qwen_local.local_qwen_runtime_status(str(self.model_path))[1])
        self.write_json("tokenizer_config.json", {"chat_template": "inline fixture"})
        self.assertTrue(qwen_local.local_qwen_runtime_status(str(self.model_path))[0])
        self.write_json("config.json", {"model_type": "qwen2_vl"})
        self.assertFalse(qwen_local.local_qwen_runtime_status(str(self.model_path))[0])

    def test_status_rejects_index_path_escape_and_missing_tensor_names(self):
        for mapping in ({"weight": "../escape.safetensors"}, {"missing": "model.safetensors"}, {}):
            self.write_json("model.safetensors.index.json", {"weight_map": mapping})
            self.assertFalse(qwen_local.local_qwen_runtime_status(str(self.model_path))[0])

    def test_status_reports_unsupported_runtime_instead_of_ready(self):
        with patch.object(qwen_local, "_runtime_support", side_effect=RuntimeError("Transformers 4.57 不支持 Qwen3.5")):
            available, error = qwen_local.local_qwen_runtime_status(str(self.model_path))
        self.assertFalse(available); self.assertIn("4.57", error)

    def test_loader_is_offline_and_shares_existing_cache_and_cleanup(self):
        import transformers

        processor, model, _ = self.models()
        with patch.object(transformers.AutoProcessor, "from_pretrained", return_value=processor) as load_processor, \
                patch.object(transformers.AutoModelForImageTextToText, "from_pretrained", return_value=model) as load_model:
            first = qwen_local._load_qwen(str(self.model_path), "cpu")
            self.assertEqual(first, qwen_local._load_qwen(str(self.model_path), "cpu"))
        self.assertEqual(load_processor.call_count, 1); self.assertEqual(load_model.call_count, 1)
        for call in (load_processor.call_args, load_model.call_args):
            self.assertTrue(call.kwargs["local_files_only"])
            self.assertFalse(call.kwargs["trust_remote_code"])
        self.assertTrue(load_model.call_args.kwargs["use_safetensors"])
        self.assertEqual(load_model.call_args.kwargs["dtype"], self.torch.float32)
        self.assertNotIn("device_map", load_model.call_args.kwargs)
        self.assertIn(("qwen3.5", str(self.model_path), "cpu"), local._MODEL_CACHE)
        unload_annotation_models()
        self.assertFalse(local._MODEL_CACHE)

    def test_insufficient_host_memory_fails_before_loading(self):
        import transformers

        with patch("psutil.virtual_memory", return_value=types.SimpleNamespace(available=1)), \
                patch.object(transformers.AutoProcessor, "from_pretrained") as processor, \
                patch.object(transformers.AutoModelForImageTextToText, "from_pretrained") as model:
            with self.assertRaisesRegex(qwen_local.QwenLocalError, "系统内存不足.*完整权重至少"):
                qwen_local._load_qwen(str(self.model_path), "cpu")
        processor.assert_not_called(); model.assert_not_called()

    def test_minimum_memory_uses_target_dtype_and_checks_cuda_without_allocating(self):
        with patch("psutil.virtual_memory", return_value=types.SimpleNamespace(available=4)), \
                patch.object(self.torch.cuda, "mem_get_info", return_value=(4, 16)) as capacity:
            qwen_local._check_weight_memory(self.model_path, "cuda:2", self.torch.float16)
            capacity.assert_called_once_with("cuda:2")
            with self.assertRaisesRegex(qwen_local.QwenLocalError, "系统内存不足"):
                qwen_local._check_weight_memory(self.model_path, "cpu", self.torch.float32)
        with patch("psutil.virtual_memory", return_value=types.SimpleNamespace(available=1000)), \
                patch.object(self.torch.cuda, "mem_get_info", return_value=(1, 16)):
            with self.assertRaisesRegex(qwen_local.QwenLocalError, "cuda:2 显存不足"):
                qwen_local._check_weight_memory(self.model_path, "cuda:2", self.torch.float16)

    def test_memory_metadata_cache_invalidates_when_weights_change(self):
        with patch("psutil.virtual_memory", return_value=types.SimpleNamespace(available=8)):
            qwen_local._check_weight_memory(self.model_path, "cpu", self.torch.float32)
            save_file({"test.weight": np.ones((3,), dtype=np.float32)}, self.model_path / "model.safetensors")
            with self.assertRaises(qwen_local.QwenLocalError):
                qwen_local._check_weight_memory(self.model_path, "cpu", self.torch.float32)

    async def test_detect_uses_no_thinking_and_decodes_only_generated_tokens(self):
        result = await self.predict({"objects": [{"label": "car", "bbox": [100, 200, 900, 800], "confidence": .9}]})
        self.assertEqual(result[0].label, "Car")
        self.assertEqual(result[0].points, [20, 20, 180, 80])
        self.assertFalse(self.template_kwargs["enable_thinking"])
        self.assertTrue(self.template_kwargs["tokenize"])
        self.assertFalse(self.generate_kwargs["do_sample"])
        self.assertEqual(self.generate_kwargs["max_new_tokens"], 2048)
        self.assertEqual(self.decoded_ids, [7, 8, 2])
        self.assertTrue(self.decode_kwargs["skip_special_tokens"])
        self.assertEqual(self.messages[0]["content"][0]["type"], "image")

    async def test_native_qwen_grounding_list_and_bbox_2d_are_explicitly_normalized(self):
        for payload in ([{"label": "car", "bbox": [100, 200, 900, 800]}],
                        {"annotations": [{"label": "car", "bbox_2d": [100, 200, 900, 800]}]}):
            result = await self.predict(payload)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].label, "Car")
            self.assertEqual(result[0].points, [20, 20, 180, 80])

    async def test_nonempty_invalid_predictions_raise_instead_of_succeeding_as_empty(self):
        for payload in (["car"], [{"label": "unknown", "bbox": [0, 0, 1000, 1000]}],
                        [{"label": "Car", "bbox_2d": [1, 2]}]):
            with self.assertRaisesRegex(qwen_local.QwenLocalError, "有效标注"):
                await self.predict(payload)
        self.assertEqual(await self.predict([]), [])

    async def test_segmentation_rejects_boxes_and_unknown_labels(self):
        result = await self.predict({"objects": [
            {"label": "Car", "polygon": [0, 0, 1000, 0, 1000, 1000]},
            {"label": "Car", "bbox": [0, 0, 1000, 1000]},
            {"label": "unknown", "polygon": [0, 0, 1000, 0, 1000, 1000]},
        ]}, {"task_type": "segment"})
        self.assertEqual(len(result), 1); self.assertEqual(result[0].shape_type, "polygon")
        self.assertEqual(result[0].points, [0, 0, 200, 0, 200, 100])

    async def test_classification_returns_one_allowed_class(self):
        result = await self.predict({"objects": [{"label": "Car", "confidence": .3}, {"label": "Other", "confidence": .8}]}, {"task_type": "classify"})
        self.assertEqual(len(result), 1); self.assertEqual(result[0].label, "Other")
        self.assertEqual(result[0].points, []); self.assertEqual(result[0].shape_type, "classification")

    async def test_pose_enforces_point_count_and_visibility(self):
        point = {"label": "Car", "bbox": [0, 0, 1000, 1000], "keypoints": [
            {"x": 500, "y": 500, "visibility": 2}, {"x": 900, "y": 900, "visibility": 0}]}
        result = await self.predict({"objects": [point, {**point, "keypoints": point["keypoints"][:1]}]},
                                    {"task_type": "pose", "keypoint_schema": {"names": ["tip", "base"]}})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].keypoints, [{"x": 100, "y": 50, "visibility": 2}, {"x": 0, "y": 0, "visibility": 0}])

    async def test_invalid_json_and_missing_object_list_are_errors(self):
        for payload in (None, {"description": "a car"}, {"objects": "not a list"}):
            with patch.object(qwen_local, "_load_qwen", return_value=self.models(payload)):
                if payload is None:
                    self.output_text = "not JSON"
                with self.assertRaisesRegex(RuntimeError, "JSON|objects"):
                    await self.provider.predict(self.image_path, 200, 100, "test", ["Car"])

    async def test_image_is_bounded_while_coordinates_keep_original_size(self):
        self.provider.max_image_pixels = 4096
        result = await self.predict({"objects": [{"label": "Car", "bbox": [0, 0, 1000, 1000]}]})
        self.assertLessEqual(self.image_size[0] * self.image_size[1], 4096)
        self.assertEqual(result[0].points, [0, 0, 200, 100])
        self.assertEqual(self.template_kwargs["processor_kwargs"]["images_kwargs"]["size"]["longest_edge"], 4096)

    async def test_token_limits_fail_before_inference_or_incomplete_output(self):
        with self.assertRaisesRegex(ValueError, "8192"):
            await self.predict({"objects": []}, input_tokens=8193, generate=lambda: self.fail("overlong prompt was generated"))
        self.provider.max_new_tokens = 2
        with self.assertRaisesRegex(RuntimeError, "token 上限"):
            await self.predict({"objects": []}, output_tokens=[7, 8])

    async def test_oom_error_explains_full_model_memory_requirement(self):
        def fail():
            raise self.torch.OutOfMemoryError("fixture OOM")
        with self.assertRaisesRegex(RuntimeError, "单个设备加载完整权重"):
            await self.predict(generate=fail, payload={"objects": []})

    async def test_native_inference_is_serialized_without_blocking_event_loop(self):
        active = 0
        maximum = 0
        threads = []

        def generate():
            nonlocal active, maximum
            active += 1; maximum = max(maximum, active)
            threads.append(threading.get_ident())
            time.sleep(.08)
            active -= 1

        with patch.object(qwen_local, "_load_qwen", return_value=self.models(generate=generate)):
            tasks = [asyncio.create_task(self.provider.predict(self.image_path, 200, 100, "test", ["Car"])) for _ in range(2)]
            await asyncio.wait_for(asyncio.sleep(.01), timeout=.05)
            self.assertFalse(all(task.done() for task in tasks))
            await asyncio.gather(*tasks)
        self.assertEqual(maximum, 1)
        self.assertTrue(all(identifier != threading.get_ident() for identifier in threads))

    def test_constructor_and_shared_prompts_preserve_bounds_and_four_tasks(self):
        for config in ({"max_new_tokens": 0}, {"max_new_tokens": 4097}, {"max_image_pixels": 1}, {"max_image_pixels": 2**24}):
            with self.assertRaises(ValueError):
                qwen_local.LocalQwenProvider(self.info, str(self.model_path), **config)
        for task, marker in (("detect", '"bbox"'), ("segment", '"polygon"'), ("classify", '"classification"'), ("pose", '"keypoints"')):
            instruction = annotation_instruction("target", ["Car"], {"task_type": task})
            self.assertIn(marker, instruction); self.assertIn("0 到 1000", instruction)


if __name__ == "__main__":
    unittest.main()
