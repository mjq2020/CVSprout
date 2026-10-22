"""Exercise real CPU training and export without touching project data or downloading weights.

Run: .venv/bin/python scripts/smoke_pipeline.py
The tiny synthetic dataset checks execution, not model quality.
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="mdp-pipeline-smoke-") as directory:
        root = Path(directory)
        models = root / "models"
        models.mkdir()
        (root / "yolo").mkdir()
        os.environ.update(
            MDP_DATA_DIR=str(root / "data"), MDP_MODELS_DIR=str(models), MDP_AUTH_MODE="disabled",
            MDP_EMBEDDED_WORKER="false", MDP_EMBEDDED_PIPELINE_WORKER="false",
            MDP_WORKER_URL="", MDP_PUBLIC_ORIGIN="", MDP_COOKIE_SECURE="false",
            MDP_TRAINING_ENABLED="true", MDP_MODEL_DEVICE="cpu",
            MDP_TRAINING_MODEL_DIRS=str(models),
            MDP_TRAINING_BASE_MODEL=str(models / "absent.pt"),
            MDP_TRAINING_SEGMENTATION_MODEL=str(models / "absent-seg.pt"),
            YOLO_OFFLINE="true", YOLO_AUTOINSTALL="false",
            YOLO_CONFIG_DIR=str(root / "yolo"), OMP_NUM_THREADS="2", MKL_NUM_THREADS="2",
            CUDA_VISIBLE_DEVICES="",
        )
        import torch
        from fastapi.testclient import TestClient
        from PIL import Image
        from ultralytics import YOLO

        from app import training
        from app.main import app

        torch.set_num_threads(2)
        tasks = [("classify", "cls"), ("pose", "pose")]
        for _, suffix in tasks:
            model = YOLO(f"yolo26n-{suffix}.yaml", verbose=False)
            model.save(str(models / f"yolo26n-{suffix}.pt"))

        def checked(response, status=200):
            assert response.status_code == status, response.text
            return response.json()

        with TestClient(app) as client:
            for task, suffix in tasks:
                payload = {"name": f"Smoke {task}", "task_type": task, "labels": ["object", "other"]}
                if task == "pose":
                    payload["keypoint_schema"] = {"names": ["tip", "base"], "skeleton": [[0, 1]]}
                project = checked(client.post("/api/projects", json=payload), 201)
                labels = checked(client.get(f"/api/projects/{project['id']}"))["labels"]
                for index in range(6):
                    image = io.BytesIO()
                    Image.new("RGB", (64, 64), (30 + index * 25, 60, 120)).save(image, "PNG")
                    asset = checked(client.post(
                        f"/api/projects/{project['id']}/assets",
                        files={"files": ("image.png", image.getvalue(), "image/png")},
                    ), 201)["asset_ids"][0]
                    shape = {"shape_type": "classification"}
                    if task == "pose":
                        shape = {"shape_type": "keypoints", "points": [10, 10, 50, 50],
                                 "keypoints": [{"x": 20, "y": 20, "visibility": 2},
                                               {"x": 40, "y": 40, "visibility": 2}]}
                    checked(client.post(f"/api/assets/{asset}/annotations", json={
                        "label_id": labels[index % 2]["id"], **shape,
                    }), 201)
                version = checked(client.post(f"/api/projects/{project['id']}/versions", json={
                    "approved_only": False, "train_ratio": .67, "valid_ratio": .33, "test_ratio": 0,
                }), 201)
                run = checked(client.post(f"/api/projects/{project['id']}/training-runs", json={
                    "version_id": version["id"], "model_arch": f"yolo26n-{suffix}.pt", "task": task,
                    "epochs": 1, "image_size": 64, "batch_size": 2, "workers": 0,
                    "device": "cpu", "amp": False, "close_mosaic": 0,
                }), 202)["id"]
                claim = training._claim_next("training_runs", "smoke")
                assert claim and claim[0] == run
                training._run_with_lease("training_runs", run, "smoke", claim[1], training.run_training)
                result = checked(client.get(f"/api/training-runs/{run}"))
                assert result["status"] == "completed", result
                assert result["artifact"]["integrity"] == "verified", result
                checkpoint = YOLO(str(root / "data" / "training" / f"run-{run}" / f"attempt-{claim[1]}" / "best.pt"))
                assert set(checkpoint.names.values()) == {"object", "other"}, checkpoint.names
                for export_format in ("torchscript", "onnx"):
                    conversion = checked(client.post(f"/api/training-runs/{run}/conversions", json={
                        "format": export_format, "image_size": 64, "device": "cpu", "simplify": False,
                    }), 202)["id"]
                    claim = training._claim_next("model_conversions", "smoke")
                    assert claim and claim[0] == conversion
                    training._run_with_lease("model_conversions", conversion, "smoke", claim[1], training.run_conversion)
                    result = checked(client.get(f"/api/conversions/{conversion}"))
                    assert result["status"] == "completed", result
                    assert client.get(f"/api/conversions/{conversion}/artifact").status_code == 200
                    print(f"PASS: {task} training → {export_format} → download", flush=True)


if __name__ == "__main__":
    main()
