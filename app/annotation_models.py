"""Server-owned preparation plans for built-in annotation models."""
from pathlib import Path

from .config import settings


DOWNLOADABLE = {
    "qwen3.5-0.8b": ("local_qwen", "qwen3.5-0.8b"),
    "qwen3.5-35b-a3b": ("local_qwen", "qwen3.5-35b-a3b"),
    "grounding-dino": ("local_grounding_dino", "grounding-dino-tiny"),
}


def nonempty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except (OSError, RuntimeError):
        return False


def sam2_model_path() -> Path:
    primary = settings.sam2_checkpoint
    try:
        if primary.exists():
            return primary
        if primary.name == "sam2.1_t.pt":
            alternative = primary.with_name("sam2.1_hiera_tiny.pt")
            if alternative.is_file():
                return alternative
    except (OSError, RuntimeError):
        # Keep the configured location so its status can explain the error.
        return primary
    legacy = settings.models_dir / "sam2.1-hiera-tiny"
    if nonempty_file(legacy / "model.safetensors"):
        return legacy
    return primary


def preparation(model: dict) -> dict | None:
    """Checking state never starts downloads or deserializes checkpoints."""
    from . import model_downloads

    match = DOWNLOADABLE.get(model["id"])
    if match and model["provider_type"] == match[0]:
        entry = next((item for item in model_downloads.catalog() if item["id"] == match[1]), None)
        if entry is None:
            return None
        # An administrator-managed path must never trigger downloads elsewhere.
        directory = settings.models_dir / Path(entry["files"][0]["path"]).parent
        try:
            matches_directory = Path(model["model_name"]).resolve() == directory.resolve()
        except (OSError, RuntimeError):
            return None
        if not matches_directory:
            return None
        files = [Path(item["path"]).name for item in entry["files"]]
        missing = [name for name in files if not nonempty_file(directory / name)]
        state = model_downloads.download_status(entry)
        return {"mode": "download", "download_id": entry["id"],
                "downloadable": bool(model["enabled"]), "installed": not missing,
                "directory": str(directory), "files": files, "missing_files": missing,
                "instructions": "选择后会在后台下载并校验权重；也可点击下载提前准备。完成后可开始预览或标注。若已有文件损坏，请管理员移走错误文件后重新下载。" +
                    (" 35B-A3B 的全部专家权重都需加载；当前本地实现使用单设备，不自动量化或跨 GPU 分片。" if model["id"] == "qwen3.5-35b-a3b" else ""),
                "source_url": entry["source"], "size_bytes": sum(item["size"] for item in entry["files"]),
                "state": state}
    if model["provider_type"] in {"local_sam2", "local_sam3"}:
        path = Path(model["model_name"])
        try:
            legacy = model["provider_type"] == "local_sam2" and path.is_dir()
        except (OSError, RuntimeError):
            legacy = False
        files = ["config.json", "model.safetensors", "preprocessor_config.json", "processor_config.json"] if legacy else [path.name]
        directory = path if legacy else path.parent
        missing = [name for name in files if not nonempty_file(directory / name)]
        is_sam3 = model["provider_type"] == "local_sam3"
        instruction = ("请先在官方页面申请并获得访问权限，自行下载 sam3.pt，放到此目录后点击重新检查。" if is_sam3 else
                       "请自行下载 SAM 2.1 Tiny 权重，按此文件名放到目录后点击重新检查；也兼容 sam2.1_hiera_tiny.pt 和原有完整 Hugging Face 目录。")
        return {"mode": "manual", "download_id": "sam3" if is_sam3 else "sam2.1-hiera-tiny",
                "downloadable": False, "installed": not missing,
                "directory": str(directory), "files": files, "missing_files": missing,
                "instructions": instruction + " 本项目不会自动下载 SAM 权重；识别文件后还会检查推理依赖。",
                "source_url": "https://huggingface.co/facebook/sam3" if is_sam3 else "https://github.com/facebookresearch/sam2",
                "size_bytes": 0, "state": {"status": "complete" if not missing else "missing", "error": ""}}
    return None
