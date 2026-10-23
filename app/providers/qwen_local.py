"""Offline Qwen3.5 vision-language annotation using the shared local GPU lease."""
from __future__ import annotations

import asyncio
from functools import lru_cache
import json
import math
from pathlib import Path
from typing import Any

from PIL import Image

from . import local
from .base import AnnotationProvider, ModelInfo, Prediction, normalize_predictions, parse_json_response
from .execution import serialized_local_inference
from .qwen import annotation_instruction
from ..task_types import validate_task_annotation
from ..worker_client import on_worker

MAX_INPUT_TOKENS = 8192
MAX_OUTPUT_TOKENS = 4096
MAX_IMAGE_PIXELS = 1024 * 1024
MAX_SOURCE_PIXELS = 50_000_000
MAX_INSTRUCTION_CHARS = 32_000
_MODEL_TYPES = {"qwen3_5", "qwen3_5_moe"}


class QwenLocalError(RuntimeError):
    """An actionable local inference failure safe to show to the operator."""


def _json_file(path: Path) -> dict[str, Any]:
    if not path.is_file() or not 0 < path.stat().st_size <= 16 * 1024 * 1024:
        raise ValueError(f"模型配置缺失或大小异常：{path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"模型配置不是 JSON 对象：{path.name}")
    return value


def _weight_files(path: Path) -> list[Path]:
    """Check the complete index and safetensors headers without reading tensors."""
    from safetensors import safe_open

    index_path = path / "model.safetensors.index.json"
    mapping = _json_file(index_path).get("weight_map") if index_path.exists() else None
    if index_path.exists() and (not isinstance(mapping, dict) or not mapping
                                or not all(isinstance(key, str) and isinstance(value, str) for key, value in mapping.items())):
        raise ValueError("模型分片索引缺少有效的 weight_map")
    names = set(mapping.values()) if mapping else {"model.safetensors"}
    files = []
    for name in sorted(names):
        if Path(name).name != name or not name.endswith(".safetensors"):
            raise ValueError("模型分片索引包含无效文件路径")
        shard = path / name
        if not shard.resolve().is_relative_to(path) or not shard.is_file() or shard.stat().st_size < 10:
            raise ValueError(f"模型权重缺失、不完整或路径越界：{name}")
        with shard.open("rb") as handle:
            header_size = int.from_bytes(handle.read(8), "little")
        if not 2 <= header_size <= 16 * 1024 * 1024:
            raise ValueError(f"模型权重文件头异常：{name}")
        # safe_open validates tensor offsets against the file length. Listing
        # keys does not allocate tensors or transfer any weights to the GPU.
        with safe_open(str(shard), framework="numpy") as handle:
            keys = set(handle.keys())
        if not keys or (mapping is not None and keys != {key for key, value in mapping.items() if value == name}):
            raise ValueError(f"模型分片内容与索引不一致：{name}")
        files.append(shard)
    return files


def _runtime_support(model_type: str) -> None:
    import psutil  # noqa: F401
    import torch  # noqa: F401
    import torchvision  # noqa: F401
    import transformers
    from transformers import AutoModelForImageTextToText, AutoProcessor  # noqa: F401
    from transformers.models.auto.modeling_auto import MODEL_FOR_IMAGE_TEXT_TO_TEXT_MAPPING_NAMES

    name = MODEL_FOR_IMAGE_TEXT_TO_TEXT_MAPPING_NAMES.get(model_type)
    if not name:
        raise RuntimeError(f"Transformers {transformers.__version__} 不支持 Qwen3.5；请安装项目固定的 worker 推理依赖并重启服务")
    getattr(transformers, name)  # Resolve the implementation, without constructing a model.


@lru_cache(maxsize=32)
def _weight_elements(identity: tuple[tuple[str, int, int, int, int], ...]) -> int:
    """Count header shapes only, caching by path/inode/size/mtime/ctime."""
    total = 0
    for filename, *_ in identity:
        with Path(filename).open("rb") as handle:
            header_size = int.from_bytes(handle.read(8), "little")
            if not 2 <= header_size <= 16 * 1024 * 1024:
                raise QwenLocalError("Qwen3.5 权重文件头异常，请重新准备模型")
            header = json.loads(handle.read(header_size))
        for name, value in header.items():
            if name == "__metadata__":
                continue
            shape = value.get("shape")
            if not isinstance(shape, list) or any(type(dim) is not int or dim < 0 for dim in shape):
                raise QwenLocalError("Qwen3.5 权重张量形状异常，请重新准备模型")
            total += math.prod(shape)
    return total


def _check_weight_memory(model_path: Path, device: str, dtype: Any) -> None:
    """A minimum weight-only check; activations/cache still need extra memory."""
    import psutil
    import torch

    files = _weight_files(model_path)
    identity = []
    for filename in files:
        stat = filename.stat()
        identity.append((str(filename), stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    required = _weight_elements(tuple(identity)) * dtype.itemsize
    # from_pretrained initially materializes the model on CPU, then moves it
    # to the chosen GPU, so both host RAM and device VRAM must fit the weights.
    capacities = [("系统内存", int(psutil.virtual_memory().available))]
    if device.startswith("cuda"):
        capacities.append((f"{device} 显存", int(torch.cuda.mem_get_info(device)[0])))
    for label, available in capacities:
        if available < required:
            gib = 1024 ** 3
            raise QwenLocalError(
                f"Qwen3.5 {label}不足：当前可用 {available / gib:.2f} GiB，"
                f"仅完整权重至少需要 {required / gib:.2f} GiB（{str(dtype).removeprefix('torch.')}）；"
                "推理还需额外内存，请改用较小模型或释放内存后重试"
            )


@on_worker("qwen-local-status")
def local_qwen_runtime_status(model_path: str) -> tuple[bool, str]:
    try:
        path = Path(model_path).expanduser().resolve()
        if not path.is_dir():
            return False, f"Qwen3.5 本地模型目录不存在：{path}"
        config = _json_file(path / "config.json")
        model_type = config.get("model_type")
        if model_type not in _MODEL_TYPES:
            return False, "本地 Qwen 标注仅支持 Qwen3.5 视觉模型（qwen3_5 / qwen3_5_moe）"
        tokenizer_config = _json_file(path / "tokenizer_config.json")
        _json_file(path / "preprocessor_config.json")
        if not (path / "tokenizer.json").is_file() or (path / "tokenizer.json").stat().st_size == 0:
            return False, "Qwen3.5 分词器文件缺失：tokenizer.json"
        if not tokenizer_config.get("chat_template") and not (
            (path / "chat_template.jinja").is_file() and (path / "chat_template.jinja").stat().st_size > 0
        ):
            return False, "Qwen3.5 对话模板缺失：chat_template.jinja"
        _weight_files(path)
        _runtime_support(model_type)
    except ImportError as exc:
        return False, f"缺少 Qwen3.5 本地推理依赖：{exc.name or str(exc)}"
    except Exception as exc:
        return False, f"Qwen3.5 本地模型不可用：{str(exc)[:350]}"
    return True, ""


def _load_qwen(model_path: str, requested_device: str) -> tuple[Any, Any, str]:
    import torch
    from transformers import AutoModelForImageTextToText, AutoProcessor

    resolved = str(Path(model_path).expanduser().resolve())
    device = local._model_device("qwen3.5", resolved, requested_device)
    key = ("qwen3.5", resolved, device)
    with local._CACHE_LOCK:
        cached = local._MODEL_CACHE.get(key)
        if cached is None:
            dtype = torch.float32
            if device.startswith("cuda"):
                with torch.cuda.device(device):
                    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
            _check_weight_memory(Path(resolved), device, dtype)
            processor = AutoProcessor.from_pretrained(resolved, local_files_only=True, trust_remote_code=False)
            model = AutoModelForImageTextToText.from_pretrained(
                resolved, local_files_only=True, trust_remote_code=False,
                use_safetensors=True, dtype=dtype,
            ).to(device).eval()
            cached = (processor, model)
            local._MODEL_CACHE[key] = cached
    return cached[0], cached[1], device


def _bounded_image(path: Path, width: int, height: int, max_pixels: int) -> Image.Image:
    with Image.open(path) as image:
        if image.size != (width, height):
            raise ValueError("图片实际尺寸与标注数据不一致，请重新导入图片")
        if width <= 0 or height <= 0 or width * height > MAX_SOURCE_PIXELS:
            raise ValueError("Qwen3.5 标注图片不能超过 5000 万像素")
        scale = min(1.0, math.sqrt(max_pixels / (width * height)))
        image.thumbnail((max(1, int(width * scale)), max(1, int(height * scale))), Image.Resampling.LANCZOS)
        return image.convert("RGB")


def _predictions(text: str, width: int, height: int, labels: list[str], hints: dict) -> list[Prediction]:
    task = hints.get("task_type", "detect")
    try:
        payload = parse_json_response(text)
    except (ValueError, TypeError) as exc:
        raise QwenLocalError("Qwen3.5 未返回有效标注 JSON，请简化提示后重试") from exc
    # Qwen may emit its native grounding list/bbox_2d schema even when asked
    # for an objects wrapper. Translate only these explicit, equivalent forms.
    if isinstance(payload, list):
        objects = payload
    elif isinstance(payload, dict):
        objects = payload.get("objects", payload.get("annotations"))
    else:
        objects = None
    if not isinstance(objects, list):
        raise QwenLocalError("Qwen3.5 输出缺少约定的 objects 标注列表")
    objects = [dict(item, bbox=item["bbox_2d"]) if isinstance(item, dict)
               and "bbox_2d" in item and "bbox" not in item else item for item in objects]
    allowed = {label.strip().casefold(): label for label in labels}
    project = {"task_type": task, "keypoint_schema_json": json.dumps(hints.get("keypoint_schema") or {})}
    result = []
    for prediction in normalize_predictions(objects, width, height, coordinate_space="normalized_1000", task_type=task):
        label = allowed.get(prediction.label.casefold())
        if label is None:
            continue
        try:
            validate_task_annotation(project, prediction.shape_type, prediction.points, prediction.keypoints, width, height)
            json.dumps(prediction.attributes, allow_nan=False)
        except (ValueError, TypeError):
            continue
        except Exception as exc:
            from fastapi import HTTPException
            if isinstance(exc, HTTPException):
                continue
            raise
        prediction.label = label
        result.append(prediction)
    if objects and not result:
        raise QwenLocalError("Qwen3.5 输出未包含符合当前类别与任务要求的有效标注，请简化提示后重试")
    if task == "classify":
        result = sorted(result, key=lambda item: item.confidence or 0, reverse=True)[:1]
    return result


class LocalQwenProvider(AnnotationProvider):
    def __init__(self, info: ModelInfo, model_path: str, device: str = "auto", *,
                 max_new_tokens: int = 2048, max_image_pixels: int = MAX_IMAGE_PIXELS) -> None:
        if type(max_new_tokens) is not int or not 1 <= max_new_tokens <= MAX_OUTPUT_TOKENS:
            raise ValueError(f"Qwen 输出 token 上限必须为 1–{MAX_OUTPUT_TOKENS}")
        if type(max_image_pixels) is not int or not 4096 <= max_image_pixels <= MAX_IMAGE_PIXELS:
            raise ValueError(f"Qwen 图片像素上限必须为 4096–{MAX_IMAGE_PIXELS}")
        self.info, self.model_path, self.device = info, model_path, device
        self.max_new_tokens, self.max_image_pixels = max_new_tokens, max_image_pixels

    async def predict(self, image_path: Path, width: int, height: int, prompt: str, labels: list[str],
                      hints: dict[str, Any] | None = None) -> list[Prediction]:
        hints = dict(hints or {})
        if hints.get("task_type", "detect") not in {"detect", "segment", "classify", "pose"}:
            raise ValueError("Qwen3.5 标注仅支持检测、分割、分类和关键点任务")
        if not labels:
            return []
        instruction = annotation_instruction(prompt, labels, hints)
        if len(instruction) > MAX_INSTRUCTION_CHARS:
            raise ValueError("Qwen 标注提示过长，请减少提示、类别或关键点定义")
        return await asyncio.to_thread(self._predict_sync, image_path, width, height, instruction, labels, hints)

    @serialized_local_inference
    def _predict_sync(self, image_path: Path, width: int, height: int, instruction: str,
                      labels: list[str], hints: dict) -> list[Prediction]:
        import torch

        available, error = local_qwen_runtime_status(self.model_path)
        if not available:
            raise QwenLocalError(error)
        image = _bounded_image(image_path, width, height, self.max_image_pixels)
        try:
            processor, model, device = _load_qwen(self.model_path, self.device)
            messages = [{"role": "user", "content": [
                {"type": "image", "image": image}, {"type": "text", "text": instruction},
            ]}]
            inputs = processor.apply_chat_template(
                messages, tokenize=True, add_generation_prompt=True, return_dict=True, return_tensors="pt",
                enable_thinking=False,
                processor_kwargs={"images_kwargs": {"size": {"shortest_edge": 4096, "longest_edge": self.max_image_pixels}}},
            )
            input_length = inputs["input_ids"].shape[-1]
            if input_length > MAX_INPUT_TOKENS:
                raise ValueError("Qwen 输入超过 8192 tokens，请减少提示、类别或图片尺寸")
            inputs = {key: value.to(device) if hasattr(value, "to") else value for key, value in inputs.items()}
            with torch.inference_mode():
                output = model.generate(**inputs, max_new_tokens=self.max_new_tokens, do_sample=False)
            tokens = output[0, input_length:]
            eos = getattr(model.generation_config, "eos_token_id", None)
            eos = eos if isinstance(eos, (tuple, list)) else [eos]
            if len(tokens) >= self.max_new_tokens and int(tokens[-1]) not in eos:
                raise QwenLocalError("Qwen 输出达到 token 上限，标注可能不完整；请减少目标数量或简化要求后重试")
            text = processor.decode(tokens, skip_special_tokens=True)
            return _predictions(text, width, height, labels, hints)
        except (MemoryError, torch.OutOfMemoryError) as exc:
            raise QwenLocalError("Qwen3.5 本地推理内存或显存不足；本模式在单个设备加载完整权重，请使用较小模型、缩小图片或释放设备内存") from exc
        finally:
            image.close()
