from __future__ import annotations

import asyncio
import importlib.metadata
import math
import re
import sys
import threading
import zipfile
from contextlib import contextmanager, nullcontext
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
    try:
        if not path.is_dir():
            return False, f"模型目录不存在：{path}"
        if not (path / "config.json").is_file() or not (path / "model.safetensors").is_file():
            return False, f"模型文件不完整：{path}"
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError as exc:
        return False, f"缺少本地推理依赖：{exc.name}"
    except (OSError, RuntimeError) as exc:
        return False, f"本地模型不可用，请检查文件权限和依赖：{str(exc)[:350]}"
    return True, ""


_SAM2_FILENAMES = {"sam2.1_t.pt", "sam2.1_hiera_tiny.pt"}


def _checkpoint_status(path: Path, name: str) -> tuple[bool, str]:
    try:
        if not path.is_file():
            return False, f"{name} 权重不存在，请手动放置到：{path}"
        if path.stat().st_size == 0:
            return False, f"{name} 权重文件为空或不完整：{path}"
        _check_weight_container(path)
    except ImportError as exc:
        return False, f"{name} 缺少权重格式检查依赖：{exc.name}"
    except (ValueError, zipfile.BadZipFile, EOFError) as exc:
        return False, f"{name} 权重文件格式错误或不完整：{str(exc)[:200]}（{path}）"
    except (OSError, RuntimeError):
        return False, f"{name} 权重不可读，请检查文件权限：{path}"
    return True, ""


def _check_weight_container(path: Path) -> None:
    """Validate container metadata without unpickling or allocating tensors.

    This catches placeholder files and interrupted copies. Parameter names,
    shapes and the actual weight contents are still checked when loading SAM.
    Official SAM checkpoints use torch.save's ZIP format; legacy pickle/tar
    files cannot provide this cheap completeness check and are not accepted.
    """
    if path.suffix == ".safetensors":
        from safetensors import SafetensorError, safe_open

        with path.open("rb") as stream:
            header_size = int.from_bytes(stream.read(8), "little")
        if not 2 <= header_size <= min(16 * 1024 * 1024, path.stat().st_size - 8):
            raise ValueError("safetensors 文件头缺失或长度异常")
        try:
            with safe_open(str(path), framework="numpy") as weights:
                if not weights.keys():
                    raise ValueError("safetensors 文件中没有权重")
        except SafetensorError as exc:
            raise ValueError("safetensors 元数据或张量存储范围不完整") from exc
        return

    with path.open("rb") as stream:
        if stream.read(4) != b"PK\x03\x04":
            raise ValueError("需要官方 PyTorch ZIP checkpoint，不支持普通文件或旧 pickle/tar 格式")
        with zipfile.ZipFile(stream) as archive:
            entries = archive.infolist()
            by_name = {entry.filename: entry for entry in entries}
            roots = {entry.filename.split("/", 1)[0] for entry in entries}
            if not entries or len(by_name) != len(entries) or len(roots) != 1:
                raise ValueError("PyTorch ZIP 目录结构异常")
            root = roots.pop()
            pickle_record = by_name.get(f"{root}/data.pkl")
            version_record = by_name.get(f"{root}/version")
            storages = [entry for entry in entries
                        if re.fullmatch(re.escape(root) + r"/data/\d+", entry.filename) and entry.file_size > 0]
            if not root or not pickle_record or pickle_record.file_size == 0 or not version_record or not storages:
                raise ValueError("PyTorch ZIP 缺少参数索引、版本或张量存储")
            for entry in entries:
                if ".." in Path(entry.filename).parts or entry.header_offset < 0 or entry.header_offset >= archive.start_dir:
                    raise ValueError("PyTorch ZIP 文件记录越界")
                # Opening a member checks its local header and central-directory
                # bounds. No read() here: tensor payloads remain untouched.
                with archive.open(entry):
                    pass
            if not 1 <= version_record.file_size <= 16:
                raise ValueError("PyTorch ZIP 版本记录异常")
            if not archive.read(version_record).strip().isdigit():
                raise ValueError("PyTorch ZIP 版本记录无效")


def _ultralytics_sam_status(*, semantic: bool = False) -> tuple[bool, str]:
    for module_name in ("torch", "torchvision", "ultralytics", "cv2"):
        if find_spec(module_name) is None:
            return False, f"缺少本地 SAM 推理依赖：{module_name}"
    try:
        version = importlib.metadata.version("ultralytics")
        if tuple(int(part) for part in version.split(".")[:3]) < (8, 4, 38):
            return False, "本地 SAM 推理需要 ultralytics 8.4.38 或更新版本"
    except (importlib.metadata.PackageNotFoundError, ValueError):
        return False, "无法确认 ultralytics 版本，请安装 requirements-models.txt"
    if semantic:
        for module_name in ("clip", "ftfy", "regex"):
            if find_spec(module_name) is None:
                return False, f"SAM3 缺少 {module_name} 分词依赖，请安装更新后的 requirements-models.txt"
        clip_spec = find_spec("clip")
        directory = Path(clip_spec.origin).parent if clip_spec and clip_spec.origin else None
        if not directory or not (directory / "bpe_simple_vocab_16e6.txt.gz").is_file():
            return False, "SAM3 的 CLIP 分词词表缺失，请重新安装锁定的 CLIP 包；无需下载 CLIP 模型权重"
    return True, ""


@on_worker("sam2-status")
def local_sam2_runtime_status(model_path: str) -> tuple[bool, str]:
    try:
        path = Path(model_path).expanduser()
        if path.is_dir():
            required = ("config.json", "model.safetensors", "preprocessor_config.json", "processor_config.json")
            missing = [name for name in required if not (path / name).is_file()]
            if missing:
                return False, f"SAM2 Hugging Face 目录不完整，缺少 {', '.join(missing)}：{path}"
            available, error = _checkpoint_status(path / "model.safetensors", "SAM2")
            return local_runtime_status(str(path)) if available else (available, error)
        if path.name not in _SAM2_FILENAMES:
            return False, f"SAM2 单文件需命名为 sam2.1_t.pt 或 sam2.1_hiera_tiny.pt：{path}"
        available, error = _checkpoint_status(path, "SAM2")
        return _ultralytics_sam_status() if available else (available, error)
    except (OSError, RuntimeError, ValueError) as exc:
        return False, f"SAM2 本地模型不可用，请检查文件权限和依赖：{str(exc)[:350]}"


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
    try:
        checkpoint = Path(checkpoint_path).expanduser()
        available, error = _checkpoint_status(checkpoint, "SAM3")
        if not available:
            return available, error
        available, ultralytics_error = _ultralytics_sam_status(semantic=True)
        if available:
            return True, ""
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
                    return False, f"{ultralytics_error}；旧 SAM3 SDK 还缺少依赖：{module_name}"
            source = find_spec("sam3")
            if not source or not source.origin or not (Path(source.origin).parent / "assets" / "bpe_simple_vocab_16e6.txt.gz").is_file():
                return False, f"{ultralytics_error}；旧 SAM3 SDK 缺少内置 BPE 分词词表"
        except (ImportError, ValueError) as exc:
            return False, f"{ultralytics_error}；{exc}"
        return True, ""
    except (OSError, RuntimeError, ValueError) as exc:
        return False, f"SAM3 本地模型不可用，请检查文件权限和依赖：{str(exc)[:350]}"


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
    resolved_path = str(Path(model_path).expanduser().resolve())
    device = _model_device("sam2", resolved_path, requested_device)
    key = ("sam2", resolved_path, device)
    with _CACHE_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached is None:
            if Path(resolved_path).is_dir():
                from transformers import Sam2Model, Sam2Processor

                processor = Sam2Processor.from_pretrained(resolved_path, local_files_only=True)
                model = Sam2Model.from_pretrained(resolved_path, local_files_only=True).to(device).eval()
            else:
                available, error = local_sam2_runtime_status(resolved_path)
                if not available:
                    raise RuntimeError(error)
                from ultralytics.models.sam import build
                from ultralytics.models.sam.predict import SAM2Predictor

                # Both supported filenames contain "sam2.1", which enables the
                # 2.1 spatial/temporal embeddings in the upstream tiny builder.
                # Its download fallback is replaced only inside our build lock.
                with _local_sam_loader(build):
                    model = build.build_sam2_t(checkpoint=resolved_path)
                predictor = SAM2Predictor(overrides={
                    "model": resolved_path, "device": device, "imgsz": 1024,
                    "task": "segment", "mode": "predict", "verbose": False, "save": False,
                })
                predictor.setup_model(model=model, verbose=False)
                model = predictor.model
                processor = _UltralyticsSam2Processor(predictor)
            cached = (processor, model)
            _MODEL_CACHE[key] = cached
    return cached[0], cached[1], device


@contextmanager
def _local_sam_loader(module: Any, *, semantic: bool = False):
    """Use strictly local weights while the caller holds _CACHE_LOCK.

    SAM2's upstream checkpoint helper can download missing assets. SAM3's
    helper silently accepts an unrelated/partial checkpoint with strict=False.
    Keep the upstream architectures, but reject both cases in this application.
    No process-wide download function or training loader is changed.
    """
    import torch

    def load(model, checkpoint):
        with Path(checkpoint).open("rb") as stream:
            weights = torch.load(stream, map_location="cpu", weights_only=True)
        if isinstance(weights, dict) and isinstance(weights.get("model"), dict):
            weights = weights["model"]
        if not isinstance(weights, dict):
            raise RuntimeError("SAM 权重格式不正确，请使用官方完整 checkpoint")
        if semantic:
            weights = {key.removeprefix("detector."): value for key, value in weights.items()
                       if key.startswith("detector.")}
            if not weights:
                raise RuntimeError("SAM3 权重不包含 detector，请使用官方完整 sam3.pt")
            incompatible = model.load_state_dict(weights, strict=False)
            if incompatible.missing_keys:
                raise RuntimeError("SAM3 权重不完整或版本不兼容，缺少模型参数："
                                   + ", ".join(incompatible.missing_keys[:3]))
        else:
            model.load_state_dict(weights)
        return model

    original = module._load_checkpoint
    module._load_checkpoint = load
    try:
        yield
    finally:
        module._load_checkpoint = original


class _UltralyticsSam2Processor:
    def __init__(self, predictor: Any) -> None:
        self.predictor = predictor

    def masks(self, image: Image.Image, prompts: dict[str, Any]):
        predictor = self.predictor
        predictor.reset_image()
        predictor.set_image(image)
        masks, boxes = predictor.inference_features(
            predictor.features, (image.height, image.width), multimask_output=True, **prompts
        )
        return masks, boxes[:, 4]


class _UltralyticsSam3Processor:
    """Small SDK-compatible adapter; image features are shared across labels."""
    def __init__(self, predictor: Any) -> None:
        self.predictor = predictor

    def set_confidence_threshold(self, threshold: float) -> None:
        self.predictor.args.conf = threshold

    def set_image(self, image: Image.Image) -> dict[str, Any]:
        self.predictor.reset_image()
        self.predictor.reset_prompts()
        self.predictor.set_image(image)
        return {"image_size": (image.height, image.width)}

    def reset_all_prompts(self, state: dict[str, Any]) -> None:
        self.predictor.reset_prompts()

    def set_text_prompt(self, *, prompt: str, state: dict[str, Any]) -> dict[str, Any]:
        masks, boxes = self.predictor.inference_features(
            self.predictor.features, state["image_size"], text=[prompt]
        )
        return {"masks": [] if masks is None else masks, "boxes": boxes[:, :4], "scores": boxes[:, 4]}


def _load_sam3(
    checkpoint_path: str, source_dir: str, requested_device: str
) -> tuple[Any, Any, str]:
    import torch

    resolved_path = str(Path(checkpoint_path).expanduser().resolve())
    device = _model_device("sam3", resolved_path, requested_device)
    key = ("sam3", resolved_path, device)
    with _CACHE_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached is None:
            available, error = _checkpoint_status(Path(resolved_path), "SAM3")
            if not available:
                raise RuntimeError(error)
            if _ultralytics_sam_status(semantic=True)[0]:
                # Import the tokenizer now: a broken installation must fail here,
                # before the upstream builder can attempt to install it itself.
                from clip.simple_tokenizer import SimpleTokenizer
                from ultralytics.models.sam import build_sam3
                from ultralytics.models.sam.predict import SAM3SemanticPredictor

                SimpleTokenizer()
                predictor = SAM3SemanticPredictor(overrides={
                    "model": resolved_path, "device": device, "imgsz": 1008,
                    "task": "segment", "mode": "predict", "conf": 0.05,
                    "verbose": False, "save": False,
                })
                with _local_sam_loader(build_sam3, semantic=True):
                    predictor.setup_model(verbose=False)
                cached = (_UltralyticsSam3Processor(predictor), predictor.model)
                _MODEL_CACHE[key] = cached
                return cached[0], cached[1], device

            _ensure_sam3_import_path(source_dir)
            from sam3.model.sam3_image_processor import Sam3Processor
            from sam3.model_builder import build_sam3_image_model

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


def _sam2_prompts(hints: dict[str, Any]) -> dict[str, Any]:
    """Keep all positive/negative points in one object, or use a rough box."""
    points = hints.get("points") or []
    try:
        if hints.get("shape_type") in {"rectangle", "polygon"} and points and not isinstance(points[0], dict):
            if len(points) < 4 or len(points) % 2:
                raise ValueError
            coordinates = [float(value) for value in points]
            if not all(math.isfinite(value) for value in coordinates):
                raise ValueError
            xs, ys = coordinates[0::2], coordinates[1::2]
            box = [min(xs), min(ys), max(xs), max(ys)]
            if box[0] == box[2] or box[1] == box[3]:
                raise ValueError
            return {"bboxes": [box]}
        if not points:
            raise ValueError
        coordinates = [[float(point["x"]), float(point["y"])] for point in points]
        labels = [int(point.get("label", 1)) for point in points]
        if not all(math.isfinite(value) for point in coordinates for value in point) or any(label not in {0, 1} for label in labels):
            raise ValueError
        return {"points": [coordinates], "labels": [labels]}
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError("SAM2 需要有效的正负点或粗框坐标") from exc


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
        available, error = local_sam2_runtime_status(self.model_path)
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
        with Image.open(image_path) as opened:
            image = opened.convert("RGB")
        prompts = _sam2_prompts(hints)
        with torch.inference_mode():
            if isinstance(processor, _UltralyticsSam2Processor):
                masks, scores = processor.masks(image, prompts)
                if masks is None or not len(scores):
                    return []
            else:
                processor_kwargs: dict[str, Any] = {"images": image, "return_tensors": "pt"}
                if "bboxes" in prompts:
                    processor_kwargs["input_boxes"] = [prompts["bboxes"]]
                else:
                    processor_kwargs["input_points"] = [prompts["points"]]
                    processor_kwargs["input_labels"] = [prompts["labels"]]
                inputs = processor(**processor_kwargs)
                original_sizes = inputs["original_sizes"]
                model_inputs = {
                    key: value.to(device) if hasattr(value, "to") else value
                    for key, value in inputs.items()
                    if key not in {"original_sizes", "reshaped_input_sizes"}
                }
                outputs = model(**model_inputs, multimask_output=True)
                masks = processor.post_process_masks(outputs.pred_masks.cpu(), original_sizes)[0]
                scores = outputs.iou_scores
        scores = scores.detach().cpu().reshape(-1)
        best_index = int(torch.argmax(scores).item())
        mask = masks.detach().cpu().reshape(-1, height, width)[best_index].numpy().astype("uint8")
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
