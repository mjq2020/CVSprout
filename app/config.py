from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
MODEL_ROOT = Path(os.getenv("MDP_MODELS_DIR", BASE_DIR / "models"))


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.getenv("MDP_DATA_DIR", BASE_DIR / "data"))
    models_dir: Path = Path(os.getenv("MDP_MODELS_DIR", BASE_DIR / "models"))
    model_device: str = os.getenv("MDP_MODEL_DEVICE", "auto").strip().lower()
    sam3_checkpoint: Path = Path(
        os.getenv("MDP_SAM3_CHECKPOINT", MODEL_ROOT / "sam3" / "sam3.pt")
    )
    sam3_source_dir: Path = Path(
        os.getenv("MDP_SAM3_SOURCE_DIR", MODEL_ROOT / "sam3" / "source")
    )
    max_upload_mb: int = int(os.getenv("MDP_MAX_UPLOAD_MB", "250"))
    max_image_pixels: int = int(os.getenv("MDP_MAX_IMAGE_PIXELS", "50000000"))
    max_assets_per_upload: int = int(os.getenv("MDP_MAX_ASSETS_PER_UPLOAD", "10000"))
    qwen_base_url: str = os.getenv("QWEN_BASE_URL", "").rstrip("/")
    qwen_api_key: str = os.getenv("QWEN_API_KEY", "")
    qwen_model: str = os.getenv("QWEN_MODEL", "Qwen/Qwen3.5-35B-A3B")
    grounding_dino_endpoint: str = os.getenv("GROUNDING_DINO_ENDPOINT", "")
    sam3_endpoint: str = os.getenv("SAM3_ENDPOINT", "")
    sam2_endpoint: str = os.getenv("SAM2_ENDPOINT", "")
    enable_demo_model: bool = os.getenv("MDP_ENABLE_DEMO_MODEL", "false").lower() == "true"
    embedded_worker: bool = os.getenv("MDP_EMBEDDED_WORKER", "true").lower() == "true"
    worker_poll_seconds: float = float(os.getenv("MDP_WORKER_POLL_SECONDS", "0.5"))
    job_lease_seconds: int = int(os.getenv("MDP_JOB_LEASE_SECONDS", "120"))
    asset_lock_seconds: int = int(os.getenv("MDP_ASSET_LOCK_SECONDS", "120"))
    auth_mode: str = os.getenv("MDP_AUTH_MODE", "accounts").lower()
    bootstrap_api_key: str = os.getenv("MDP_BOOTSTRAP_API_KEY", "")
    cookie_secure: bool = os.getenv("MDP_COOKIE_SECURE", "false").lower() == "true"
    public_origin: str = os.getenv("MDP_PUBLIC_ORIGIN", "").strip().rstrip("/")
    training_enabled: bool = os.getenv("MDP_TRAINING_ENABLED", "true").lower() == "true"
    embedded_pipeline_worker: bool = (
        os.getenv("MDP_EMBEDDED_PIPELINE_WORKER", "true").lower() == "true"
    )
    training_poll_seconds: float = float(os.getenv("MDP_TRAINING_POLL_SECONDS", "1.0"))
    pipeline_lease_seconds: int = int(os.getenv("MDP_PIPELINE_LEASE_SECONDS", "180"))
    # Keep the RKNN SDK's dependency stack separate from the training environment.
    rknn_python: str = os.getenv("MDP_RKNN_PYTHON", "").strip()
    worker_url: str = os.getenv("MDP_WORKER_URL", "").rstrip("/")
    worker_token: str = os.getenv("MDP_WORKER_TOKEN", "")
    release_gpu_after_inference: bool = os.getenv("MDP_RELEASE_GPU_AFTER_INFERENCE", "true").lower() == "true"
    training_base_model: Path = Path(
        os.getenv("MDP_TRAINING_BASE_MODEL", MODEL_ROOT / "training" / "yolo26n.pt")
    )
    training_segmentation_model: Path = Path(
        os.getenv(
            "MDP_TRAINING_SEGMENTATION_MODEL",
            MODEL_ROOT / "training" / "yolo26n-seg.pt",
        )
    )
    training_model_dirs_raw: str = os.getenv(
        "MDP_TRAINING_MODEL_DIRS",
        str(MODEL_ROOT / "training"),
    )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mdp.sqlite3"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def thumbnail_dir(self) -> Path:
        return self.data_dir / "thumbnails"

    @property
    def versions_dir(self) -> Path:
        return self.data_dir / "versions"

    @property
    def training_dir(self) -> Path:
        return self.data_dir / "training"

    @property
    def training_model_dirs(self) -> tuple[Path, ...]:
        """Administrator-controlled directories scanned for Ultralytics checkpoints.

        Only direct ``*.pt`` children are considered. Keeping this list explicit avoids
        accidentally loading arbitrary pickle files from upload or dataset directories.
        """

        paths: list[Path] = []
        for value in self.training_model_dirs_raw.split(os.pathsep):
            value = value.strip()
            if value:
                path = Path(value)
                if path not in paths:
                    paths.append(path)
        for configured in (self.training_base_model, self.training_segmentation_model):
            if configured.parent not in paths:
                paths.append(configured.parent)
        return tuple(paths)

    def validate(self) -> None:
        if self.public_origin:
            from urllib.parse import urlsplit
            try:
                public = urlsplit(self.public_origin)
                public.port  # Validate malformed and out-of-range proxy ports.
            except ValueError as exc:
                raise RuntimeError("MDP_PUBLIC_ORIGIN contains an invalid host or port") from exc
            if public.scheme not in {"http", "https"} or not public.hostname or public.username or public.password or public.path or public.query or public.fragment:
                raise RuntimeError("MDP_PUBLIC_ORIGIN must be an HTTP(S) origin without a path")
        if self.auth_mode not in {"disabled", "token", "accounts"}:
            raise RuntimeError("MDP_AUTH_MODE must be disabled, token, or accounts")
        if self.auth_mode == "token" and len(self.bootstrap_api_key) < 32:
            raise RuntimeError("MDP_BOOTSTRAP_API_KEY must contain at least 32 characters")
        if self.auth_mode == "accounts" and self.bootstrap_api_key and len(self.bootstrap_api_key) < 32:
            raise RuntimeError("MDP_BOOTSTRAP_API_KEY must contain at least 32 characters when set")
        if self.max_upload_mb <= 0 or self.max_image_pixels <= 0 or self.max_assets_per_upload <= 0:
            raise RuntimeError("upload resource limits must be positive")
        if self.worker_poll_seconds <= 0 or self.job_lease_seconds < 3 or self.asset_lock_seconds < 3:
            raise RuntimeError("worker polling and lease settings are invalid")
        if self.training_poll_seconds <= 0:
            raise RuntimeError("MDP_TRAINING_POLL_SECONDS must be positive")
        if self.pipeline_lease_seconds < 30:
            raise RuntimeError("MDP_PIPELINE_LEASE_SECONDS must be at least 30")
        if not self.training_model_dirs:
            raise RuntimeError("MDP_TRAINING_MODEL_DIRS must contain at least one directory")
        if self.model_device != "auto" and self.model_device != "cpu" and not self.model_device.startswith("cuda"):
            raise RuntimeError("MDP_MODEL_DEVICE must be auto, cpu, cuda, or cuda:<index>")


settings = Settings()
