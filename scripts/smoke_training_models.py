"""Train representative catalog models on isolated synthetic CPU datasets.

Default: random initialization, no pretrained downloads. --pretrained exercises
the pinned official download path instead. --models accepts catalog filenames.
This checks execution and artifacts, not accuracy or GPU memory requirements.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", default=[
        "yolo26m.pt", "yolov8s-seg.pt", "yolo11s-cls.pt", "yolo26s-pose.pt",
    ])
    parser.add_argument("--pretrained", action="store_true")
    parser.add_argument("--exports", nargs="*", choices=["onnx", "torchscript"], default=[])
    parser.add_argument("--result", type=Path)
    args = parser.parse_args()
    results = []
    with tempfile.TemporaryDirectory(prefix="cvsprout-model-matrix-") as directory:
        root = Path(directory)
        models = root / "models"
        (root / "yolo").mkdir()
        os.environ.update(
            MDP_DATA_DIR=str(root / "data"), MDP_MODELS_DIR=str(models),
            MDP_AUTH_MODE="disabled", MDP_EMBEDDED_WORKER="false",
            MDP_EMBEDDED_PIPELINE_WORKER="false", MDP_TRAINING_ENABLED="true",
            MDP_WORKER_URL="", MDP_PUBLIC_ORIGIN="", MDP_COOKIE_SECURE="false",
            MDP_MODEL_DEVICE="cpu", MDP_TRAINING_MODEL_DIRS=str(models / "training"),
            MDP_TRAINING_BASE_MODEL=str(models / "training" / "absent.pt"),
            MDP_TRAINING_SEGMENTATION_MODEL=str(models / "training" / "absent-seg.pt"),
            YOLO_OFFLINE="true", YOLO_AUTOINSTALL="false", CUDA_VISIBLE_DEVICES="",
            YOLO_CONFIG_DIR=str(root / "yolo"), OMP_NUM_THREADS="2", MKL_NUM_THREADS="2",
        )
        import torch
        from fastapi.testclient import TestClient
        from PIL import Image, ImageDraw
        from ultralytics import YOLO

        from app import training, training_models
        from app.database import one
        from app.main import app

        torch.set_num_threads(2)

        def checked(response, status=200):
            assert response.status_code == status, response.text
            return response.json()

        with TestClient(app) as client:
            catalog = {item["model_arch"]: item for item in checked(client.get("/api/training/catalog"))["models"]}
            versions = {}

            def dataset(task):
                if task in versions:
                    return versions[task]
                body = {"name": f"Model matrix {task}", "task_type": task, "labels": ["object", "other"]}
                if task == "pose":
                    body["keypoint_schema"] = {"names": ["tip", "base"], "skeleton": [[0, 1]]}
                project = checked(client.post("/api/projects", json=body), 201)
                labels = checked(client.get(f"/api/projects/{project['id']}"))["labels"]
                for index in range(6):
                    image = Image.new("RGB", (128, 128), (30 + index * 25, 60, 120))
                    ImageDraw.Draw(image).rectangle([24, 24, 96, 96], fill=(200, 170, 40))
                    output = io.BytesIO()
                    image.save(output, "PNG")
                    asset = checked(client.post(f"/api/projects/{project['id']}/assets", files={
                        "files": (f"image-{index}.png", output.getvalue(), "image/png"),
                    }), 201)["asset_ids"][0]
                    shape = {"shape_type": "rectangle", "points": [24, 24, 96, 96]}
                    if task == "segment":
                        shape = {"shape_type": "polygon", "points": [24, 24, 96, 24, 96, 96, 24, 96]}
                    elif task == "classify":
                        shape = {"shape_type": "classification"}
                    elif task == "pose":
                        shape = {"shape_type": "keypoints", "points": [24, 24, 96, 96], "keypoints": [
                            {"x": 40, "y": 40, "visibility": 2}, {"x": 80, "y": 80, "visibility": 2},
                        ]}
                    checked(client.post(f"/api/assets/{asset}/annotations", json={"label_id": labels[index % 2]["id"], **shape}), 201)
                version = checked(client.post(f"/api/projects/{project['id']}/versions", json={
                    "approved_only": False, "train_ratio": .67, "valid_ratio": .33, "test_ratio": 0,
                }), 201)
                versions[task] = (project["id"], version["id"])
                return versions[task]

            for filename in args.models:
                assert filename in catalog, f"Unknown catalog model: {filename}"
                model = catalog[filename]
                assert model["available"] and (args.pretrained or model["supports_scratch"]), model
                task = model["task"]
                project_id, version_id = dataset(task)
                rtdetr = model["family"] == "RT-DETR"
                run = checked(client.post(f"/api/projects/{project_id}/training-runs", json={
                    "version_id": version_id, "model_arch": filename, "task": task,
                    "pretrained": args.pretrained, "epochs": 1, "image_size": 256 if rtdetr else 128,
                    "batch_size": 2, "workers": 0, "device": "cpu", "amp": False,
                    "deterministic": not rtdetr, "close_mosaic": 0,
                }), 202)
                claim = training._claim_next("training_runs", "matrix-smoke")
                assert claim and claim[0] == run["id"], claim
                if args.pretrained:
                    training._run_with_lease("training_runs", run["id"], "matrix-smoke", claim[1], training.run_training)
                else:
                    with patch.object(training_models, "ensure_pretrained_model", side_effect=AssertionError("Scratch downloaded pretrained weights")):
                        training._run_with_lease("training_runs", run["id"], "matrix-smoke", claim[1], training.run_training)
                result = checked(client.get(f"/api/training-runs/{run['id']}"))
                assert result["status"] == "completed", result
                assert result["artifact"]["integrity"] == "verified", result
                path = root / "data" / "training" / f"run-{run['id']}" / f"attempt-{claim[1]}" / "best.pt"
                checkpoint = YOLO(str(path), verbose=False)
                assert checkpoint.task == task and set(checkpoint.names.values()) == {"object", "other"}
                assert not one("SELECT * FROM pipeline_resource_leases"), "Resource lease leaked"
                if not args.pretrained:
                    assert not list(models.rglob("*.pt")), "Scratch installed pretrained weights"
                results.append({"model": filename, "task": task, "pretrained": args.pretrained,
                                "trainer": checkpoint._smart_load("trainer").__name__, "artifact": "verified"})
                print(f"PASS: {filename} → {task} CPU training → verified artifact", flush=True)
                for export_format in args.exports:
                    size = 256 if rtdetr else 128
                    conversion = checked(client.post(f"/api/training-runs/{run['id']}/conversions", json={
                        "format": export_format, "image_size": size, "device": "cpu", "simplify": False,
                    }), 202)
                    export_claim = training._claim_next("model_conversions", "matrix-smoke")
                    assert export_claim and export_claim[0] == conversion["id"]
                    training._run_with_lease("model_conversions", conversion["id"], "matrix-smoke", export_claim[1], training.run_conversion)
                    converted = checked(client.get(f"/api/conversions/{conversion['id']}"))
                    assert converted["status"] == "completed", converted
                    assert converted["artifact"]["integrity"] == "verified", converted
                    deployment = converted["artifact"]["deployment"]
                    if rtdetr:
                        assert deployment["preprocessing"]["preserve_aspect_ratio"] is False, deployment
                        assert deployment["postprocessing"]["external_nms_required"] is False, deployment
                        assert deployment["postprocessing"]["box_format"] == "xywh", deployment
                    downloaded = client.get(f"/api/conversions/{conversion['id']}/artifact")
                    assert downloaded.status_code == 200
                    exported = root / f"export-{conversion['id']}.{export_format}"
                    exported.write_bytes(downloaded.content)
                    if rtdetr:
                        import numpy as np
                        # A fixed textured input tests numerical fidelity
                        # reproducibly without relying on a blank image.
                        inputs = torch.rand(1, 3, size, size, generator=torch.Generator().manual_seed(42))
                        checkpoint.model.eval()
                        with torch.inference_mode():
                            expected = checkpoint.model(inputs)[0].numpy()
                            if export_format == "torchscript":
                                actual = torch.jit.load(str(exported))(inputs).numpy()
                            else:
                                import onnxruntime as ort
                                options = ort.SessionOptions()
                                options.intra_op_num_threads = 2
                                options.inter_op_num_threads = 1
                                session = ort.InferenceSession(str(exported), options, providers=["CPUExecutionProvider"])
                                actual = session.run(None, {session.get_inputs()[0].name: inputs.numpy()})[0]
                                del session
                        assert actual.shape == expected.shape == (1, 300, 6), actual.shape
                        assert np.isfinite(actual).all()
                        assert ((actual[..., :4] >= 0) & (actual[..., :4] <= 1)).all()
                        # RT-DETR queries form a set: TopK may swap near-tied
                        # candidates after layer fusion. Require a bijection of
                        # ALL rows and ALL columns at the original tolerance.
                        from scipy.optimize import linear_sum_assignment
                        close = np.all(np.isclose(actual[0, :, None, :], expected[0, None, :, :],
                                                  rtol=1e-3, atol=1e-4), axis=-1)
                        rows, columns = linear_sum_assignment((~close).astype(np.int8))
                        if not close[rows, columns].all() and args.result:
                            np.savez(args.result.with_suffix(f".{export_format}.npz"),
                                     inputs=inputs.numpy(), actual=actual, expected=expected)
                        assert close[rows, columns].all(), "Export changed the query set beyond numerical tolerance"
                        np.testing.assert_allclose(actual[0, rows], expected[0, columns], rtol=1e-3, atol=1e-4)
                        results[-1].setdefault("runtime_checks", {})[export_format] = {
                            "queries": len(rows), "reordered_queries": int(np.count_nonzero(rows != columns)),
                            "max_aligned_difference": float(np.max(np.abs(actual[0, rows] - expected[0, columns]))),
                        }
                    results[-1].setdefault("exports", []).append(export_format)
                    print(f"PASS: {filename} → {export_format} → download and runtime output", flush=True)
                del checkpoint
    if args.result:
        args.result.write_text(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
