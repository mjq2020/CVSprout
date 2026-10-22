"""Train with fresh architectures, uploaded weights, and optional official downloads.

Run: .venv/bin/python scripts/smoke_training_initialization.py
Add --download-official to exercise a real cold-cache download from the pinned
official source (MDP_DOWNLOAD_PROXY is honored). Everything lives in a temporary
directory; the six synthetic images verify execution, not model accuracy.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-official", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="cvsprout-initialization-") as directory:
        root = Path(directory)
        models = root / "models"
        custom = root / "external-models"
        custom.mkdir()
        (root / "yolo").mkdir()
        os.environ.update(
            MDP_DATA_DIR=str(root / "data"), MDP_MODELS_DIR=str(models),
            MDP_AUTH_MODE="disabled", MDP_EMBEDDED_WORKER="false",
            MDP_EMBEDDED_PIPELINE_WORKER="false", MDP_TRAINING_ENABLED="true",
            MDP_WORKER_URL="", MDP_PUBLIC_ORIGIN="", MDP_COOKIE_SECURE="false",
            MDP_MODEL_DEVICE="cpu", MDP_TRAINING_MODEL_DIRS=str(custom),
            MDP_TRAINING_BASE_MODEL=str(custom / "absent.pt"),
            MDP_TRAINING_SEGMENTATION_MODEL=str(custom / "absent-seg.pt"),
            YOLO_OFFLINE="true", YOLO_AUTOINSTALL="false",
            YOLO_CONFIG_DIR=str(root / "yolo"), OMP_NUM_THREADS="2", MKL_NUM_THREADS="2",
            CUDA_VISIBLE_DEVICES="",
        )
        import torch
        from fastapi.testclient import TestClient
        from PIL import Image
        from ultralytics import YOLO

        from app import training, training_models
        from app.database import one
        from app.main import app

        torch.set_num_threads(2)

        def checked(response, status=200):
            assert response.status_code == status, response.text
            return response.json()

        with TestClient(app) as client:
            catalog = checked(client.get("/api/training/catalog"))["models"]
            for filename in ("yolov8n.pt", "yolo11n.pt", "yolo26n.pt"):
                model = next(item for item in catalog if item["model_arch"] == filename)
                assert model["available"] and not model["installed"], model
                assert model["supports_scratch"], model
            assert not list(models.rglob("*.pt")), "Catalog browsing downloaded weights"
            project = checked(client.post("/api/projects", json={
                "name": "Initialization smoke", "task_type": "detect", "labels": ["object"],
            }), 201)
            label = checked(client.get(f"/api/projects/{project['id']}"))["labels"][0]["id"]
            for index in range(6):
                image = io.BytesIO()
                Image.new("RGB", (64, 64), (30 + index * 25, 60, 120)).save(image, "PNG")
                asset = checked(client.post(f"/api/projects/{project['id']}/assets", files={
                    "files": (f"image-{index}.png", image.getvalue(), "image/png"),
                }), 201)["asset_ids"][0]
                checked(client.post(f"/api/assets/{asset}/annotations", json={
                    "label_id": label, "shape_type": "rectangle", "points": [10, 10, 50, 50],
                }), 201)
            version = checked(client.post(f"/api/projects/{project['id']}/versions", json={
                "approved_only": False, "train_ratio": .67, "valid_ratio": .33, "test_ratio": 0,
            }), 201)

            def create(filename, *, pretrained=None):
                payload = {
                    "version_id": version["id"], "model_arch": filename, "task": "detect",
                    "epochs": 1, "image_size": 64, "batch_size": 2, "workers": 0,
                    "device": "cpu", "amp": False, "close_mosaic": 0,
                }
                if pretrained is not None:
                    payload["pretrained"] = pretrained
                return checked(client.post(f"/api/projects/{project['id']}/training-runs", json=payload), 202)

            def execute(run):
                claim = training._claim_next("training_runs", "initialization-smoke")
                assert claim and claim[0] == run["id"], claim
                training._run_with_lease("training_runs", run["id"], "initialization-smoke", claim[1], training.run_training)
                result = checked(client.get(f"/api/training-runs/{run['id']}"))
                assert result["status"] == "completed", result
                assert result["artifact"]["integrity"] == "verified", result
                assert result["settings"]["pretrained"] == run["settings"]["pretrained"]
                path = root / "data" / "training" / f"run-{run['id']}" / f"attempt-{claim[1]}" / "best.pt"
                checkpoint = YOLO(str(path), verbose=False)
                assert checkpoint.task == "detect" and set(checkpoint.names.values()) == {"object"}
                assert not one("SELECT * FROM pipeline_resource_leases"), "Resource lease leaked"
                return path

            with patch.object(training_models, "ensure_pretrained_model", side_effect=AssertionError("Scratch mode requested pretrained weights")):
                scratch = create("yolo11n.pt", pretrained=False)
                checkpoint_path = execute(scratch)
                assert not list(models.rglob("*.pt")), "Scratch mode installed pretrained weights"
                print("PASS: official architecture → random initialization → CPU training", flush=True)

            with checkpoint_path.open("rb") as handle:
                checked(client.post("/api/model-downloads/import/checkpoint", files={
                    "file": ("uploaded-smoke.pt", handle, "application/octet-stream"),
                }), 201)
            imported = next(item for item in checked(client.get("/api/training/catalog"))["models"]
                            if item["model_arch"] == "uploaded-smoke.pt")
            assert imported["installed"] and imported["available"] and imported["supports_scratch"], imported
            with patch.object(training_models, "ensure_pretrained_model", side_effect=AssertionError("Local weights requested a download")):
                uploaded = create("uploaded-smoke.pt")
                assert uploaded["settings"]["pretrained"] is True, "Pretrained must default to true"
                execute(uploaded)
                print("PASS: upload → catalog (with custom search directory) → pretrained CPU training", flush=True)
                fresh = create("uploaded-smoke.pt", pretrained=False)
                architecture_dir = root / "fresh-check"
                architecture_dir.mkdir()
                rebuilt, _ = training._fresh_training_model(fresh["settings"], None, architecture_dir, "detect")
                original = YOLO(str(checkpoint_path), verbose=False)
                source_weight = next(original.model.parameters()).detach()
                fresh_weight = next(rebuilt.model.parameters()).detach()
                assert source_weight.shape == fresh_weight.shape, "Custom architecture scale changed"
                assert not torch.equal(source_weight, fresh_weight), "Scratch reused checkpoint parameters"
                execute(fresh)
                print("PASS: uploaded architecture → fresh parameters → CPU training", flush=True)

            if args.download_official:
                official = training_models.get_builtin_training_model("yolov8n.pt")
                destination = Path(official["path"])
                assert not destination.exists()
                run = create("yolov8n.pt")
                assert not destination.exists(), "Creating a run downloaded weights synchronously"
                original_download = training_models.ensure_pretrained_model
                calls = []

                def download_before_resource(*arguments, **keywords):
                    assert not one("SELECT * FROM pipeline_resource_leases"), "Download held a compute lease"
                    calls.append(True)
                    return original_download(*arguments, **keywords)

                with patch.object(training_models, "ensure_pretrained_model", side_effect=download_before_resource):
                    execute(run)
                assert calls and destination.is_file()
                assert training._file_sha256(destination) == official["sha256"]
                print("PASS: create without download → verified official download before compute lease → CPU training", flush=True)


if __name__ == "__main__":
    main()
