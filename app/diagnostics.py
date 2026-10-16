"""Container-side checks; a real tensor operation verifies usable CUDA."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import shutil

from .config import settings


def inspect(require_gpu=False):
    checks = []
    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
    for path in (settings.data_dir, settings.models_dir):
        try:
            path.mkdir(parents=True, exist_ok=True)
            import tempfile
            with tempfile.TemporaryFile(dir=path) as stream:
                stream.write(b"check")
            free = shutil.disk_usage(path).free
            add(str(path), free > 1024**3, f"可用空间 {free / 1024**3:.1f} GiB；建议至少保留 1 GiB")
        except OSError as exc:
            add(str(path), False, str(exc))
    try:
        import torch
        add("PyTorch", True, torch.__version__)
        add("Ultralytics", True, importlib.metadata.version("ultralytics"))
        available = torch.cuda.is_available()
        if require_gpu or available:
            if not available:
                add("CUDA", False, "容器内无法使用 GPU。检查 NVIDIA 驱动、Container Toolkit；Windows 检查 WSL2 后端。")
            for index in range(torch.cuda.device_count() if available else 0):
                try:
                    x = torch.ones((16, 16), device=f"cuda:{index}")
                    value = (x @ x).sum().item()
                    torch.cuda.synchronize(index)
                    add(f"GPU {index}", value == 4096, torch.cuda.get_device_name(index))
                except Exception as exc:
                    add(f"GPU {index}", False, str(exc))
        else:
            add("CPU 模式", True, "可进行人工标注和小规模训练；不会使用 NVIDIA GPU。")
    except Exception as exc:
        add("模型运行环境", False, str(exc))
    return {"ok": all(check["ok"] for check in checks), "checks": checks}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--gpu", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = inspect(args.gpu)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        for check in result["checks"]:
            print(f"{'✓' if check['ok'] else '✗'} {check['name']}: {check['detail']}")
    raise SystemExit(0 if result["ok"] else 1)
