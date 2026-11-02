"""Explicit export contract for the device's built-in YOLO DFL decoder.

The device uses six native INT8 outputs, ordered box/class for strides 8/16/32.
This host-only module exports those trained branches; no decoder is installed
in the Connector. Reference: Rockchip Model Zoo bad6c733 and the RV1126B system
YOLOv10 decoder shared by yolov8/yolo11. The optional score-sum branch is omitted.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

PROFILE = "rv1126b-yolo-dfl-int8-v1"


def system_algorithm(architecture: str) -> str | None:
    value = str(architecture).lower()
    match = re.fullmatch(r"(yolov8|yolo11)(?:[nslmx])?(?:\.yaml|\.pt)?", value)
    return match[1] if match else None


def system_contract(algorithm: str, size: int, classes: list[str]) -> dict:
    if (algorithm not in {"yolov8", "yolo11"} or not isinstance(classes, list)
            or not classes or any(not isinstance(name, str) or not name.strip() for name in classes)):
        raise ValueError("系统模型需要 YOLOv8 / YOLO11 检测架构和类别表")
    if type(size) is not int or not 32 <= size <= 2048 or size % 32:
        raise ValueError("系统模型尺寸必须为 32–2048 之间的 32 倍数")
    return {"schema_version": 1, "profile": PROFILE, "algorithm": algorithm,
            "category": "Detection", "input_size": size, "classes": classes,
            "preprocessing": {"color_space": "RGB", "dtype": "uint8", "resize": "letterbox",
                              "padding_value": 114, "padding_position": "bottom_right"},
            "outputs": [{"shape": [1, channels, size // stride, size // stride], "dtype": "INT8", "role": role}
                        for stride in (8, 16, 32) for channels, role in ((64, "box"), (len(classes), "class"))]}


def raw_head_model(detector):
    """Replace only the export copy's final head, retaining every trained weight."""
    import copy
    import torch
    from ultralytics.nn.modules.head import Detect

    model = copy.deepcopy(detector.model).cpu().float().eval()
    head = model.model[-1]
    if (type(head) is not Detect or head.reg_max != 16 or head.nl != 3
            or bool(head.end2end) or [int(value) for value in head.stride] != [8, 16, 32]):
        raise ValueError("系统模型仅支持 stride=8/16/32、reg_max=16 的标准非端到端 Detect 头")

    class SystemDFLHead(torch.nn.Module):
        def __init__(self, original):
            super().__init__()
            self.cv2, self.cv3 = original.cv2, original.cv3
            self.i, self.f, self.type = original.i, original.f, original.type
            self.np = getattr(original, "np", 0)

        def forward(self, features):
            return tuple(output for index in range(3)
                         for output in (self.cv2[index](features[index]), self.cv3[index](features[index]).sigmoid()))

    model.model[-1] = SystemDFLHead(head)
    return model


def export_system_onnx(request: dict) -> dict:
    import os
    os.environ["YOLO_AUTOINSTALL"] = "false"
    import torch
    import onnx
    from ultralytics import YOLO
    from ultralytics.nn.tasks import yaml_model_load

    algorithm = request.get("algorithm")
    if request["task"] != "detect" or algorithm not in {"yolov8", "yolo11"}:
        raise ValueError("reCamera 系统导出仅支持 YOLOv8 / YOLO11 目标检测")
    size = request["kwargs"].get("imgsz", 640)
    detector = YOLO(str(request["source"]), task="detect", verbose=False)
    # Older official v8 checkpoints omit yaml_file. Compare the actual graph
    # recipe instead of trusting a filename or requiring newer metadata.
    canonical = yaml_model_load(algorithm + "n.yaml")
    actual = detector.model.yaml
    if any(actual.get(section) != canonical.get(section) for section in ("backbone", "head")):
        raise ValueError("模型内的实际架构与请求的系统算法不一致")
    names = detector.names
    classes = [names[index] for index in range(len(names))]
    contract = system_contract(algorithm, size, classes)
    model = raw_head_model(detector)
    sample = torch.zeros(1, 3, size, size)
    output_names = [f"{role}_s{stride}" for stride in (8, 16, 32) for role in ("box", "class")]
    output = Path(request["output"])
    temporary = output.with_name(f".{output.name}.tmp.onnx")
    try:
        with torch.inference_mode():
            tensors = model(sample)
            if [list(tensor.shape) for tensor in tensors] != [item["shape"] for item in contract["outputs"]]:
                raise ValueError("实际检测输出形状与系统 DFL 契约不一致")
            torch.onnx.export(model, sample, str(temporary), input_names=["images"],
                              output_names=output_names, opset_version=12, dynamo=False,
                              do_constant_folding=True)
        graph = onnx.load(str(temporary))
        onnx.checker.check_model(graph)
        metadata = {"cvsprout_system_model": json.dumps(contract, ensure_ascii=False),
                    "end2end": "False", "task": "detect"}
        onnx.helper.set_model_props(graph, metadata)
        onnx.save(graph, str(temporary))
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return {"format": "onnx", "task": "detect", "deployment_profile": "recamera_system",
            "system_model": contract}
