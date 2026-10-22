"""Validate every built-in training architecture without downloading weights.

Run with the worker environment. Optionally pass --release-metadata pointing to
the JSON response from the official GitHub release API:
https://api.github.com/repos/ultralytics/assets/releases/tags/v8.4.0
The checker itself never fetches metadata or model files.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-metadata", type=Path)
    args = parser.parse_args()
    from app.training_models import builtin_training_models

    models = builtin_training_models()
    assert len({model["id"] for model in models}) == len(models), "Duplicate model IDs"
    assets = None
    if args.release_metadata:
        release = json.loads(args.release_metadata.read_text())
        assert release["tag_name"] == "v8.4.0", "Unexpected official release"
        assets = {asset["name"]: asset for asset in release["assets"]}

    with tempfile.TemporaryDirectory(prefix="cvsprout-catalog-check-") as directory:
        os.environ.update(YOLO_OFFLINE="true", YOLO_AUTOINSTALL="false", YOLO_CONFIG_DIR=directory)
        from ultralytics.nn.tasks import guess_model_task, yaml_model_load

        for model in models:
            name = model["id"]
            config = yaml_model_load(model["architecture"])
            assert config.get("backbone") and config.get("head"), f"Missing architecture: {name}"
            head = config["head"][-1][2]
            # Ultralytics 8.4.38 does not infer RT-DETR's task from a YAML dict.
            task = "detect" if head == "RTDETRDecoder" else guess_model_task(config)
            assert task == model["task"], f"Task mismatch: {name} ({task})"
            if config.get("scales"):
                scale = config.get("scale")
                # YOLOv10b has one explicit scale; the parser selects it when
                # filename inference (which only recognizes n/s/m/l/x) is empty.
                if not scale and len(config["scales"]) == 1:
                    scale = next(iter(config["scales"]))
                assert scale in config["scales"], f"Unresolved model scale: {name} ({scale})"
            if assets is not None:
                asset = assets[name]
                assert asset["browser_download_url"] == model["url"], f"Source mismatch: {name}"
                assert asset["size"] == model["size_bytes"], f"Size mismatch: {name}"
                assert asset.get("digest") == "sha256:" + model["sha256"], f"Digest mismatch: {name}"
                assert model["revision"] == release["tag_name"], f"Revision mismatch: {name}"

    print(json.dumps({"models": len(models), "tasks": dict(Counter(model["task"] for model in models)),
                      "architectures": "passed", "official_metadata": "passed" if assets is not None else "not supplied"},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
