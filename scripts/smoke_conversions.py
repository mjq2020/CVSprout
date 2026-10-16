"""Compile synthetic, untrained YOLO checkpoints without touching project data.

Example: .venv/bin/python scripts/smoke_conversions.py --formats rknn engine --device cuda:0
Use CUDA_VISIBLE_DEVICES to choose a physical GPU. This checks conversion, not accuracy.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, help="Keep isolated fixtures and generated artifacts here")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--formats", nargs="+", choices=["rknn", "engine", "onnx", "torchscript"], default=["rknn"])
    parser.add_argument("--tasks", nargs="+", choices=["detect", "segment", "classify", "pose"], default=["detect"])
    parser.add_argument("--target", default="rv1126b")
    parser.add_argument("--quantized", action="store_true")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--dynamic", action="store_true", help="Check dynamic ONNX / TensorRT input profiles")
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--image-size", type=int, default=64)
    parser.add_argument("--simplify", action="store_true")
    args = parser.parse_args()
    temporary = tempfile.TemporaryDirectory(prefix="mdp-conversion-smoke-") if not args.directory else None
    root = args.directory.resolve() if args.directory else Path(temporary.name)
    root.mkdir(parents=True, exist_ok=True)
    models = root / "models"; models.mkdir(exist_ok=True)
    os.environ.update(
        MDP_DATA_DIR=str(root / "data"), MDP_AUTH_MODE="disabled",
        MDP_EMBEDDED_WORKER="false", MDP_EMBEDDED_PIPELINE_WORKER="false",
        MDP_TRAINING_ENABLED="true", MDP_MODEL_DEVICE="cpu", MDP_MODELS_DIR=str(models),
        MDP_TRAINING_MODEL_DIRS=str(models), MDP_TRAINING_BASE_MODEL=str(models / "absent.pt"),
        MDP_TRAINING_SEGMENTATION_MODEL=str(models / "absent-seg.pt"),
        YOLO_OFFLINE="true", YOLO_AUTOINSTALL="false", YOLO_CONFIG_DIR=str(root / "yolo"),
        OMP_NUM_THREADS="2", MKL_NUM_THREADS="2", MPLBACKEND="Agg",
    )
    import torch
    import yaml
    import ultralytics
    from ultralytics import YOLO
    from PIL import Image, ImageDraw
    from fastapi.testclient import TestClient
    from app.main import app
    from app import training
    from app.database import db, now_iso

    torch.set_num_threads(2)
    def checked(response, status=200):
        assert response.status_code == status, (response.status_code, response.text)
        return response.json()

    fixture_file = root / "fixture.json"
    fixture = json.loads(fixture_file.read_text()) if fixture_file.exists() else {"root": str(root), "tasks": {}}
    with TestClient(app, headers={"X-MDP-Client": "browser"}) as client:
        for task in args.tasks:
            if task in fixture["tasks"]:
                continue
            suffix = {"detect": "", "segment": "-seg", "classify": "-cls", "pose": "-pose"}[task]
            config_path = Path(ultralytics.__file__).parent / "cfg" / "models" / "26" / f"yolo26{suffix}.yaml"
            config = yaml.safe_load(config_path.read_text()); config["nc"] = 2; config["scale"] = "n"
            if task == "pose": config["kpt_shape"] = [2, 3]
            local_config = models / f"yolo26n{suffix}.yaml"; local_config.write_text(yaml.safe_dump(config))
            model = YOLO(str(local_config), task=task, verbose=False)
            model.model.names = {0: "part", 1: "other"}
            checkpoint = models / f"yolo26n{suffix}.pt"; model.save(str(checkpoint))
            project_body = {"name": f"转换隔离测试 / {task}", "task_type": task, "labels": ["part", "other"]}
            if task == "pose": project_body["keypoint_schema"] = {"names": ["left", "right"], "skeleton": [[0, 1]], "flip_idx": [1, 0]}
            project = checked(client.post("/api/projects", json=project_body), 201)
            pid = project["id"]; labels = checked(client.get(f"/api/projects/{pid}"))["labels"]
            for index in range(12):
                im = Image.new("RGB", (96 + index, 72), (20 + index * 15, 80, 130))
                ImageDraw.Draw(im).rectangle((20, 15, 60, 52), fill=(210, 150 + index, 60))
                data = io.BytesIO(); im.save(data, "PNG")
                aid = checked(client.post(f"/api/projects/{pid}/assets", files={"files": (f"sample-{index}.png", data.getvalue(), "image/png")}), 201)["asset_ids"][0]
                shape = {"shape_type": "rectangle", "points": [20, 15, 60, 52]}
                if task == "classify": shape = {"shape_type": "classification"}
                if task == "segment": shape = {"shape_type": "polygon", "points": [20, 15, 60, 15, 60, 52, 20, 52]}
                if task == "pose": shape = {"shape_type": "keypoints", "points": [20, 15, 60, 52], "keypoints": [{"x": 25, "y": 25, "visibility": 2}, {"x": 50, "y": 40, "visibility": 2}]}
                checked(client.post(f"/api/assets/{aid}/annotations", json={"label_id": labels[index % 2]["id"], **shape}), 201)
                with db() as conn: conn.execute("UPDATE assets SET review_status='approved' WHERE id=?", (aid,))
            version = checked(client.post(f"/api/projects/{pid}/versions", json={"name": "校准数据快照", "approved_only": True, "train_ratio": .75, "valid_ratio": .25, "test_ratio": 0}), 201)
            with db() as conn:
                rid = conn.execute("""INSERT INTO training_runs
                    (project_id,version_id,name,model_arch,task,status,progress,current_epoch,total_epochs,settings_json,created_by,created_at)
                    VALUES (?,?,?,?,?,'completed',1,1,1,?,'isolated-test',?)""",
                    (pid, version["id"], f"隔离转换检查：未训练的 {task} 权重", checkpoint.name, task,
                     json.dumps({"image_size": 64, "epochs": 1, "version_content_hash": version["content_hash"]}), now_iso())).lastrowid
                artifact = root / "data" / "training" / f"run-{rid}" / "attempt-1" / "best.pt"
                artifact.parent.mkdir(parents=True); artifact.write_bytes(checkpoint.read_bytes())
                metadata = {"filename": artifact.name, "size_bytes": artifact.stat().st_size, "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
                conn.execute("UPDATE training_runs SET artifact_path=?,artifact_metadata_json=? WHERE id=?", (str(artifact), json.dumps(metadata), rid))
            fixture["tasks"][task] = {"project_id": pid, "version_id": version["id"], "run_id": rid}
            fixture_file.write_text(json.dumps(fixture, ensure_ascii=False, indent=2))
        print("Fixture:", fixture_file, flush=True)
        if args.prepare_only: return
        for task in args.tasks:
            rid = fixture["tasks"][task]["run_id"]
            for target in args.formats:
                payload = {"format": target, "image_size": args.image_size, "device": "cpu" if target == "rknn" else args.device, "simplify": args.simplify,
                           "dynamic": args.dynamic if target in {"engine", "onnx"} else False,
                           "batch": args.batch if target == "engine" else 1}
                if target == "rknn": payload["rknn"] = {"target_platform": args.target, "quantized": args.quantized, "calibration_count": 8}
                if target == "engine": payload.update(half=True, workspace=1.0)
                conversion = checked(client.post(f"/api/training-runs/{rid}/conversions", json=payload), 202)
                cid = conversion["id"]; worker = "conversion-smoke"
                claim = training._claim_next("model_conversions", worker)
                assert claim and claim[0] == cid, claim
                training._run_with_lease("model_conversions", cid, worker, claim[1], training.run_conversion)
                result = checked(client.get(f"/api/conversions/{cid}"))
                assert result["status"] == "completed", result
                assert result["artifact"]["integrity"] == "verified", result
                downloaded = client.get(f"/api/conversions/{cid}/artifact")
                assert downloaded.status_code == 200 and len(downloaded.content) > 1024
                manifest = checked(client.get(f"/api/conversions/{cid}/manifest"))
                deployment = manifest["deployment"]
                assert manifest["sha256"] == hashlib.sha256(downloaded.content).hexdigest()
                assert deployment["task"] == task and len(deployment["labels"]) == 2
                assert deployment["version_id"] == fixture["tasks"][task]["version_id"]
                if target == "rknn":
                    assert deployment["target_platform"] == args.target
                    if args.quantized:
                        assert deployment["calibration"]["count"] == 8
                        assert len(deployment["calibration"]["source_sha256"]) == 8
                if target == "engine":
                    assert not deployment["engine"].get("inspection_error"), deployment
                    assert deployment["engine"]["inputs"] and deployment["engine"]["outputs"]
                    if args.dynamic: assert deployment["engine"]["optimization_profiles"], deployment
                if target == "onnx" and args.dynamic:
                    # Regression for a traced end-to-end TopK exceeding the
                    # available candidates at smaller dynamic input sizes.
                    import numpy as np
                    import onnxruntime as ort
                    opts = ort.SessionOptions(); opts.intra_op_num_threads = 2; opts.inter_op_num_threads = 1
                    session = ort.InferenceSession(downloaded.content, sess_options=opts, providers=["CPUExecutionProvider"])
                    for size, batch in ((32, 1), (args.image_size, 2), (args.image_size * 2, 2)):
                        tensor = np.random.default_rng(0).random((batch, 3, size, size), dtype=np.float32)
                        outputs = session.run(None, {session.get_inputs()[0].name: tensor})
                        assert outputs and all(np.isfinite(value).all() for value in outputs)
                    if task != "classify": assert deployment["postprocessing"]["external_nms_required"]
                print(json.dumps({"task": task, "format": target, "conversion_id": cid, "bytes": len(downloaded.content), "settings": result["settings"], "manifest_verified": True, "result": "PASS"}, ensure_ascii=False), flush=True)
    if temporary: temporary.cleanup()


if __name__ == "__main__":
    main()
