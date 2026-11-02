"""Real CPU numerical checks for the built-in system decoder export contract."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from app.recamera_export import raw_head_model, export_system_onnx, system_algorithm, system_contract


class SystemContractTest(unittest.TestCase):
    def test_family_and_output_contract_are_explicit(self):
        self.assertEqual(system_algorithm("yolov8n.pt"), "yolov8")
        self.assertEqual(system_algorithm("yolo11x.yaml"), "yolo11")
        for unsupported in ("yolo26n.pt", "yolo11n-seg.pt", "rtdetr-l.pt", "custom-yolo11.pt"):
            self.assertIsNone(system_algorithm(unsupported))
        contract = system_contract("yolo11", 320, ["cat", "dog"])
        self.assertEqual([x["shape"] for x in contract["outputs"]],
                         [[1, 64, 40, 40], [1, 2, 40, 40], [1, 64, 20, 20],
                          [1, 2, 20, 20], [1, 64, 10, 10], [1, 2, 10, 10]])
        for algorithm, size, classes in (("yolo26", 320, ["a"]), ("yolo11", 33, ["a"]),
                                         ("yolo11", 320, []), ("yolo11", 320, [1])):
            with self.assertRaises(ValueError):
                system_contract(algorithm, size, classes)


@unittest.skipUnless(all(importlib.util.find_spec(name) for name in ("torch", "ultralytics", "onnx", "onnxruntime")),
                     "Optional CPU model-export dependencies are unavailable")
class SystemExportNumericalTest(unittest.TestCase):
    def test_six_outputs_preserve_trained_head_and_match_onnx_runtime(self):
        import numpy as np
        import onnxruntime as ort
        import torch
        from ultralytics import YOLO

        threads = torch.get_num_threads()
        torch.set_num_threads(2)
        self.addCleanup(torch.set_num_threads, threads)
        torch.manual_seed(21)
        with tempfile.TemporaryDirectory(prefix="cvsprout-system-export-") as directory:
            root = Path(directory)
            for architecture, algorithm in (("yolov8n", "yolov8"), ("yolo11n", "yolo11")):
                with self.subTest(architecture=architecture):
                    detector = YOLO(architecture + ".yaml", verbose=False)
                    detector.model.eval()
                    if algorithm == "yolov8":
                        # Official older pretrained v8 checkpoints omit this key.
                        detector.model.yaml.pop("yaml_file", None)
                    detector.save(root / "source.pt")
                    # Reload the saved checkpoint: Ultralytics saves FP16 weights.
                    detector = YOLO(root / "source.pt", verbose=False)
                    detector.model.float().eval()
                    head = detector.model.model[-1]
                    converted = raw_head_model(detector)
                    sample = torch.rand(1, 3, 64, 64)
                    with torch.inference_mode():
                        raw = converted(sample)
                        expected = detector.model(sample)[0]
                    self.assertIs(detector.model.model[-1], head)
                    decoded = []
                    for index, stride in enumerate((8, 16, 32)):
                        boxes, classes = raw[2 * index:2 * index + 2]
                        height, width = boxes.shape[2:]
                        distribution = boxes.reshape(1, 4, 16, -1).softmax(2)
                        distances = (distribution * torch.arange(16).reshape(1, 1, 16, 1)).sum(2)
                        y, x = torch.meshgrid(torch.arange(height), torch.arange(width), indexing="ij")
                        centers = torch.stack((x, y)).reshape(1, 2, -1) + .5
                        start, end = centers - distances[:, :2], centers + distances[:, 2:]
                        xywh = torch.cat(((start + end) / 2, end - start), 1) * stride
                        decoded.append(torch.cat((xywh, classes.flatten(2)), 1))
                    torch.testing.assert_close(torch.cat(decoded, 2), expected, atol=2e-4, rtol=1e-5)
                    export_system_onnx({"source": root / "source.pt", "output": root / "model.onnx",
                                        "algorithm": algorithm, "task": "detect", "kwargs": {"imgsz": 64}})
                    options = ort.SessionOptions(); options.intra_op_num_threads = 2
                    session = ort.InferenceSession(str(root / "model.onnx"), options, providers=["CPUExecutionProvider"])
                    actual = session.run(None, {"images": sample.numpy()})
                    self.assertEqual(len(actual), 6)
                    for expected_tensor, actual_tensor in zip(raw, actual):
                        np.testing.assert_allclose(actual_tensor, expected_tensor.numpy(), atol=2e-5, rtol=1e-4)

    def test_end_to_end_head_is_rejected(self):
        from ultralytics import YOLO
        with self.assertRaisesRegex(ValueError, "标准非端到端"):
            raw_head_model(YOLO("yolo26n.yaml", verbose=False))
