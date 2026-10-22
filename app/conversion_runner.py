"""Isolated model exporter; invoked by a worker with a private request JSON.

This module deliberately does not import the web application or its database.
The worker owns process cancellation, leases, publication and artifact hashes.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import os
from pathlib import Path
import shutil
import sys
import uuid
from typing import Any
import zipfile


RKNN_TARGETS = {
    "rk3588", "rk3576", "rk3566", "rk3568", "rk3562", "rk2118",
    "rv1126b", "rv1103", "rv1106", "rv1103b", "rv1106b",
}
INT8_ONLY_TARGETS = {"rv1103", "rv1106", "rv1103b", "rv1106b"}
TASKS = {"detect", "segment", "pose", "classify"}
EXPORT_KEYS = {
    "format", "imgsz", "batch", "half", "dynamic", "simplify", "device",
    "opset", "nms", "end2end", "workspace", "int8",
}
RKNN_KEYS = {
    "target_platform", "quantized", "quantized_algorithm", "optimization_level",
    "mean_values", "std_values", "input_color", "calibration_count", "calibration_source",
}


def _versions(names: tuple[str, ...]) -> dict[str, str]:
    result = {"python": sys.version.split()[0]}
    for name in names:
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return result


def _path(value: Any, root: Path, *, inside: bool = False) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("转换文件路径缺失")
    original = Path(value)
    result = (original if original.is_absolute() else root / original).resolve()
    if inside and (not result.is_relative_to(root) or result == root):
        raise ValueError("转换输出必须位于当前任务目录中")
    return result


def _validate_request(request: Any, root: Path, request_path: Path) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ValueError("转换请求必须是 JSON 对象")
    if request.get("operation") not in {"ultralytics", "rknn"}:
        raise ValueError("不支持的转换操作")
    if request.get("task") not in TASKS:
        raise ValueError("不支持的模型任务类型")
    result = dict(request)
    result["source"] = _path(request.get("source"), root, inside=True)
    result["output"] = _path(request.get("output"), root, inside=True)
    result["result_path"] = _path(request.get("result_path"), root, inside=True)
    if not result["source"].is_file() or result["source"].stat().st_size == 0:
        raise ValueError("转换源模型不存在或为空")
    if len({result["source"], result["output"], result["result_path"], request_path}) != 4:
        raise ValueError("源模型、请求、结果与产物路径不能相同")
    if result["output"].exists() or result["result_path"].exists():
        raise ValueError("转换输出已存在，请使用新的尝试目录")
    if not isinstance(request.get("preprocessing", {}), dict):
        raise ValueError("预处理说明必须是 JSON 对象")
    if request["operation"] == "rknn":
        result["rknn"] = _rknn_config(request.get("rknn"))
        if result["source"].suffix.lower() != ".onnx":
            raise ValueError("RKNN 转换需要 ONNX 中间模型")
        if result["rknn"]["quantized"]:
            dataset = _path(request.get("dataset_path"), root, inside=True)
            if not dataset.is_file():
                raise ValueError("INT8 转换缺少校准图片清单")
            lines = [line.strip() for line in dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
            if not lines:
                raise ValueError("INT8 转换的校准图片清单为空")
            configured_count = result["rknn"].get("calibration_count")
            if configured_count is not None and configured_count != len(lines):
                raise ValueError("实际校准图片数量与任务配置不一致")
            for line in lines:
                sample = _path(line, dataset.parent, inside=False)
                if not sample.is_file() or not sample.is_relative_to(root):
                    raise ValueError("校准图片必须存在于当前任务目录中")
                if any(character.isspace() for character in str(sample)):
                    raise ValueError("校准图片路径不能包含空白字符，请使用任务内的安全文件名")
            result["dataset_path"] = dataset
            result["calibration_count"] = len(lines)
        else:
            result["dataset_path"] = None
            result["calibration_count"] = 0
    else:
        result["kwargs"] = _export_config(request.get("kwargs"))
    return result


def _rknn_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - RKNN_KEYS:
        raise ValueError("RKNN 配置包含不支持的字段")
    config = {
        "target_platform": "rk3588", "quantized": False,
        "quantized_algorithm": "normal", "optimization_level": 3,
        "mean_values": [0, 0, 0], "std_values": [255, 255, 255], "input_color": "rgb",
        **value,
    }
    if config["target_platform"] not in RKNN_TARGETS:
        raise ValueError("不支持的 RKNN 目标芯片")
    if type(config["quantized"]) is not bool:
        raise ValueError("quantized 必须为布尔值")
    if config["target_platform"] in INT8_ONLY_TARGETS and not config["quantized"]:
        raise ValueError("所选芯片仅支持 INT8 转换")
    if config["quantized_algorithm"] not in {"normal", "mmse", "kl_divergence"}:
        raise ValueError("不支持的 RKNN 量化算法")
    if type(config["optimization_level"]) is not int or not 0 <= config["optimization_level"] <= 3:
        raise ValueError("RKNN 优化等级必须为 0–3 的整数")
    if config["input_color"] != "rgb":
        raise ValueError("当前 YOLO 模型要求 RGB 输入")
    if config.get("calibration_source", "version_train") != "version_train":
        raise ValueError("校准图片必须来自训练使用的数据版本训练集")
    if "calibration_count" in config and (type(config["calibration_count"]) is not int or not 1 <= config["calibration_count"] <= 1000):
        raise ValueError("校准图片数量必须为 1–1000 的整数")
    for name in ("mean_values", "std_values"):
        numbers = config[name]
        if not isinstance(numbers, (list, tuple)) or len(numbers) != 3:
            raise ValueError("均值与标准差必须分别包含三个数值")
        if any(type(number) not in (int, float) or not math.isfinite(number) or abs(number) > 1_000_000 for number in numbers):
            raise ValueError("均值与标准差必须为有限数值，绝对值不超过 1000000")
        if name == "std_values" and any(number <= 0 for number in numbers):
            raise ValueError("标准差必须大于零")
        config[name] = list(numbers)
    return config


def _export_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - EXPORT_KEYS:
        raise ValueError("模型导出配置包含不支持的字段")
    if value.get("format") not in {"onnx", "engine", "torchscript"}:
        raise ValueError("隔离导出器仅支持 ONNX、TensorRT 和 TorchScript")
    if value.get("int8", False):
        raise ValueError("当前 TensorRT 导出未配置 INT8 校准流程")
    for key in ("half", "dynamic", "simplify", "nms", "end2end", "int8"):
        if key in value and type(value[key]) is not bool:
            raise ValueError(f"{key} 必须为布尔值")
    for key, lower, upper in (("batch", 1, 64), ("imgsz", 32, 4096), ("opset", 9, 22)):
        if key in value and (type(value[key]) is not int or not lower <= value[key] <= upper):
            raise ValueError(f"{key} 超出支持范围")
    if "workspace" in value and value["workspace"] is not None:
        number = value["workspace"]
        if type(number) not in (int, float) or not math.isfinite(number) or not 0 < number <= 64:
            raise ValueError("TensorRT workspace 必须大于零且不超过 64 GiB")
    device = value.get("device", "cpu")
    if not isinstance(device, (str, int)) or isinstance(device, bool):
        raise ValueError("转换设备配置无效")
    device_text = str(device)
    if device_text != "cpu" and not device_text.isdigit() and not (device_text.startswith("cuda:") and device_text[5:].isdigit()):
        raise ValueError("转换设备必须为 CPU 或明确的 CUDA 设备编号")
    if value["format"] == "engine" and device_text == "cpu":
        raise ValueError("TensorRT 转换需要 CUDA 设备")
    return dict(value)


def _onnx_metadata(path: Path) -> dict[str, Any]:
    import onnx

    model = onnx.load(str(path), load_external_data=False)
    onnx.checker.check_model(str(path))
    initializer_names = {item.name for item in model.graph.initializer}

    def describe(value: Any) -> dict[str, Any]:
        tensor = value.type.tensor_type
        shape = [dim.dim_value if dim.HasField("dim_value") else dim.dim_param or None for dim in tensor.shape.dim]
        return {"name": value.name, "shape": shape, "dtype": onnx.TensorProto.DataType.Name(tensor.elem_type)}

    return {
        "inputs": [describe(value) for value in model.graph.input if value.name not in initializer_names],
        "outputs": [describe(value) for value in model.graph.output],
        "opsets": {item.domain or "ai.onnx": item.version for item in model.opset_import},
        "ir_version": model.ir_version,
        "model_metadata": {item.key: item.value for item in model.metadata_props},
        "contains_non_max_suppression": any(node.op_type == "NonMaxSuppression" for node in model.graph.node),
    }


def _preprocessing(request: dict[str, Any], *, rknn: bool, model_backend: str = "yolo") -> dict[str, Any]:
    config = request.get("rknn", {}) if rknn else {}
    mean = config.get("mean_values", [0, 0, 0])
    std = config.get("std_values", [255, 255, 255])
    image_size = request.get("kwargs", {}).get("imgsz")
    result = {
        "image_size": [image_size, image_size] if image_size is not None else None,
        "preserve_aspect_ratio": True,
        "interpolation": "bilinear",
        "padding_value": None if request["task"] == "classify" else 114,
        "auto_padding": False,
        **request.get("preprocessing", {}),
        "color": "RGB", "source_pixel_range": [0, 255],
        "geometry": "resize_shorter_side_then_center_crop" if request["task"] == "classify" else "letterbox",
        "mean_values": mean, "std_values": std,
        "normalization": "(pixel - mean) / std",
        "normalization_location": "rknn_input_preprocessing" if rknn else "application_before_model_input",
        "normalization_matches_training": mean == [0, 0, 0] and std == [255, 255, 255],
        "quant_img_RGB2BGR": False,
        "channel_order_note": "校准和部署均使用 RGB；quant_img_RGB2BGR 仅影响校准图片读取，不会改变模型的部署通道顺序。",
    }
    if model_backend == "rtdetr":
        result.update(geometry="resize_stretch", preserve_aspect_ratio=False,
                      padding_value=None, auto_padding=False)
    return result


def _postprocessing(task: str, *, end2end: bool = False, nms_in_graph: bool = False) -> dict[str, Any]:
    selected = end2end or nms_in_graph
    notes = {
        "detect": "模型已输出筛选后的框、分数和类别索引；无需重复 NMS，仍需按应用阈值过滤和坐标还原。" if selected else "模型输出密集的已解码框与类别分数；部署端执行置信度筛选、NMS 和坐标还原。",
        "segment": "模型已筛选检测结果，无需重复 NMS；仍需配套 mask prototype 解码、裁剪及尺寸还原。" if selected else "保留密集检测输出和 mask prototype 输出；需框筛选/NMS、mask 解码、裁剪及尺寸还原。",
        "pose": "模型已筛选检测结果，无需重复 NMS；仍需按训练时关键点顺序解析并还原坐标。" if selected else "保留密集框、类别分数和关键点输出；需按训练时关键点顺序解析、NMS 及坐标还原。",
        "classify": "保留源模型分类输出；Ultralytics 分类头已含 Softmax，不要重复施加 Softmax。",
    }
    return {
        "family": "ultralytics_original_graph", "task": task, "note": notes[task],
        "end2end": end2end, "nms_in_graph": nms_in_graph,
        "external_nms_required": task != "classify" and not selected,
        "selection": "classification" if task == "classify" else "end2end_topk" if end2end else "embedded_nms" if nms_in_graph else "application_nms",
        "box_format": None if task == "classify" else "xyxy" if selected else "xywh",
        "rockchip_model_zoo_postprocess_compatible": False,
        "rockchip_note": "未改写为 Rockchip Model Zoo 的多分支优化输出，不能直接套用其 YOLO 示例后处理。",
        "quantized_output_note": "部署时查询实际输出 dtype、zero_point 和 scale；读取原生量化输出时先反量化，不能把 INT8 字节直接当分数。",
    }


def _rtdetr_head(model: Any) -> bool:
    layers = getattr(getattr(model, "model", None), "model", None)
    return (layers is not None and len(layers) > 0
            and any(base.__name__ == "RTDETRDecoder" for base in type(layers[-1]).__mro__))


def _rtdetr_postprocessing() -> dict[str, Any]:
    return {
        "family": "ultralytics_rtdetr", "task": "detect",
        "end2end": False, "nms_in_graph": False, "external_nms_required": False,
        "selection": "per_query_class_argmax_then_confidence",
        "output_layout": "B,N,4+nc", "box_format": "xywh", "box_coordinates": "normalized",
        "class_scores": "sigmoid_probabilities", "score_activation_required": False,
        "coordinate_restoration": "将归一化中心框 xywh 转为 xyxy，再将 x 坐标乘原图宽度、y 坐标乘原图高度；无需去除 letterbox padding。",
        "note": "每个 query 的前四项为归一化 cx、cy、w、h，后续项为各类别分数。每行取分数最高的类别并按置信度过滤，可按分数排序截取 max_det；无需额外 sigmoid、Softmax 或 NMS。",
        "rockchip_model_zoo_postprocess_compatible": False,
        "contract_source": "model_head",
    }


def _export_boolean(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return default


def _torchscript_metadata(path: Path) -> dict[str, Any]:
    # Read the exporter's extra JSON without loading or executing the graph.
    try:
        with zipfile.ZipFile(path) as archive:
            config = next((item for item in archive.infolist() if item.filename.endswith("/extra/config.txt")), None)
            if config is not None and config.file_size <= 1024 * 1024:
                value = json.loads(archive.read(config).decode("utf-8"))
                return value if isinstance(value, dict) else {}
    except (zipfile.BadZipFile, ValueError, UnicodeDecodeError):
        pass
    return {}


def _engine_metadata(path: Path) -> dict[str, Any]:
    metadata: dict[str, Any] = {"container": "tensorrt_plan", "payload_offset_bytes": 0}
    with path.open("rb") as handle:
        length = int.from_bytes(handle.read(4), "little")
        if 0 < length <= 1024 * 1024 and length + 4 < path.stat().st_size:
            try:
                header = json.loads(handle.read(length).decode("utf-8"))
                if isinstance(header, dict):
                    metadata.update(container="ultralytics_metadata_prefix", payload_offset_bytes=4 + length,
                                    exporter_metadata=header)
            except (ValueError, UnicodeDecodeError):
                pass
    metadata["loader_note"] = "使用 Ultralytics 加载，或先跳过 payload_offset_bytes 字节再交给 TensorRT deserialize_cuda_engine。"
    import tensorrt as trt

    with path.open("rb") as handle:
        handle.seek(metadata["payload_offset_bytes"])
        serialized = handle.read()
    runtime = None
    engine = None
    try:
        runtime = trt.Runtime(trt.Logger(trt.Logger.WARNING))
        engine = runtime.deserialize_cuda_engine(serialized)
    except Exception as exc:
        raise RuntimeError(f"TensorRT 无法重新读取导出引擎，产物未发布：{exc}") from exc
    if engine is None:
        raise RuntimeError("TensorRT 无法重新读取导出引擎，产物未发布")
    try:
        inputs, outputs, profiles = [], [], []
        if hasattr(engine, "num_io_tensors"):
            for index in range(engine.num_io_tensors):
                name = engine.get_tensor_name(index)
                is_input = engine.get_tensor_mode(name) == trt.TensorIOMode.INPUT
                tensor = {"name": name, "shape": list(engine.get_tensor_shape(name)), "dtype": str(engine.get_tensor_dtype(name))}
                (inputs if is_input else outputs).append(tensor)
                if is_input and any(size < 0 for size in tensor["shape"]):
                    for profile in range(engine.num_optimization_profiles):
                        minimum, optimum, maximum = engine.get_tensor_profile_shape(name, profile)
                        profiles.append({"input": name, "profile": profile, "min": list(minimum), "opt": list(optimum), "max": list(maximum)})
        else:
            for index in range(engine.num_bindings):
                tensor = {"name": engine.get_binding_name(index), "shape": list(engine.get_binding_shape(index)), "dtype": str(engine.get_binding_dtype(index))}
                (inputs if engine.binding_is_input(index) else outputs).append(tensor)
        metadata.update(inputs=inputs, outputs=outputs, optimization_profiles=profiles)
    except Exception as exc:
        # A valid engine may come from an older runtime with a different tensor
        # query API. This is distinct from a failed engine deserialization.
        metadata["inspection_error"] = str(exc)[:1000]
    finally:
        del engine, runtime, serialized
    return metadata


def _publish_copy(source: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def export_ultralytics(request: dict[str, Any]) -> dict[str, Any]:
    # Set before importing Ultralytics: check_requirements reads this at import.
    os.environ["YOLO_AUTOINSTALL"] = "false"
    from ultralytics import YOLO

    kwargs = request["kwargs"]
    model = YOLO(str(request["source"]), task=request["task"], verbose=False)
    is_rtdetr = _rtdetr_head(model)
    if is_rtdetr:
        if request["task"] != "detect":
            raise ValueError("RT-DETR 仅支持目标检测转换")
        if kwargs.get("dynamic", False):
            raise ValueError("RT-DETR 当前仅支持固定输入尺寸导出")
        if kwargs.get("nms", False):
            raise ValueError("RT-DETR 输出无需 NMS，不能开启导出 NMS")
        if kwargs.get("imgsz", 640) < 128:
            raise ValueError("RT-DETR 导出图像尺寸不能小于 128")
    exported = Path(model.export(**kwargs)).resolve()
    if not exported.is_file() or exported.stat().st_size == 0:
        raise RuntimeError("转换器未生成非空模型文件")
    metadata = {
        "format": kwargs["format"], "task": request["task"], "export_options": kwargs,
        "model_backend": "rtdetr" if is_rtdetr else "yolo",
        "preprocessing": _preprocessing(request, rknn=False, model_backend="rtdetr" if is_rtdetr else "yolo"),
        "postprocessing": _postprocessing(request["task"]),
        "tool_versions": _versions(("ultralytics", "torch", "onnx", "onnxslim", "tensorrt",
                                    "tensorrt-cu12", "tensorrt-cu12-bindings", "tensorrt-cu12-libs")),
    }
    if kwargs["format"] == "onnx":
        metadata.update(_onnx_metadata(exported))
    elif kwargs["format"] == "engine":
        metadata["engine"] = _engine_metadata(exported)
        import tensorrt as trt

        # NVIDIA's CUDA-specific wheels may not install a distribution named
        # "tensorrt". Record the SDK actually imported to build/read the engine.
        metadata["tool_versions"]["tensorrt"] = str(trt.__version__)
        metadata["compatibility"] = {
            "device_specific": True,
            "note": "引擎按当前 GPU 和 TensorRT 环境构建；部署到其他 GPU、系统或 TensorRT 版本时通常需要重新转换。",
        }
        try:
            import torch

            device = str(kwargs["device"])
            index = int(device.split(":")[-1])
            metadata["compatibility"].update(gpu_name=torch.cuda.get_device_name(index),
                                             compute_capability=list(torch.cuda.get_device_capability(index)),
                                             cuda_version=torch.version.cuda)
        except Exception as exc:
            metadata["compatibility"]["device_inspection_error"] = str(exc)[:500]
    elif kwargs["format"] == "torchscript":
        metadata["model_metadata"] = _torchscript_metadata(exported)
    exporter_metadata = metadata.get("model_metadata", metadata.get("engine", {}).get("exporter_metadata", {}))
    head_end2end = bool(getattr(model.model, "end2end", False))
    requested_end2end = kwargs.get("end2end", head_end2end)
    end2end = _export_boolean(exporter_metadata.get("end2end"), requested_end2end)
    export_args = exporter_metadata.get("args", {})
    requested_nms = export_args.get("nms", kwargs.get("nms", False)) if isinstance(export_args, dict) else kwargs.get("nms", False)
    nms_in_graph = bool(metadata.get("contains_non_max_suppression", requested_nms))
    if end2end:
        nms_in_graph = False
    metadata["postprocessing"] = _postprocessing(request["task"], end2end=end2end, nms_in_graph=nms_in_graph)
    metadata["postprocessing"]["contract_source"] = "exporter_metadata" if "end2end" in exporter_metadata else "model_head_and_export_options"
    if is_rtdetr:
        if metadata.get("contains_non_max_suppression", False):
            raise ValueError("RT-DETR 导出图意外包含 NMS，产物未发布")
        metadata["postprocessing"] = _rtdetr_postprocessing()
    exported_size = exporter_metadata.get("imgsz")
    if isinstance(exported_size, list) and len(exported_size) == 2:
        metadata["preprocessing"]["image_size"] = exported_size
    elif metadata.get("inputs") and len(metadata["inputs"][0]["shape"]) == 4:
        shape = metadata["inputs"][0]["shape"]
        if all(type(size) is int for size in shape[2:]):
            metadata["preprocessing"]["image_size"] = shape[2:]
    _publish_copy(exported, request["output"])
    return metadata


def export_rknn(request: dict[str, Any]) -> dict[str, Any]:
    from rknn.api import RKNN

    config = request["rknn"]
    source_metadata = _onnx_metadata(request["source"])
    inputs = source_metadata["inputs"]
    if len(inputs) != 1 or len(inputs[0]["shape"]) != 4 or inputs[0]["shape"][:2] != [1, 3]:
        raise ValueError("RKNN 中间模型必须是 batch=1、RGB 三通道的 NCHW 输入")
    if inputs[0]["dtype"] != "FLOAT" or any(type(size) is not int or size <= 0 for size in inputs[0]["shape"]):
        raise ValueError("RKNN 中间模型需要固定输入尺寸和 float32 类型")
    if source_metadata["contains_non_max_suppression"]:
        raise ValueError("RKNN 中间模型不可包含 NMS，请在部署端进行后处理")
    if source_metadata["opsets"].get("ai.onnx", 0) > 19:
        raise ValueError("RKNN 中间模型的 ONNX opset 不能超过 19")
    output = request["output"]
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}.{uuid.uuid4().hex}.tmp.rknn")
    converter = None

    def require_success(stage: str, result: Any) -> None:
        if type(result) is not int or result != 0:
            raise RuntimeError(f"RKNN {stage} 失败，返回码：{result}")
        print(f"RKNN {stage} completed", flush=True)

    try:
        converter = RKNN(verbose=False)
        options = {
            "target_platform": config["target_platform"],
            "mean_values": [config["mean_values"]], "std_values": [config["std_values"]],
            "quant_img_RGB2BGR": False, "optimization_level": config["optimization_level"],
            "float_dtype": "float16",
        }
        if config["quantized"]:
            options.update(quantized_dtype="w8a8", quantized_method="channel",
                           quantized_algorithm=config["quantized_algorithm"])
        require_success("config", converter.config(**options))
        require_success("load_onnx", converter.load_onnx(model=str(request["source"])))
        build = {"do_quantization": config["quantized"]}
        if config["quantized"]:
            build["dataset"] = str(request["dataset_path"])
        require_success("build", converter.build(**build))
        require_success("export_rknn", converter.export_rknn(str(temporary)))
        if not temporary.is_file() or temporary.stat().st_size <= 4:
            raise RuntimeError("RKNN 导出未生成有效产物")
        with temporary.open("rb") as handle:
            if handle.read(4) != b"RKNN":
                raise RuntimeError("RKNN 产物文件头无效")
        os.replace(temporary, output)
    finally:
        if converter is not None:
            converter.release()
        temporary.unlink(missing_ok=True)
    preprocessing = _preprocessing(request, rknn=True)
    preprocessing["image_size"] = inputs[0]["shape"][2:]
    warnings = []
    if not preprocessing["normalization_matches_training"]:
        warnings.append("自定义均值或标准差偏离当前 YOLO 训练的 0–1 归一化；部署前请使用真实样本验证精度。")
    return {
        "format": "rknn", "task": request["task"], "target_platform": config["target_platform"],
        "precision": "int8" if config["quantized"] else "float16",
        "rknn_config": config, "calibration_count": request["calibration_count"],
        "source_onnx": source_metadata,
        "preprocessing": preprocessing,
        "postprocessing": {**_postprocessing(request["task"], end2end=_export_boolean(source_metadata["model_metadata"].get("end2end"), False)),
                           "contract_source": "source_onnx_metadata"},
        "tool_versions": _versions(("rknn-toolkit2", "onnx", "numpy")),
        "compatibility": {
            "target_platform": config["target_platform"], "board_validated": False,
            "runtime_tensor_attributes": "query_on_target",
            "note": "RKNN 编译成功；尚未在目标板验证。部署时查询实际输入输出布局、类型、量化参数，并使用兼容的 RKNN Runtime。",
        },
        "warnings": warnings,
    }


def _write_result(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    result_path = None
    try:
        request_path = Path(args.request).resolve(strict=True)
        if request_path.stat().st_size > 1024 * 1024:
            raise ValueError("转换请求文件过大")
        root = request_path.parent
        request = _validate_request(json.loads(request_path.read_text(encoding="utf-8")), root, request_path)
        result_path = request["result_path"]
        # SDK caches, generated models and logs stay within this private attempt.
        os.chdir(root)
        metadata = export_rknn(request) if request["operation"] == "rknn" else export_ultralytics(request)
        _write_result(result_path, {"artifact": str(request["output"]), "metadata": metadata})
        print("Conversion completed", flush=True)
        return 0
    except Exception as exc:
        message = str(exc)[:4000]
        print(f"Conversion failed ({type(exc).__name__}): {message}", file=sys.stderr, flush=True)
        if result_path is not None:
            _write_result(result_path, {"error": {"type": type(exc).__name__, "message": message}})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
