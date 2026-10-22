from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class KeypointSchema(BaseModel):
    names: list[str] = Field(min_length=1, max_length=200)
    skeleton: list[tuple[int, int]] = Field(default_factory=list, max_length=1000)
    flip_idx: list[int] | None = None

    @model_validator(mode="after")
    def validate_layout(self):
        self.names = [name.strip() for name in self.names]
        if any(not name or len(name) > 80 for name in self.names) or len(set(self.names)) != len(self.names):
            raise ValueError("关键点名称不能为空或重复，最长 80 字符")
        count = len(self.names)
        if any(a == b or min(a, b) < 0 or max(a, b) >= count for a, b in self.skeleton):
            raise ValueError("骨架连接必须使用有效的关键点索引（从 0 开始）")
        if self.flip_idx is not None:
            if sorted(self.flip_idx) != list(range(count)) or any(self.flip_idx[self.flip_idx[i]] != i for i in range(count)):
                raise ValueError("flip_idx 必须是对称的左右关键点映射")
        return self


class Keypoint(BaseModel):
    x: float = Field(ge=0, allow_inf_nan=False)
    y: float = Field(ge=0, allow_inf_nan=False)
    visibility: Literal[0, 1, 2] = 2


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    labels: list[str] = Field(default_factory=list, max_length=100)
    task_type: Literal["mixed", "detect", "segment", "classify", "pose"] = "mixed"
    keypoint_schema: KeypointSchema | None = None

    @model_validator(mode="after")
    def validate_task(self):
        if self.task_type == "pose" and self.keypoint_schema is None:
            raise ValueError("关键点项目必须定义关键点名称与顺序")
        if self.task_type != "pose" and self.keypoint_schema is not None:
            raise ValueError("仅关键点项目可配置关键点结构")
        return self

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("名称不能为空")
        return value.strip()

    @field_validator("labels")
    @classmethod
    def clean_labels(cls, value: list[str]) -> list[str]:
        labels: list[str] = []
        seen: set[str] = set()
        for item in value:
            label = item.strip()
            if not label:
                continue
            if len(label) > 80:
                raise ValueError("label names must not exceed 80 characters")
            key = label.casefold()
            if key not in seen:
                seen.add(key)
                labels.append(label)
        return labels


class ProjectArchiveUpdate(BaseModel):
    archived: bool


class LabelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    shortcut: str = Field(default="", max_length=1)
    attributes_schema: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("attributes_schema")
    @classmethod
    def validate_attributes_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        import json

        if len(json.dumps(value, ensure_ascii=False)) > 20_000:
            raise ValueError("attributes_schema is too large")
        if not value:
            return value
        if value.get("type", "object") != "object":
            raise ValueError("attributes_schema root type must be object")
        properties = value.get("properties", {})
        required = value.get("required", [])
        if not isinstance(properties, dict):
            raise ValueError("attributes_schema.properties must be an object")
        if not isinstance(required, list) or any(not isinstance(name, str) for name in required):
            raise ValueError("attributes_schema.required must be a string list")
        if set(required) - set(properties):
            raise ValueError("required attributes must exist in properties")
        allowed_types = {"string", "number", "integer", "boolean", "array", "object"}
        for name, rule in properties.items():
            if not isinstance(name, str) or not isinstance(rule, dict):
                raise ValueError("each attribute rule must be an object")
            if rule.get("type") and rule["type"] not in allowed_types:
                raise ValueError(f"unsupported attribute type: {rule['type']}")
            if "enum" in rule and not isinstance(rule["enum"], list):
                raise ValueError("attribute enum must be a list")
        return value


class AnnotationCreate(BaseModel):
    label_id: int
    shape_type: Literal["rectangle", "polygon", "classification", "keypoints"]
    points: list[float] = Field(default_factory=list, max_length=20000)
    keypoints: list[Keypoint] = Field(default_factory=list, max_length=200)
    attributes: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = Field(default=None, ge=0, le=1)
    source: Literal["manual", "auto", "assist"] = "manual"
    model_id: str | None = None
    expected_asset_revision: int | None = Field(default=None, ge=0)

    @field_validator("points")
    @classmethod
    def validate_points(cls, value: list[float], info: Any) -> list[float]:
        if len(value) % 2:
            raise ValueError("points must contain x/y pairs")
        if info.data.get("shape_type") == "rectangle" and len(value) != 4:
            raise ValueError("rectangle needs [x1, y1, x2, y2]")
        if info.data.get("shape_type") == "polygon" and len(value) < 6:
            raise ValueError("polygon needs at least three points")
        return value


class JobCreate(BaseModel):
    model_id: str
    prompt: str = Field(min_length=1, max_length=4000)
    target_labels: list[str] | None = Field(default=None, max_length=100)
    mode: Literal["auto", "assist"] = "auto"
    asset_ids: list[int] | None = None
    hint_annotation_id: int | None = None
    hints: dict[str, Any] | None = None
    replace_existing_auto: bool = True
    confidence_threshold: float = Field(default=0.35, ge=0, le=1)
    max_retries: int = Field(default=2, ge=0, le=10)
    include_reviewed: bool = False

    @field_validator("target_labels")
    @classmethod
    def clean_target_labels(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        labels: list[str] = []
        seen: set[str] = set()
        for item in value:
            label = item.strip()
            if not label:
                continue
            if len(label) > 80:
                raise ValueError("target label names must not exceed 80 characters")
            key = label.casefold()
            if key not in seen:
                seen.add(key)
                labels.append(label)
        if not labels:
            raise ValueError("target_labels must contain at least one label")
        return labels


class AnnotationPreviewCreate(JobCreate):
    asset_ids: list[Annotated[int, Field(strict=True, gt=0, le=9223372036854775807)]] = Field(min_length=1, max_length=3)

    @field_validator("asset_ids")
    @classmethod
    def unique_preview_assets(cls, value: list[int]) -> list[int]:
        if any(asset_id <= 0 for asset_id in value) or len(set(value)) != len(value):
            raise ValueError("预览请选择 1–3 张不同的有效图片")
        return value


class AnnotationUpdate(BaseModel):
    expected_version: int = Field(ge=1)
    label_id: int | None = None
    shape_type: Literal["rectangle", "polygon", "classification", "keypoints"] | None = None
    points: list[float] | None = None
    keypoints: list[Keypoint] | None = None
    attributes: dict[str, Any] | None = None


class AssetWorkflowUpdate(BaseModel):
    action: Literal["start", "submit", "approve", "reject", "reopen", "confirm_empty"]
    expected_revision: int = Field(ge=0)
    reason: str = Field(default="", max_length=1000)


class ModelServiceUpsert(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$")
    name: str = Field(min_length=1, max_length=100)
    family: str = Field(min_length=1, max_length=100)
    provider_type: Literal[
        "openai_vision", "remote_detection", "remote_segmentation", "interactive_segmentation",
        "remote_classification", "remote_keypoints"
    ]
    description: str = Field(default="", max_length=500)
    endpoint: str = Field(min_length=1, max_length=1000)
    model_name: str = Field(default="", max_length=200)
    api_key_env: str = Field(default="", pattern=r"^[A-Z][A-Z0-9_]*$|^$")
    capabilities: list[str] = Field(min_length=1)
    modes: list[Literal["auto", "assist"]] = Field(min_length=1)
    timeout_seconds: int = Field(default=180, ge=1, le=3600)
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("endpoint must use http or https")
        return value

    @model_validator(mode="after")
    def validate_provider_configuration(self) -> "ModelServiceUpsert":
        if self.provider_type == "interactive_segmentation" and "assist" not in self.modes:
            raise ValueError("interactive segmentation must enable assist mode")
        if self.provider_type == "openai_vision" and not self.model_name.strip():
            raise ValueError("OpenAI-compatible vision service requires model_name")
        self.capabilities = list(dict.fromkeys(item.strip() for item in self.capabilities if item.strip()))
        self.modes = list(dict.fromkeys(self.modes))
        if not self.capabilities:
            raise ValueError("at least one capability is required")
        return self


class AugmentationOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["horizontal_flip", "vertical_flip", "rotate90", "brightness", "contrast", "saturation", "grayscale", "blur", "noise", "gamma", "hue", "sharpness", "autocontrast", "equalize", "jpeg_compression", "downscale"]
    probability: float = Field(default=1.0, gt=0, le=1, strict=True)
    degrees: list[int] | None = None
    min_factor: float | None = Field(default=None, ge=0, le=3, strict=True)
    max_factor: float | None = Field(default=None, ge=0, le=3, strict=True)
    radius: float | None = Field(default=None, ge=0.1, le=5, strict=True)
    std: float | None = Field(default=None, ge=1, le=30, strict=True)
    min_gamma: float | None = Field(default=None, ge=0.2, le=3, strict=True)
    max_gamma: float | None = Field(default=None, ge=0.2, le=3, strict=True)
    min_shift: float | None = Field(default=None, ge=-0.5, le=0.5, strict=True)
    max_shift: float | None = Field(default=None, ge=-0.5, le=0.5, strict=True)
    cutoff: float | None = Field(default=None, ge=0, le=10, strict=True)
    min_quality: int | None = Field(default=None, ge=10, le=100, strict=True)
    max_quality: int | None = Field(default=None, ge=10, le=100, strict=True)
    min_scale: float | None = Field(default=None, ge=0.1, le=1, strict=True)
    max_scale: float | None = Field(default=None, ge=0.1, le=1, strict=True)

    @field_validator("degrees", mode="before")
    @classmethod
    def quarter_turns(cls, value):
        if value is not None and (not isinstance(value, list) or not value or len(value) > 3 or any(type(item) is not int or item not in {90, 180, 270} for item in value) or len(set(value)) != len(value)):
            raise ValueError("旋转角度只能从 90、180、270 度中选择，且不能重复")
        return value

    @model_validator(mode="after")
    def operation_parameters(self):
        allowed = {
            "rotate90": {"degrees"}, "brightness": {"min_factor", "max_factor"},
            "contrast": {"min_factor", "max_factor"}, "saturation": {"min_factor", "max_factor"},
            "blur": {"radius"}, "noise": {"std"}, "gamma": {"min_gamma", "max_gamma"},
            "hue": {"min_shift", "max_shift"}, "sharpness": {"min_factor", "max_factor"},
            "autocontrast": {"cutoff"}, "jpeg_compression": {"min_quality", "max_quality"},
            "downscale": {"min_scale", "max_scale"},
        }.get(self.type, set())
        for name in set(type(self).model_fields) - {"type", "probability"} - allowed:
            if name in self.model_fields_set:
                raise ValueError(f"{self.type} 不支持参数 {name}")
        if self.type == "rotate90": self.degrees = self.degrees or [90, 180, 270]
        if "min_factor" in allowed:
            self.min_factor = self.min_factor if self.min_factor is not None else (.5 if self.type == "sharpness" else .8)
            self.max_factor = self.max_factor if self.max_factor is not None else (1.5 if self.type == "sharpness" else 1.2)
            if self.type != "sharpness" and not (.1 <= self.min_factor <= 2 and .1 <= self.max_factor <= 2):
                raise ValueError("亮度、对比度和饱和度系数必须在 0.1–2 之间")
            if self.min_factor > self.max_factor:
                raise ValueError("增强强度下限不能大于上限")
        for minimum, maximum, low, high in (
            ("min_gamma", "max_gamma", .8, 1.2),
            ("min_shift", "max_shift", -.05, .05),
            ("min_quality", "max_quality", 60, 95),
            ("min_scale", "max_scale", .5, .9),
        ):
            if minimum not in allowed: continue
            if getattr(self, minimum) is None: setattr(self, minimum, low)
            if getattr(self, maximum) is None: setattr(self, maximum, high)
            if getattr(self, minimum) > getattr(self, maximum):
                raise ValueError("增强参数下限不能大于上限")
        if self.type == "blur" and self.radius is None: self.radius = 1.5
        if self.type == "noise" and self.std is None: self.std = 10.0
        if self.type == "autocontrast" and self.cutoff is None: self.cutoff = 1.0
        return self


class DatasetAugmentation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed: int = Field(default=42, ge=0, le=2_147_483_647, strict=True)
    copies_per_image: int = Field(default=1, ge=1, le=20, strict=True)
    target_count: int | None = Field(default=None, ge=1, le=5000, strict=True)
    operations: list[AugmentationOperation] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def unique_operations(self):
        if len({item.type for item in self.operations}) != len(self.operations):
            raise ValueError("同一种增强方式只能配置一次")
        return self


class DatasetVersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=100)
    description: str = Field(default="", max_length=1000)
    approved_only: bool = True
    split_seed: int = Field(default=42, ge=0, le=2_147_483_647)
    train_ratio: float = Field(default=0.7, ge=0, le=1)
    valid_ratio: float = Field(default=0.2, ge=0, le=1)
    test_ratio: float = Field(default=0.1, ge=0, le=1)
    augmentation: DatasetAugmentation | None = None
    expected_source_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("name")
    @classmethod
    def clean_version_name(cls, value: str | None) -> str | None:
        value = value.strip() if value else None
        return value or None

    @model_validator(mode="after")
    def validate_split_ratios(self) -> "DatasetVersionCreate":
        total = self.train_ratio + self.valid_ratio + self.test_ratio
        if abs(total - 1.0) > 1e-6:
            raise ValueError("train_ratio + valid_ratio + test_ratio must equal 1")
        if self.train_ratio <= 0:
            raise ValueError("train_ratio must be greater than 0")
        return self


class DatasetVersionPreview(DatasetVersionCreate):
    asset_id: int | None = Field(default=None, ge=1, strict=True)


class TrainingRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: int = Field(ge=1)
    name: str | None = Field(default=None, max_length=100)
    model_arch: str = Field(default="yolo26n.pt", min_length=1, max_length=200)
    task: Literal["auto", "detect", "segment", "classify", "pose"] = "auto"
    epochs: int = Field(default=50, ge=1, le=10_000)
    image_size: int = Field(default=640, ge=32, le=4096)
    batch_size: int = Field(default=16, ge=-1, le=4096)
    device: str = Field(default="auto", min_length=1, max_length=50)
    resource_profile: Literal["balanced", "performance", "low_memory"] = "balanced"
    workers: int = Field(default=4, ge=0, le=64)
    patience: int = Field(default=25, ge=0, le=10_000)
    optimizer: Literal[
        "auto", "SGD", "Adam", "AdamW", "NAdam", "RAdam", "RMSProp", "MuSGD"
    ] = "auto"
    lr0: float = Field(default=0.01, gt=0, le=1)
    weight_decay: float = Field(default=0.0005, ge=0, le=1)
    seed: int = Field(default=0, ge=0, le=2_147_483_647)
    deterministic: bool = True
    amp: bool = True
    cache: bool | Literal["ram", "disk"] = False
    save_period: int = Field(default=-1, ge=-1, le=10_000)
    freeze: int | list[int] | None = None
    cos_lr: bool = False
    close_mosaic: int = Field(default=10, ge=0, le=10_000)
    max_retries: int = Field(default=2, ge=0, le=10)

    @field_validator("name", "model_arch", "device")
    @classmethod
    def clean_training_strings(cls, value: str | None) -> str | None:
        return value.strip() if value else value

    @field_validator("model_arch")
    @classmethod
    def validate_model_arch(cls, value: str) -> str:
        from pathlib import Path

        value = value.strip()
        if Path(value).name != value or not value.endswith(".pt"):
            raise ValueError("model_arch must be a .pt filename")
        return value

    @field_validator("device")
    @classmethod
    def validate_device(cls, value: str) -> str:
        value = value.strip().lower()
        if value in {"auto", "cpu", "cuda"}:
            return value
        if value.startswith("cuda:") and value[5:].isdigit():
            return value
        if value.isdigit():
            return f"cuda:{value}"
        raise ValueError("device must be auto, cpu, cuda, cuda:<index>, or a GPU index")

    @field_validator("batch_size")
    @classmethod
    def validate_batch_size(cls, value: int) -> int:
        if value == 0:
            raise ValueError("batch_size must be -1 or greater than 0")
        return value

    @field_validator("save_period")
    @classmethod
    def validate_save_period(cls, value: int) -> int:
        if value == 0:
            raise ValueError("save_period must be -1 or greater than 0")
        return value

    @field_validator("freeze")
    @classmethod
    def validate_freeze(cls, value: int | list[int] | None) -> int | list[int] | None:
        if value is None:
            return value
        if isinstance(value, int):
            if value < 0 or value > 1000:
                raise ValueError("freeze must be between 0 and 1000")
            return value
        if len(value) > 1000 or any(index < 0 or index > 1000 for index in value):
            raise ValueError("freeze layer indices must be between 0 and 1000")
        return list(dict.fromkeys(value))


class TrainingRetryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=100)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        value = value.strip() if value else None
        return value or None


class RKNNConversionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_platform: Literal["rk3588", "rk3576", "rk3566", "rk3568", "rk3562", "rv1103", "rv1106", "rv1126b"] = "rk3588"
    quantized: bool = Field(default=False, strict=True)
    quantized_algorithm: Literal["normal", "mmse", "kl_divergence"] = "normal"
    optimization_level: int = Field(default=3, ge=0, le=3, strict=True)
    mean_values: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0], min_length=3, max_length=3)
    std_values: list[float] = Field(default_factory=lambda: [255.0, 255.0, 255.0], min_length=3, max_length=3)
    input_color: Literal["rgb"] = "rgb"
    calibration_source: Literal["version_train"] = "version_train"
    calibration_count: int = Field(default=100, ge=1, le=1000, strict=True)

    @model_validator(mode="after")
    def supported_precision(self):
        if self.target_platform in {"rv1103", "rv1106"} and not self.quantized:
            raise ValueError("RV1103 / RV1106 仅支持 INT8，请开启量化并选择校准图片数量")
        return self

    @field_validator("mean_values", "std_values")
    @classmethod
    def finite_channels(cls, values: list[float], info):
        import math
        if any(not math.isfinite(value) or abs(value) > 1_000_000 for value in values):
            raise ValueError("均值和标准差必须是三个有限数值，绝对值不超过 1000000")
        if info.field_name == "std_values" and any(value <= 0 for value in values):
            raise ValueError("标准差的三个数值都必须大于 0")
        return values


class ConversionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["torchscript", "onnx", "engine", "rknn"]
    image_size: int = Field(default=640, ge=32, le=4096)
    half: bool = False
    dynamic: bool = False
    simplify: bool = True
    device: str = Field(default="auto", min_length=1, max_length=50)
    max_retries: int = Field(default=2, ge=0, le=10)
    batch: int = Field(default=1, ge=1, le=64, strict=True)
    workspace: float | None = Field(default=None, ge=0.25, le=64, allow_inf_nan=False)
    rknn: RKNNConversionConfig | None = None

    @model_validator(mode="after")
    def conversion_options(self):
        if self.image_size % 32:
            raise ValueError("转换图片尺寸必须是 32 的整数倍")
        if self.workspace is not None and self.format != "engine":
            raise ValueError("workspace 显存上限仅用于 TensorRT 转换")
        if self.format == "rknn":
            if self.rknn is None:
                raise ValueError("RKNN 转换必须指定 rknn 配置和目标芯片")
            if self.half or self.dynamic or self.batch != 1:
                raise ValueError("RKNN 使用静态单张输入：half=false、dynamic=false、batch=1；是否 INT8 由 quantized 设置")
            if self.device not in {"auto", "cpu"}:
                raise ValueError("RKNN 转换在 CPU 上构建，请选择 auto 或 cpu")
        elif self.rknn is not None:
            raise ValueError("rknn 参数仅用于 RKNN 转换")
        if self.format == "engine" and self.device == "cpu":
            raise ValueError("TensorRT 转换需要 CUDA GPU，不能选择 CPU")
        if self.half and self.device == "cpu":
            raise ValueError("FP16 导出需要选择 CUDA GPU")
        return self

    @field_validator("device")
    @classmethod
    def validate_device(cls, value: str) -> str:
        value = value.strip().lower()
        if value in {"auto", "cpu", "cuda"}:
            return value
        if value.startswith("cuda:") and value[5:].isdigit():
            return value
        if value.isdigit():
            return f"cuda:{value}"
        raise ValueError("device must be auto, cpu, cuda, cuda:<index>, or a GPU index")


class ConversionRetryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device: str | None = Field(default=None, min_length=1, max_length=50)

    @field_validator("device")
    @classmethod
    def validate_device(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if value in {"auto", "cpu", "cuda"}:
            return value
        if value.startswith("cuda:") and value[5:].isdigit():
            return value
        if value.isdigit():
            return f"cuda:{value}"
        raise ValueError("device must be auto, cpu, cuda, cuda:<index>, or a GPU index")
