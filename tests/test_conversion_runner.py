from __future__ import annotations

import importlib
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


class ConversionRunnerTest(unittest.TestCase):
    def setUp(self):
        # No application settings, database, GPUs or optional conversion SDKs.
        self.runner = importlib.import_module("app.conversion_runner")
        self.temp = tempfile.TemporaryDirectory(prefix="mdp-conversion-runner-")
        self.root = Path(self.temp.name)
        self.source = self.root / "source.onnx"
        self.source.write_bytes(b"onnx-fixture")
        self.request_path = self.root / "request.json"
        self.raw = {
            "operation": "rknn", "source": str(self.source),
            "output": str(self.root / "model.rknn"), "result_path": str(self.root / "result.json"),
            "task": "detect", "rknn": {"target_platform": "rv1126b", "quantized": False},
        }
        self.onnx_metadata = {
            "inputs": [{"name": "images", "shape": [1, 3, 64, 64], "dtype": "FLOAT"}],
            "outputs": [{"name": "output0", "shape": [1, 84, 84], "dtype": "FLOAT"}],
            "opsets": {"ai.onnx": 12}, "ir_version": 10,
            "model_metadata": {"end2end": "False"}, "contains_non_max_suppression": False,
        }

    def tearDown(self):
        self.temp.cleanup()

    def request(self):
        return self.runner._validate_request(self.raw, self.root, self.request_path)

    def sdk(self, *, stage=None, code=-1, invalid_header=False):
        owner = self

        class FakeRKNN:
            def __init__(self, **_kwargs):
                owner.converter = self
                self.released = False

            def config(self, **kwargs):
                self.config_options = kwargs
                return code if stage == "config" else 0

            def load_onnx(self, **_kwargs):
                return code if stage == "load_onnx" else 0

            def build(self, **kwargs):
                self.build_options = kwargs
                return code if stage == "build" else 0

            def export_rknn(self, path):
                Path(path).write_bytes(b"BADMODEL" if invalid_header else b"RKNN-fixture")
                return code if stage == "export_rknn" else 0

            def release(self):
                self.released = True

        return patch.dict(sys.modules, {"rknn.api": types.SimpleNamespace(RKNN=FakeRKNN)})

    def test_nonzero_sdk_return_never_reports_success_and_always_releases(self):
        for stage in ("config", "load_onnx", "build", "export_rknn"):
            with self.subTest(stage=stage):
                self.request_path.write_text(json.dumps(self.raw))
                previous = Path.cwd()
                try:
                    with (
                        self.sdk(stage=stage),
                        patch.object(self.runner, "_onnx_metadata", return_value=self.onnx_metadata),
                        patch.object(sys, "argv", ["conversion_runner", "--request", str(self.request_path)]),
                    ):
                        self.assertEqual(self.runner.main(), 1)
                finally:
                    os.chdir(previous)
                self.assertTrue(self.converter.released)
                self.assertFalse(Path(self.raw["output"]).exists())
                report = json.loads(Path(self.raw["result_path"]).read_text())
                self.assertIn(stage, report["error"]["message"])
                self.assertNotIn("artifact", report)
                Path(self.raw["result_path"]).unlink()

    def test_non_quantized_build_omits_dataset_and_records_custom_normalization(self):
        self.raw["rknn"].update(mean_values=[127, 127, 127], std_values=[127, 127, 127])
        with self.sdk(), patch.object(self.runner, "_onnx_metadata", return_value=self.onnx_metadata):
            metadata = self.runner.export_rknn(self.request())
        self.assertEqual(self.converter.build_options, {"do_quantization": False})
        self.assertEqual(self.converter.config_options["mean_values"], [[127, 127, 127]])
        self.assertFalse(self.converter.config_options["quant_img_RGB2BGR"])
        self.assertTrue(self.converter.released)
        self.assertFalse(metadata["preprocessing"]["normalization_matches_training"])
        self.assertTrue(metadata["warnings"])
        self.assertFalse(metadata["compatibility"]["board_validated"])

    def test_quantized_build_uses_private_calibration_and_rejects_count_mismatch(self):
        sample = self.root / "sample.png"
        sample.write_bytes(b"image-fixture")
        dataset = self.root / "calibration.txt"
        dataset.write_text(str(sample) + "\n")
        self.raw.update(dataset_path=str(dataset))
        self.raw["rknn"].update(quantized=True, calibration_count=2, calibration_source="version_train")
        with self.assertRaisesRegex(ValueError, "数量"):
            self.request()
        self.raw["rknn"]["calibration_count"] = 1
        with self.sdk(), patch.object(self.runner, "_onnx_metadata", return_value=self.onnx_metadata):
            metadata = self.runner.export_rknn(self.request())
        self.assertEqual(self.converter.build_options, {"do_quantization": True, "dataset": str(dataset)})
        self.assertEqual(self.converter.config_options["quantized_dtype"], "w8a8")
        self.assertEqual(metadata["calibration_count"], 1)
        self.assertEqual(metadata["source_onnx"]["inputs"][0]["dtype"], "FLOAT")
        self.assertEqual(metadata["compatibility"]["runtime_tensor_attributes"], "query_on_target")

    def system_fixture(self):
        from app.recamera_export import system_contract
        sample = self.root / "system-sample.png"
        sample.write_bytes(b"private-calibration-fixture")
        dataset = self.root / "system-calibration.txt"
        dataset.write_text(str(sample) + "\n")
        self.raw.update(dataset_path=str(dataset), preprocessing={"input_color": "rgb", "image_size": [64, 64],
                        "resize": "letterbox", "padding_position": "bottom_right", "padding_value": 114,
                        "resize_rounding": "floor_even", "preserve_aspect_ratio": True})
        self.raw["rknn"].update(deployment_profile="recamera_system", quantized=True, calibration_count=1,
                                mean_values=[0, 0, 0], std_values=[255, 255, 255])
        contract = system_contract("yolov8", 64, ["object", "other"])
        metadata = copy.deepcopy(self.onnx_metadata)
        metadata["model_metadata"]["cvsprout_system_model"] = json.dumps(contract)
        metadata["outputs"] = [{"name": f"output{index}", "shape": item["shape"], "dtype": "FLOAT"}
                               for index, item in enumerate(contract["outputs"])]
        return contract, metadata

    def test_system_profile_build_preserves_six_dfl_branches_and_int8_contract(self):
        contract, onnx = self.system_fixture()
        with self.sdk(), patch.object(self.runner, "_onnx_metadata", return_value=onnx):
            result = self.runner.export_rknn(self.request())
        self.assertTrue(self.converter.released)
        self.assertEqual(result["deployment_profile"], "recamera_system")
        self.assertEqual(result["system_model"], contract)
        self.assertEqual(result["precision"], "int8")
        self.assertEqual(result["source_onnx"]["outputs"], onnx["outputs"])
        self.assertEqual(result["postprocessing"]["executor"], "device_builtin")
        self.assertEqual(result["postprocessing"]["family"], "recamera_system_dfl")
        self.assertEqual(result["postprocessing"]["strides"], [8, 16, 32])
        self.assertFalse(result["postprocessing"]["board_validated"])
        self.assertFalse(result["compatibility"]["board_validated"])
        self.assertEqual(result["preprocessing"]["padding_position"], "bottom_right")
        self.assertEqual(result["preprocessing"]["resize_rounding"], "floor_even")
        self.assertEqual(self.converter.config_options["quantized_dtype"], "w8a8")
        self.assertEqual(self.converter.config_options["target_platform"], "rv1126b")
        self.assertEqual(self.converter.config_options["mean_values"], [[0, 0, 0]])
        self.assertEqual(self.converter.config_options["std_values"], [[255, 255, 255]])
        self.assertFalse(self.converter.config_options["quant_img_RGB2BGR"])
        self.assertEqual(self.converter.build_options, {"do_quantization": True, "dataset": self.raw["dataset_path"]})
        self.assertEqual(Path(self.raw["output"]).read_bytes(), b"RKNN-fixture")

    def test_system_profile_rejects_forged_or_incompatible_onnx_before_sdk_build(self):
        contract, base = self.system_fixture()
        cases = []

        def changed(label, change):
            metadata = copy.deepcopy(base)
            change(metadata)
            cases.append((label, metadata))

        changed("legacy single output", lambda value: value.update(outputs=self.onnx_metadata["outputs"]))
        changed("wrong class channels", lambda value: value["outputs"][1]["shape"].__setitem__(1, 3))
        changed("wrong branch order", lambda value: value["outputs"].reverse())
        changed("float16 branch", lambda value: value["outputs"][0].update(dtype="FLOAT16"))
        changed("non-square input", lambda value: value["inputs"][0]["shape"].__setitem__(3, 96))
        changed("missing contract", lambda value: value["model_metadata"].pop("cvsprout_system_model"))
        changed("malformed contract", lambda value: value["model_metadata"].update(cvsprout_system_model="{bad"))
        changed("NMS graph", lambda value: value.update(contains_non_max_suppression=True))
        for key, value in (("algorithm", "yolo26"), ("classes", []), ("profile", "generic"), ("input_size", 96), ("schema_version", 2)):
            forged = {**contract, key: value}
            changed(f"forged {key}", lambda data, forged=forged: data["model_metadata"].update(cvsprout_system_model=json.dumps(forged)))
        for label, metadata in cases:
            with self.subTest(label=label), self.sdk(), patch.object(self.runner, "_onnx_metadata", return_value=metadata):
                with self.assertRaises(ValueError):
                    self.runner.export_rknn(self.request())
                self.assertFalse(hasattr(self, "converter"), "Invalid contracts must not invoke the compiler")
                self.assertFalse(Path(self.raw["output"]).exists())
        self.raw["task"] = "segment"
        with self.sdk(), patch.object(self.runner, "_onnx_metadata", return_value=base), self.assertRaises(ValueError):
            self.runner.export_rknn(self.request())

    def test_system_profile_rejects_mismatched_calibration_preprocessing(self):
        _contract, metadata = self.system_fixture()
        original = copy.deepcopy(self.raw["preprocessing"])
        for key, value in (("padding_position", "center"), ("resize_rounding", "round"), ("padding_value", 0),
                           ("input_color", "bgr"), ("resize", "resize_stretch"), ("preserve_aspect_ratio", False)):
            self.raw["preprocessing"] = {**original, key: value}
            with self.subTest(key=key), self.sdk(), patch.object(self.runner, "_onnx_metadata", return_value=metadata):
                with self.assertRaises(ValueError):
                    self.runner.export_rknn(self.request())
                self.assertFalse(hasattr(self, "converter"))
                self.assertFalse(Path(self.raw["output"]).exists())

    def test_system_rknn_profile_validation_does_not_change_generic_custom_settings(self):
        valid = {"deployment_profile": "recamera_system", "target_platform": "rv1126b", "quantized": True}
        self.assertEqual(self.runner._rknn_config(valid)["deployment_profile"], "recamera_system")
        for change in ({"target_platform": "rk3588"}, {"quantized": False}, {"mean_values": [1, 0, 0]},
                       {"std_values": [127, 255, 255]}, {"deployment_profile": "unknown"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.runner._rknn_config({**valid, **change})
        generic = self.runner._rknn_config({"target_platform": "rk3576", "quantized": False,
                                            "mean_values": [1, 2, 3], "std_values": [100, 110, 120]})
        self.assertEqual(generic["deployment_profile"], "generic")
        self.assertEqual(generic["target_platform"], "rk3576")
        self.assertEqual(generic["mean_values"], [1, 2, 3])
        self.assertEqual(generic["std_values"], [100, 110, 120])

    def test_bad_sdk_artifact_is_not_published(self):
        with self.sdk(invalid_header=True), patch.object(self.runner, "_onnx_metadata", return_value=self.onnx_metadata):
            with self.assertRaisesRegex(RuntimeError, "文件头"):
                self.runner.export_rknn(self.request())
        self.assertTrue(self.converter.released)
        self.assertFalse(Path(self.raw["output"]).exists())
        self.assertFalse(list(self.root.glob("*.tmp.rknn")))

    def test_static_onnx_and_int8_only_target_constraints(self):
        for chip in ("rv1103", "rv1106", "rv1103b", "rv1106b"):
            with self.subTest(chip=chip):
                with self.assertRaisesRegex(ValueError, "INT8"):
                    self.runner._rknn_config({"target_platform": chip, "quantized": False})
                self.assertTrue(self.runner._rknn_config({"target_platform": chip, "quantized": True})["quantized"])
        for config in ({"target_platform": "rv1126"}, {"input_color": "bgr"}, {"std_values": [0, 255, 255]},
                       {"mean_values": [float("nan"), 0, 0]}, {"quantized": 1}, {"calibration_source": "live_project"}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                self.runner._rknn_config(config)
        for shape, dtype in (([2, 3, 64, 64], "FLOAT"), ([1, 3, "height", "width"], "FLOAT"), ([1, 3, 64, 64], "FLOAT16")):
            metadata = {**self.onnx_metadata, "inputs": [{"name": "images", "shape": shape, "dtype": dtype}]}
            with self.subTest(shape=shape, dtype=dtype), self.sdk(), patch.object(self.runner, "_onnx_metadata", return_value=metadata):
                with self.assertRaises(ValueError):
                    self.runner.export_rknn(self.request())

    def fake_onnx(self, *, end2end):
        class Dimension:
            def __init__(self, value):
                self.dim_value = value

            def HasField(self, _name):
                return True

        def tensor(name, shape):
            return types.SimpleNamespace(name=name, type=types.SimpleNamespace(tensor_type=types.SimpleNamespace(
                elem_type=1, shape=types.SimpleNamespace(dim=[Dimension(value) for value in shape]))))

        graph = types.SimpleNamespace(
            input=[tensor("images", [1, 3, 64, 64])],
            output=[tensor("output0", [1, 116, 84]), tensor("output1", [1, 32, 16, 16])],
            initializer=[], node=[],
        )
        model = types.SimpleNamespace(graph=graph, ir_version=10,
                                      opset_import=[types.SimpleNamespace(domain="", version=12)],
                                      metadata_props=[types.SimpleNamespace(key="end2end", value=str(end2end))])
        return types.SimpleNamespace(load=lambda *_args, **_kwargs: model,
                                     checker=types.SimpleNamespace(check_model=lambda _path: None),
                                     TensorProto=types.SimpleNamespace(DataType=types.SimpleNamespace(Name=lambda _code: "FLOAT")))

    def test_exported_end2end_metadata_wins_over_source_head_and_multi_outputs_are_preserved(self):
        for end2end in (False, True):
            with self.subTest(end2end=end2end):
                exported = self.root / f"exported-{end2end}.onnx"
                exported.write_bytes(b"export-fixture")
                owner = self

                class FakeYOLO:
                    def __init__(self, *_args, **_kwargs):
                        owner.assertEqual(os.environ["YOLO_AUTOINSTALL"], "false")
                        self.model = types.SimpleNamespace(end2end=not end2end)

                    def export(self, **_kwargs):
                        return str(exported)

                request = {"source": self.source, "output": self.root / f"output-{end2end}.onnx", "task": "segment",
                           "kwargs": {"format": "onnx", "imgsz": 64, "device": "cpu"}}
                with patch.dict(os.environ), patch.dict(sys.modules, {
                    "ultralytics": types.SimpleNamespace(YOLO=FakeYOLO), "onnx": self.fake_onnx(end2end=end2end),
                }):
                    metadata = self.runner.export_ultralytics(request)
                self.assertEqual(metadata["postprocessing"]["end2end"], end2end)
                self.assertEqual(metadata["postprocessing"]["external_nms_required"], not end2end)
                self.assertEqual(metadata["postprocessing"]["contract_source"], "exporter_metadata")
                self.assertEqual([output["name"] for output in metadata["outputs"]], ["output0", "output1"])
                self.assertEqual(metadata["outputs"][1]["shape"], [1, 32, 16, 16])
                self.assertEqual(metadata["preprocessing"]["image_size"], [64, 64])
                self.assertEqual(metadata["preprocessing"]["padding_value"], 114)
        self.assertFalse(self.runner._postprocessing("classify")["external_nms_required"])

    def rtdetr_model(self, exported):
        class RTDETRDecoder:
            pass

        class FakeYOLO:
            def __init__(self, *_args, **_kwargs):
                self.model = types.SimpleNamespace(model=[RTDETRDecoder()], end2end=False)

            def export(self, **_kwargs):
                return str(exported)

        return FakeYOLO

    def test_rtdetr_exports_use_stretch_and_normalized_query_contract_for_renamed_checkpoints(self):
        for format_name in ("onnx", "torchscript"):
            with self.subTest(format_name=format_name):
                exported = self.root / f"exported.{format_name}"
                exported.write_bytes(b"export-fixture")
                request = {"source": self.root / "uploaded-custom.pt", "output": self.root / f"output.{format_name}",
                           "task": "detect", "kwargs": {"format": format_name, "imgsz": 128, "device": "cpu"},
                           "preprocessing": {"geometry": "letterbox", "preserve_aspect_ratio": True, "padding_value": 114}}
                source_metadata = {**self.onnx_metadata,
                                   "inputs": [{"name": "images", "shape": [1, 3, 128, 128], "dtype": "FLOAT"}],
                                   "outputs": [{"name": "output0", "shape": [1, 300, 6], "dtype": "FLOAT"}]}
                with patch.dict(os.environ), patch.dict(sys.modules, {"ultralytics": types.SimpleNamespace(YOLO=self.rtdetr_model(exported))}), \
                        patch.object(self.runner, "_onnx_metadata", return_value=source_metadata), \
                        patch.object(self.runner, "_torchscript_metadata", return_value={"imgsz": [128, 128], "end2end": False}):
                    metadata = self.runner.export_ultralytics(request)
                self.assertEqual(metadata["model_backend"], "rtdetr")
                preprocessing = metadata["preprocessing"]
                self.assertEqual(preprocessing["geometry"], "resize_stretch")
                self.assertFalse(preprocessing["preserve_aspect_ratio"])
                self.assertIsNone(preprocessing["padding_value"])
                self.assertEqual(preprocessing["image_size"], [128, 128])
                postprocessing = metadata["postprocessing"]
                self.assertEqual(postprocessing["output_layout"], "B,N,4+nc")
                self.assertEqual(postprocessing["box_format"], "xywh")
                self.assertEqual(postprocessing["box_coordinates"], "normalized")
                self.assertEqual(postprocessing["selection"], "per_query_class_argmax_then_confidence")
                self.assertEqual(postprocessing["class_scores"], "sigmoid_probabilities")
                self.assertFalse(postprocessing["external_nms_required"])
                self.assertFalse(postprocessing["score_activation_required"])
                self.assertEqual(postprocessing["contract_source"], "model_head")
                self.assertTrue(request["output"].is_file())
                if format_name == "onnx":
                    self.assertEqual(metadata["outputs"][0]["shape"], [1, 300, 6])

    def test_rtdetr_name_does_not_override_a_standard_yolo_head(self):
        exported = self.root / "exported.onnx"
        exported.write_bytes(b"export-fixture")

        class FakeYOLO:
            def __init__(self, *_args, **_kwargs):
                self.model = types.SimpleNamespace(model=[object()], end2end=False)

            def export(self, **_kwargs):
                return str(exported)

        request = {"source": self.root / "rtdetr-l.pt", "output": self.root / "output.onnx", "task": "detect",
                   "kwargs": {"format": "onnx", "imgsz": 64, "device": "cpu"}}
        with patch.dict(os.environ), patch.dict(sys.modules, {"ultralytics": types.SimpleNamespace(YOLO=FakeYOLO)}), \
                patch.object(self.runner, "_onnx_metadata", return_value=self.onnx_metadata):
            metadata = self.runner.export_ultralytics(request)
        self.assertEqual(metadata["model_backend"], "yolo")
        self.assertEqual(metadata["preprocessing"]["geometry"], "letterbox")
        self.assertTrue(metadata["postprocessing"]["external_nms_required"])

    def test_rtdetr_rejects_unsupported_export_options_before_export(self):
        for changes in ({"dynamic": True}, {"nms": True}, {"imgsz": 64}, {"task": "segment"}):
            with self.subTest(changes=changes):
                request = {"source": self.source, "output": self.root / "output.onnx", "task": changes.get("task", "detect"),
                           "kwargs": {"format": "onnx", "imgsz": 128, "device": "cpu", **{key: value for key, value in changes.items() if key != "task"}}}
                model_class = self.rtdetr_model(self.root / "missing.onnx")
                with patch.dict(os.environ), patch.dict(sys.modules, {"ultralytics": types.SimpleNamespace(YOLO=model_class)}), \
                        patch.object(model_class, "export", side_effect=AssertionError("unsupported export must not run")):
                    with self.assertRaisesRegex(ValueError, "RT-DETR"):
                        self.runner.export_ultralytics(request)
                self.assertFalse(request["output"].exists())

    def test_rtdetr_unexpected_nms_graph_is_not_published(self):
        exported = self.root / "exported.onnx"
        exported.write_bytes(b"export-fixture")
        request = {"source": self.source, "output": self.root / "output.onnx", "task": "detect",
                   "kwargs": {"format": "onnx", "imgsz": 128, "device": "cpu"}}
        with patch.dict(os.environ), patch.dict(sys.modules, {"ultralytics": types.SimpleNamespace(YOLO=self.rtdetr_model(exported))}), \
                patch.object(self.runner, "_onnx_metadata", return_value={**self.onnx_metadata, "contains_non_max_suppression": True}):
            with self.assertRaisesRegex(ValueError, "意外包含 NMS"):
                self.runner.export_ultralytics(request)
        self.assertFalse(request["output"].exists())

    def fake_trt(self, engine):
        owner = self

        class Logger:
            WARNING = 1

            def __init__(self, _level):
                pass

        class Runtime:
            def __init__(self, _logger):
                pass

            def deserialize_cuda_engine(self, content):
                owner.assertEqual(content, b"serialized-plan")
                return engine

        return patch.dict(sys.modules, {"tensorrt": types.SimpleNamespace(Logger=Logger, Runtime=Runtime, __version__="10.16.0.72")})

    def engine_file(self):
        header = json.dumps({"batch": 2, "imgsz": [64, 64], "end2end": True}).encode()
        path = self.root / "exported.engine"
        path.write_bytes(len(header).to_bytes(4, "little") + header + b"serialized-plan")
        return path, len(header) + 4

    def test_invalid_engine_fails_but_tensor_metadata_compatibility_error_is_explicit(self):
        path, offset = self.engine_file()
        with self.fake_trt(None), self.assertRaisesRegex(RuntimeError, "产物未发布"):
            self.runner._engine_metadata(path)
        # Deserialization succeeded; only the optional legacy tensor API is absent.
        with self.fake_trt(object()):
            metadata = self.runner._engine_metadata(path)
        self.assertEqual(metadata["container"], "ultralytics_metadata_prefix")
        self.assertEqual(metadata["payload_offset_bytes"], offset)
        self.assertTrue(metadata["exporter_metadata"]["end2end"])
        self.assertIn("inspection_error", metadata)
        self.assertNotIn("optimization_profiles", metadata)

    def test_engine_deserialization_failure_prevents_artifact_publication(self):
        exported, _ = self.engine_file()

        class FakeYOLO:
            def __init__(self, *_args, **_kwargs):
                self.model = types.SimpleNamespace(end2end=True)

            def export(self, **_kwargs):
                return str(exported)

        request = {"source": self.source, "output": self.root / "model.engine", "task": "detect",
                   "kwargs": {"format": "engine", "imgsz": 64, "device": "0"}}
        with patch.dict(os.environ), patch.dict(sys.modules, {"ultralytics": types.SimpleNamespace(YOLO=FakeYOLO)}), self.fake_trt(None):
            with self.assertRaisesRegex(RuntimeError, "产物未发布"):
                self.runner.export_ultralytics(request)
        self.assertFalse(request["output"].exists())

    def test_engine_records_imported_sdk_version_without_generic_distribution(self):
        exported, _ = self.engine_file()

        class FakeYOLO:
            def __init__(self, *_args, **_kwargs):
                self.model = types.SimpleNamespace(end2end=True)

            def export(self, **_kwargs):
                return str(exported)

        request = {"source": self.source, "output": self.root / "model.engine", "task": "detect",
                   "kwargs": {"format": "engine", "imgsz": 64, "device": "0"}}
        torch_stub = types.SimpleNamespace(
            cuda=types.SimpleNamespace(get_device_name=lambda _index: "Fixture GPU", get_device_capability=lambda _index: (8, 9)),
            version=types.SimpleNamespace(cuda="12.4"),
        )
        with (
            patch.dict(os.environ),
            patch.dict(sys.modules, {"ultralytics": types.SimpleNamespace(YOLO=FakeYOLO), "torch": torch_stub}),
            self.fake_trt(types.SimpleNamespace(num_io_tensors=0)),
            patch.object(self.runner, "_versions", return_value={"python": "3.12", "tensorrt-cu12": "10.16.0.72"}),
        ):
            metadata = self.runner.export_ultralytics(request)
        self.assertEqual(metadata["tool_versions"]["tensorrt"], "10.16.0.72")
        self.assertEqual(metadata["tool_versions"]["tensorrt-cu12"], "10.16.0.72")
        self.assertTrue(request["output"].exists())

    def test_private_paths_and_unsupported_export_configs_are_rejected(self):
        self.raw["output"] = str(self.root.parent / "escape.rknn")
        with self.assertRaisesRegex(ValueError, "任务目录"):
            self.request()
        for kwargs in ({"format": "rknn"}, {"format": "engine", "device": "cpu"},
                       {"format": "engine", "device": "0", "int8": True}, {"format": "onnx", "device": "auto"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.runner._export_config(kwargs)


if __name__ == "__main__":
    unittest.main()
