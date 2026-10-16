from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from functools import lru_cache
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageOps

from .config import settings
from .database import all_rows

RKNN_TARGETS = ["rk3588", "rk3576", "rk3566", "rk3568", "rk3562", "rv1103", "rv1106", "rv1126b"]
RKNN_ALGORITHMS = ["normal", "mmse", "kl_divergence"]
TASKS = ["detect", "segment", "classify", "pose"]
RKNN_WARNING = "输出遵循当前 Ultralytics 模型，不能直接套用 Rockchip model-zoo 专用分支版后处理；请按部署说明适配并在目标设备验证。"
_TERMINAL_ESCAPES = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-_])")
_TERMINAL_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def rknn_python() -> str:
    return str(getattr(settings, "rknn_python", "") or sys.executable)


@lru_cache(maxsize=16)
def _probe_toolkit(python: str, bucket: int) -> dict[str, Any]:
    del bucket
    code = "import json,importlib.metadata; from rknn.api import RKNN; print('MDP_TOOLKIT='+json.dumps({'version':importlib.metadata.version('rknn-toolkit2')}))"
    try:
        result = subprocess.run([python, "-c", code], capture_output=True, text=True, timeout=20, env={**os.environ, "PYTHONUNBUFFERED": "1"})
        line = next((line for line in reversed(result.stdout.splitlines()) if line.startswith("MDP_TOOLKIT=")), "")
        if result.returncode or not line:
            return {"available": False, "version": None, "reason": "RKNN Toolkit2 未就绪，请联系管理员配置转换环境。"}
        version = json.loads(line.split("=", 1)[1])["version"]
        digits = tuple(int(part) for part in version.split("+")[0].split(".")[:3])
        if digits < (2, 3, 2):
            return {"available": False, "version": version, "reason": "RKNN 转换需要 Toolkit2 2.3.2 或更新版本。"}
        return {"available": True, "version": version, "reason": ""}
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError):
        return {"available": False, "version": None, "reason": "RKNN Toolkit2 未就绪，请联系管理员配置转换环境。"}


def toolkit_status() -> dict[str, Any]:
    return _probe_toolkit(rknn_python(), int(time.monotonic() // 60))


@lru_cache(maxsize=8)
def _probe_tensorrt(python: str, bucket: int) -> dict[str, Any]:
    del bucket
    try:
        result = subprocess.run([python, "-c", "import tensorrt;print('MDP_TENSORRT='+tensorrt.__version__)"], capture_output=True, text=True, timeout=15)
        version = next((line.split("=", 1)[1] for line in result.stdout.splitlines() if line.startswith("MDP_TENSORRT=")), "")
        supported = bool(version) and int(version.split(".")[0]) >= 7 and not version.startswith("10.1.")
        if result.returncode == 0 and supported:
            return {"available": True, "version": version, "reason": ""}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return {"available": False, "version": None, "reason": "TensorRT 运行时未就绪，请联系管理员检查安装版本和 CUDA 依赖。"}


def format_catalog(enabled: bool, backend_installed: bool, cuda_available: bool) -> dict[str, Any]:
    base = enabled and backend_installed
    base_reason = "模型转换已被管理员禁用" if not enabled else "未安装模型导出工具，请联系管理员配置。" if not backend_installed else ""
    onnx_ready = all(find_spec(name) is not None for name in ("onnx", "onnxslim", "onnxruntime"))
    onnx_reason = "" if onnx_ready else "ONNX 导出依赖未就绪，请联系管理员安装 onnx、onnxslim 和 onnxruntime。"
    trt = _probe_tensorrt(sys.executable, int(time.monotonic() // 60)) if base and cuda_available and find_spec("tensorrt") is not None else {"available": False, "version": None, "reason": "未安装 TensorRT 运行时。"}
    trt_ready = trt["available"]
    toolkit = toolkit_status() if base and onnx_ready else {"available": False, "version": None, "reason": base_reason or onnx_reason}
    formats = {
        "torchscript": dict(label="TorchScript", description="适用于 PyTorch / LibTorch 部署。", available=base, reason=base_reason, requires_gpu=False, warnings=[]),
        "onnx": dict(label="ONNX", description="通用交换格式，可交给 ONNX Runtime 或其他推理引擎。", available=base and onnx_ready, reason=base_reason or onnx_reason, requires_gpu=False, warnings=[]),
        "engine": dict(label="TensorRT", description="在本机 NVIDIA GPU 上构建推理引擎。", available=base and onnx_ready and cuda_available and trt_ready, reason=base_reason or onnx_reason or ("本机没有可用的 CUDA GPU。" if not cuda_available else trt["reason"]), requires_gpu=True, runtime_version=trt["version"],
                       warnings=["TensorRT 引擎与构建时的 GPU、TensorRT / CUDA 版本有关，请在实际部署设备或兼容环境构建。", "下载文件含 Ultralytics 元数据头，加载方式和动态输入范围见部署说明。"]),
        "rknn": dict(label="RKNN", description="为指定 Rockchip NPU 芯片构建模型；无需在转换主机连接板卡。", available=base and onnx_ready and toolkit["available"], reason=base_reason or onnx_reason or toolkit["reason"], requires_gpu=False,
                     tasks=TASKS, target_platforms=RKNN_TARGETS, int8_only_platforms=["rv1103", "rv1106"], quantized_algorithms=RKNN_ALGORITHMS, optimization_levels=[0, 1, 2, 3], toolkit_version=toolkit["version"], warnings=["使用固定尺寸、单张 RGB 输入。非量化采用 RKNN 浮点构建；INT8 需训练集图片校准。RV1103 / RV1106 仅支持 INT8。", RKNN_WARNING]),
    }
    for value in formats.values():
        value.setdefault("tasks", TASKS)
    return formats


def calibration_assets(version_id: int) -> list[dict[str, Any]]:
    return all_rows("SELECT id,storage_name,sha256 FROM dataset_version_assets WHERE version_id=? AND split='train' ORDER BY id", (version_id,))


def preprocessing_contract(task: str, image_size: int, config: dict[str, Any]) -> dict[str, Any]:
    return {"input_color": "rgb", "image_size": [image_size, image_size], "resize": "shortest_edge_then_center_crop" if task == "classify" else "letterbox", "preserve_aspect_ratio": True, "interpolation": "pillow_bilinear" if task == "classify" else "opencv_inter_linear", "padding_value": None if task == "classify" else 114, "mean_values": config.get("mean_values", [0, 0, 0]), "std_values": config.get("std_values", [255, 255, 255]), "normalization": "(pixel - mean) / std", "normalization_location": "rknn_model"}


def prepare_calibration(version_id: int, task: str, image_size: int, count: int, attempt_dir: Path, check: Callable[[], None]) -> tuple[Path, dict[str, Any]]:
    assets = calibration_assets(version_id)
    if len(assets) < count:
        raise RuntimeError(f"冻结版本的训练集只有 {len(assets)} 张图片，少于请求的 {count} 张校准图片。")
    # Evenly spaced, repeatable sample rather than only the first upload batch.
    selected = [assets[index * len(assets) // count] for index in range(count)]
    source_root = (settings.versions_dir / f"version-{version_id}").resolve()
    destination = attempt_dir / "calibration"
    destination.mkdir()
    paths = []
    hashes = []
    for index, row in enumerate(selected):
        check()
        source = (source_root / row["storage_name"]).resolve()
        if source_root not in source.parents or not source.is_file():
            raise RuntimeError("冻结版本的校准图片缺失，请恢复版本文件后重试。")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if digest != row["sha256"]:
            raise RuntimeError("冻结版本的校准图片校验失败，已停止转换。")
        with Image.open(source) as original:
            image = ImageOps.exif_transpose(original).convert("RGB")
            width, height = image.size
            if task == "classify":
                ratio = image_size / min(width, height)
                image = image.resize((max(image_size, int(width * ratio)), max(image_size, int(height * ratio))), Image.Resampling.BILINEAR)
                left = round((image.width - image_size) / 2); top = round((image.height - image_size) / 2)
                image = image.crop((left, top, left + image_size, top + image_size))
            else:
                import cv2
                import numpy as np
                ratio = min(image_size / width, image_size / height)
                resized = Image.fromarray(cv2.resize(np.asarray(image), (max(1, round(width * ratio)), max(1, round(height * ratio))), interpolation=cv2.INTER_LINEAR))
                image = Image.new("RGB", (image_size, image_size), (114, 114, 114))
                image.paste(resized, (round((image_size - resized.width) / 2 - 0.1), round((image_size - resized.height) / 2 - 0.1)))
            path = destination / f"sample-{index:04d}.png"
            image.save(path, format="PNG")
            paths.append(str(path.resolve())); hashes.append(digest)
    dataset = attempt_dir / "calibration.txt"
    dataset.write_text("\n".join(paths) + "\n", encoding="utf-8")
    return dataset, {"source": "version_train", "version_id": version_id, "count": len(selected), "version_asset_ids": [row["id"] for row in selected], "source_sha256": hashes, "selection": "deterministic_evenly_spaced_by_snapshot_id"}


class ConversionStopped(RuntimeError):
    def __init__(self, lost_claim: bool = False):
        super().__init__("转换执行权已移交" if lost_claim else "转换已取消")
        self.lost_claim = lost_claim


def run_converter_process(python: str, request: dict[str, Any], attempt_dir: Path, stage: str, check: Callable[[], None], log: Callable[[str], None]) -> dict[str, Any]:
    check()
    request_path = attempt_dir / f"{stage}-request.json"
    result_path = attempt_dir / f"{stage}-result.json"
    request = {**request, "result_path": str(result_path.resolve())}
    request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    env = {**os.environ, "YOLO_AUTOINSTALL": "false", "PYTHONUNBUFFERED": "1", "MPLBACKEND": "Agg"}
    process = subprocess.Popen([python, str(Path(__file__).with_name("conversion_runner.py")), "--request", str(request_path.resolve())], cwd=attempt_dir, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True)
    messages: queue.Queue[str] = queue.Queue()
    def signal_group(sig: int) -> None:
        # The SDK may leave subprocesses behind after its own process has exited.
        # The dedicated session keeps their process group safe to clean up.
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
    def consume():
        assert process.stdout is not None
        for line in process.stdout:
            # Text mode converts CR progress updates to lines; strip terminal
            # styling/control characters while retaining normal Unicode text.
            line = _TERMINAL_CONTROLS.sub("", _TERMINAL_ESCAPES.sub("", line))
            messages.put(line.rstrip()[:4000])
    reader = threading.Thread(target=consume, daemon=True); reader.start()
    tail = []
    try:
        while process.poll() is None or reader.is_alive() or not messages.empty():
            check()
            if process.poll() is not None:
                # A descendant retaining stdout must not keep a finished stage
                # alive indefinitely, even when no cancellation was requested.
                signal_group(signal.SIGKILL)
            try:
                line = messages.get(timeout=0.2)
            except queue.Empty:
                continue
            if line:
                tail = (tail + [line])[-8:]
                log(line)
        check()
        if process.returncode:
            raise RuntimeError("转换工具执行失败：" + ("\n".join(tail)[-1600:] or f"退出码 {process.returncode}"))
        if not result_path.is_file():
            raise RuntimeError("转换工具未返回结果说明。")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        artifact = Path(result["artifact"]).resolve()
        if attempt_dir.resolve() not in artifact.parents or not artifact.is_file() or artifact.stat().st_size == 0:
            raise RuntimeError("转换工具未生成有效的模型文件。")
        return result
    finally:
        signal_group(signal.SIGTERM)
        if process.poll() is None:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                signal_group(signal.SIGKILL); process.wait(timeout=5)
        signal_group(signal.SIGKILL)
        reader.join(timeout=2)
        if process.stdout:
            process.stdout.close()
