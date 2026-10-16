from __future__ import annotations

import asyncio
import re
import sys
import threading
from contextlib import nullcontext
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from PIL import Image

from .base import AnnotationProvider, ModelInfo, Prediction
from .execution import serialized_local_inference
from ..worker_client import on_worker


_MODEL_CACHE: dict[tuple[str, str, str], tuple[Any, Any]] = {}
_CACHE_LOCK = threading.Lock()
_MODEL_DEVICES: dict[tuple[str, str], str] = {}
_DEVICE_LOCK = threading.Lock()


def grounding_labels_from_prompt(prompt: str, fallback: list[str]) -> list[str]:
    """Resolve Grounding DINO class phrases from its user-facing prompt.

    Grounding DINO is a class-conditioned detector rather than an instruction
    following VLM. The browser therefore sends a comma-separated class prompt.
    The marked-sentence branch keeps jobs created by the previous UI compatible.
    """
    value = prompt.strip()
    marked = re.search(r"(?:目标|类别)\s*[:：]\s*([^\n。；;]+)", value, re.IGNORECASE)
    candidate = marked.group(1) if marked else value
    if not marked and re.search(r"[\u3400-\u9fff]", candidate) and re.search(r"\s", candidate):
        return list(fallback)
    simple_list = bool(re.fullmatch(r"[\w\- .,/|，、；;]+", candidate, re.UNICODE))
    if not simple_list:
        return list(fallback)
    labels: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[,/|，、；;\n]+", candidate):
        label = item.strip().strip(".")
        if not label or len(label) > 80:
            continue
        key = label.casefold()
        if key not in seen:
            seen.add(key)
            labels.append(label)
    return labels or list(fallback)


@on_worker("local-status")
def local_runtime_status(model_path: str) -> tuple[bool, str]:
    path = Path(model_path)
    if not path.is_dir():
        return False, f"模型目录不存在：{path}"
    if not (path / "config.json").is_file() or not (path / "model.safetensors").is_file():
        return False, f"模型文件不完整：{path}"
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError as exc:
        return False, f"缺少本地推理依赖：{exc.name}"
    return True, ""


def _ensure_sam3_import_path(source_dir: str) -> None:
    if find_spec("sam3") is not None:
        return
    source = Path(source_dir).expanduser().resolve()
    if not (source / "sam3" / "__init__.py").is_file():
        raise ImportError(f"SAM3 源码目录不存在或不完整：{source}")
    source_value = str(source)
    if source_value not in sys.path:
        sys.path.insert(0, source_value)


@on_worker("sam3-status")
def local_sam3_runtime_status(checkpoint_path: str, source_dir: str) -> tuple[bool, str]:
    checkpoint = Path(checkpoint_path).expanduser()
    if not checkpoint.is_file():
        return False, f"SAM3 权重不存在：{checkpoint}"
    if checkpoint.stat().st_size < 1024 * 1024:
        return False, f"SAM3 权重文件异常：{checkpoint}"
    try:
        _ensure_sam3_import_path(source_dir)
        for module_name in (
            "torch",
            "torchvision",
            "timm",
            "ftfy",
            "iopath",
            "einops",
            "pycocotools",
            "psutil",
            "pkg_resources",
            "cv2",
        ):
            if find_spec(module_name) is None:
                return False, f"缺少 SAM3 本地推理依赖：{module_name}"
    except (ImportError, ValueError) as exc:
        return False, str(exc)
    return True, ""


def _device(requested: str) -> str:
    import torch

    if requested == "auto":
        if not torch.cuda.is_available():
            return "cpu"
        # Large segmentation models are sensitive to fragmentation. Pick the GPU
        # with the most currently free memory instead of always occupying cuda:0.
        best_index = max(
            range(torch.cuda.device_count()),
            key=lambda index: torch.cuda.mem_get_info(index)[0],
        )
        return f"cuda:{best_index}"
    if requested.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError("已配置 CUDA 推理，但 PyTorch 未检测到可用 GPU")
    return requested


def _model_device(model_kind: str, model_path: str, requested: str) -> str:
    if requested != "auto":
        return _device(requested)
    identity = (model_kind, str(Path(model_path).expanduser().resolve()))
    with _DEVICE_LOCK:
        if identity not in _MODEL_DEVICES:
            _MODEL_DEVICES[identity] = _device("auto")
        return _MODEL_DEVICES[identity]


def _load_grounding_dino(model_path: str, requested_device: str) -> tuple[Any, Any, str]:
    from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

    resolved_path = str(Path(model_path).resolve())
    device = _model_device("grounding-dino", resolved_path, requested_device)
    key = ("grounding-dino", resolved_path, device)
    with _CACHE_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached is None:
            processor = AutoProcessor.from_pretrained(model_path, local_files_only=True)
            model = AutoModelForZeroShotObjectDetection.from_pretrained(
                model_path, local_files_only=True
            ).to(device).eval()
            cached = (processor, model)
            _MODEL_CACHE[key] = cached
    return cached[0], cached[1], device


def _load_sam2(model_path: str, requested_device: str) -> tuple[Any, Any, str]:
    from transformers import Sam2Model, Sam2Processor

    resolved_path = str(Path(model_path).resolve())
    device = _model_device("sam2", resolved_path, requested_device)
    key = ("sam2", resolved_path, device)
    with _CACHE_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached is None:
            processor = Sam2Processor.from_pretrained(model_path, local_files_only=True)
            model = Sam2Model.from_pretrained(model_path, local_files_only=True).to(device).eval()
            cached = (processor, model)
            _MODEL_CACHE[key] = cached
    return cached[0], cached[1], device


def _load_sam3(
    checkpoint_path: str, source_dir: str, requested_device: str
) -> tuple[Any, Any, str]:
    import torch

    _ensure_sam3_import_path(source_dir)
    from sam3.model.sam3_image_processor import Sam3Processor
    from sam3.model_builder import build_sam3_image_model

    resolved_path = str(Path(checkpoint_path).expanduser().resolve())
    device = _model_device("sam3", resolved_path, requested_device)
    key = ("sam3", resolved_path, device)
    with _CACHE_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached is None:
            if device.startswith("cuda"):
                cuda_index = torch.device(device).index
                if cuda_index is None:
                    cuda_index = torch.cuda.current_device()
                # The official builder currently recognizes the literal "cuda"
                # only; the CUDA context makes it honor an indexed device.
                with torch.cuda.device(cuda_index):
                    model = build_sam3_image_model(
                        checkpoint_path=resolved_path,
                        load_from_HF=False,
                        device="cuda",
                        eval_mode=True,
                        enable_segmentation=True,
                    )
            else:
                model = build_sam3_image_model(
                    checkpoint_path=resolved_path,
                    load_from_HF=False,
                    device="cpu",
                    eval_mode=True,
                    enable_segmentation=True,
                )
            processor = Sam3Processor(
                model,
                device=device,
                # The task layer applies the user-selected threshold. Keeping a
                # small floor here avoids returning numerically meaningless masks.
                confidence_threshold=0.05,
            )
            cached = (processor, model)
            _MODEL_CACHE[key] = cached
    return cached[0], cached[1], device


def _sam3_mask_prediction(
    mask: Any,
    box: Any,
    score: Any,
    label: str,
    width: int,
    height: int,
) -> Prediction | None:
    import cv2
    import numpy as np

    mask_array = mask.detach().cpu().squeeze().numpy().astype("uint8")
    contours, _ = cv2.findContours(mask_array, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if contours:
        contour = max(contours, key=cv2.contourArea)
        if cv2.contourArea(contour) >= 1:
            perimeter = cv2.arcLength(contour, True)
            polygon = contour
            for ratio in (0.0025, 0.005, 0.01, 0.02):
                polygon = cv2.approxPolyDP(contour, max(1.0, ratio * perimeter), True)
                if len(polygon) <= 256:
                    break
            polygon = polygon.reshape(-1, 2)
            if len(polygon) >= 3:
                polygon[:, 0] = np.clip(polygon[:, 0], 0, width)
                polygon[:, 1] = np.clip(polygon[:, 1], 0, height)
                return Prediction(
                    label=label,
                    shape_type="polygon",
                    points=[round(float(value), 2) for value in polygon.ravel().tolist()],
                    confidence=float(score),
                    attributes={},
                )

    # A degenerate mask should not discard an otherwise valid detection.
    x1, y1, x2, y2 = [float(value) for value in box.detach().cpu().tolist()]
    x1, x2 = sorted((max(0.0, min(width, x1)), max(0.0, min(width, x2))))
    y1, y2 = sorted((max(0.0, min(height, y1)), max(0.0, min(height, y2))))
    if x2 - x1 < 1 or y2 - y1 < 1:
        return None
    return Prediction(
        label=label,
        shape_type="rectangle",
        points=[round(value, 2) for value in (x1, y1, x2, y2)],
        confidence=float(score),
        attributes={},
    )


class LocalGroundingDinoProvider(AnnotationProvider):
    def __init__(self, info: ModelInfo, model_path: str, device: str = "auto") -> None:
        self.info = info
        self.model_path = model_path
        self.device = device

    async def predict(
        self,
        image_path: Path,
        width: int,
        height: int,
        prompt: str,
        labels: list[str],
        hints: dict[str, Any] | None = None,
    ) -> list[Prediction]:
        confidence_threshold = float((hints or {}).get("confidence_threshold", 0.35))
        target_labels = grounding_labels_from_prompt(prompt, labels)
        if not target_labels:
            return []
        available, error = local_runtime_status(self.model_path)
        if not available:
            raise RuntimeError(error)
        return await asyncio.to_thread(
            self._predict_sync, image_path, width, height, target_labels, confidence_threshold
        )

    @serialized_local_inference
    def _predict_sync(
        self, image_path: Path, width: int, height: int, labels: list[str], confidence_threshold: float = 0.35
    ) -> list[Prediction]:
        import torch

        processor, model, device = _load_grounding_dino(self.model_path, self.device)
        image = Image.open(image_path).convert("RGB")
        # A full stop is the class separator expected by Grounding DINO.
        text = ". ".join(label.strip().lower() for label in labels if label.strip()) + "."
        inputs = processor(images=image, text=text, return_tensors="pt")
        model_inputs = {
            key: value.to(device) if hasattr(value, "to") else value
            for key, value in inputs.items()
        }
        with torch.inference_mode():
            outputs = model(**model_inputs)
        result = processor.post_process_grounded_object_detection(
            outputs,
            model_inputs["input_ids"],
            threshold=min(0.20, confidence_threshold),
            text_threshold=min(0.20, confidence_threshold),
            target_sizes=[(height, width)],
        )[0]
        predictions: list[Prediction] = []
        allowed = {label.casefold(): label for label in labels}
        for box, score, raw_label in zip(
            result["boxes"].detach().cpu().tolist(),
            result["scores"].detach().cpu().tolist(),
            result["text_labels"],
        ):
            label_text = str(raw_label).strip().strip(".")
            label = allowed.get(label_text.casefold())
            if label is None:
                # Processor phrases can include an article; prefer an exact allowed suffix.
                label = next(
                    (value for key, value in allowed.items() if label_text.casefold().endswith(key)),
                    None,
                )
            if label is None:
                continue
            predictions.append(
                Prediction(
                    label=label,
                    shape_type="rectangle",
                    points=[round(float(value), 2) for value in box],
                    confidence=float(score),
                    attributes={},
                )
            )
        return predictions


class LocalSam2Provider(AnnotationProvider):
    def __init__(self, info: ModelInfo, model_path: str, device: str = "auto") -> None:
        self.info = info
        self.model_path = model_path
        self.device = device

    async def predict(
        self,
        image_path: Path,
        width: int,
        height: int,
        prompt: str,
        labels: list[str],
        hints: dict[str, Any] | None = None,
    ) -> list[Prediction]:
        del prompt
        available, error = local_runtime_status(self.model_path)
        if not available:
            raise RuntimeError(error)
        if not hints:
            raise RuntimeError("SAM2 半自动分割需要正负点或粗框提示")
        label = str(hints.get("label") or (labels[0] if labels else "")).strip()
        if label not in labels:
            label = labels[0] if labels else ""
        if not label:
            return []
        return await asyncio.to_thread(
            self._predict_sync, image_path, width, height, label, hints
        )

    @serialized_local_inference
    def _predict_sync(
        self,
        image_path: Path,
        width: int,
        height: int,
        label: str,
        hints: dict[str, Any],
    ) -> list[Prediction]:
        import cv2
        import numpy as np
        import torch

        processor, model, device = _load_sam2(self.model_path, self.device)
        image = Image.open(image_path).convert("RGB")
        processor_kwargs: dict[str, Any] = {"images": image, "return_tensors": "pt"}
        hint_points = hints.get("points") or []
        if hint_points:
            processor_kwargs["input_points"] = [[[
                [float(point["x"]), float(point["y"])] for point in hint_points
            ]]]
            processor_kwargs["input_labels"] = [[[int(point.get("label", 1)) for point in hint_points]]]
        shape_type = hints.get("shape_type")
        shape_points = hints.get("points") if shape_type in {"rectangle", "polygon"} else None
        if shape_points and not isinstance(shape_points[0], dict):
            xs = [float(value) for value in shape_points[0::2]]
            ys = [float(value) for value in shape_points[1::2]]
            processor_kwargs["input_boxes"] = [[[min(xs), min(ys), max(xs), max(ys)]]]
            processor_kwargs.pop("input_points", None)
            processor_kwargs.pop("input_labels", None)
        inputs = processor(**processor_kwargs)
        original_sizes = inputs["original_sizes"]
        model_inputs = {
            key: value.to(device) if hasattr(value, "to") else value
            for key, value in inputs.items()
            if key not in {"original_sizes", "reshaped_input_sizes"}
        }
        with torch.inference_mode():
            outputs = model(**model_inputs, multimask_output=True)
        masks = processor.post_process_masks(outputs.pred_masks.cpu(), original_sizes)[0]
        scores = outputs.iou_scores.detach().cpu().reshape(-1)
        best_index = int(torch.argmax(scores).item())
        mask = masks.reshape(-1, height, width)[best_index].numpy().astype("uint8")
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return []
        contour = max(contours, key=cv2.contourArea)
        if cv2.contourArea(contour) < 1:
            return []
        epsilon = max(1.0, 0.0025 * cv2.arcLength(contour, True))
        polygon = cv2.approxPolyDP(contour, epsilon, True).reshape(-1, 2)
        if len(polygon) < 3:
            return []
        points = [round(float(value), 2) for value in np.ravel(polygon).tolist()]
        return [
            Prediction(
                label=label,
                shape_type="polygon",
                points=points,
                confidence=float(scores[best_index]),
                attributes={},
            )
        ]


class LocalSam3Provider(AnnotationProvider):
    def __init__(
        self,
        info: ModelInfo,
        checkpoint_path: str,
        source_dir: str,
        device: str = "auto",
    ) -> None:
        self.info = info
        self.checkpoint_path = checkpoint_path
        self.source_dir = source_dir
        self.device = device

    async def predict(
        self,
        image_path: Path,
        width: int,
        height: int,
        prompt: str,
        labels: list[str],
        hints: dict[str, Any] | None = None,
    ) -> list[Prediction]:
        confidence_threshold = float((hints or {}).get("confidence_threshold", 0.35))
        target_labels = grounding_labels_from_prompt(prompt, labels)
        if not target_labels:
            return []
        available, error = local_sam3_runtime_status(
            self.checkpoint_path, self.source_dir
        )
        if not available:
            raise RuntimeError(error)
        return await asyncio.to_thread(
            self._predict_sync, image_path, width, height, target_labels, confidence_threshold
        )

    @serialized_local_inference
    def _predict_sync(
        self, image_path: Path, width: int, height: int, labels: list[str], confidence_threshold: float = 0.35
    ) -> list[Prediction]:
        import torch

        processor, _model, device = _load_sam3(
            self.checkpoint_path, self.source_dir, self.device
        )
        # Keep the broad candidate set for preview diagnostics while allowing a
        # user threshold below the previous 0.05 floor to reveal faint objects.
        processor.set_confidence_threshold(min(0.05, confidence_threshold))
        with Image.open(image_path) as opened:
            image = opened.convert("RGB")
        autocast = (
            torch.autocast(device_type="cuda", dtype=torch.bfloat16)
            if device.startswith("cuda")
            else nullcontext()
        )
        with torch.inference_mode(), autocast:
            state = processor.set_image(image)

        predictions: list[Prediction] = []
        for index, label in enumerate(labels):
            if index:
                processor.reset_all_prompts(state)
            with torch.inference_mode(), (
                torch.autocast(device_type="cuda", dtype=torch.bfloat16)
                if device.startswith("cuda")
                else nullcontext()
            ):
                output = processor.set_text_prompt(prompt=label, state=state)
            for mask, box, score in zip(
                output["masks"], output["boxes"], output["scores"]
            ):
                prediction = _sam3_mask_prediction(
                    mask, box, score, label, width, height
                )
                if prediction is not None:
                    predictions.append(prediction)
        return predictions
