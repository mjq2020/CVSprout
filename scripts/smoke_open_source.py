"""Real two-process Web/worker and standalone inference acceptance test.

Uses temporary storage and randomly initialized tiny models. No business data or
downloaded weights are read. --device cuda:0 exercises the selected visible GPU.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import zipfile

import httpx

PROJECT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_health(url, process):
    for _ in range(120):
        if process.poll() is not None:
            raise RuntimeError(f"Process exited: {process.returncode}; see log")
        try:
            if httpx.get(url, timeout=2, trust_env=False).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(.5)
    raise TimeoutError(url)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--web-python", default=sys.executable)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="visionforge-acceptance-") as directory:
        root = Path(directory)
        data, models = root / "data", root / "models"
        data.mkdir(); (models / "training").mkdir(parents=True)
        code = secrets.token_urlsafe(24)
        (data / ".setup-code").write_text(code)
        env = {**os.environ, "MDP_DATA_DIR": str(data), "MDP_MODELS_DIR": str(models),
               "MDP_AUTH_MODE": "accounts", "MDP_MODEL_DEVICE": args.device,
               "MDP_TRAINING_MODEL_DIRS": str(models / "training"),
               "MDP_TRAINING_BASE_MODEL": str(models / "training/yolo26n.pt"),
               "MDP_TRAINING_SEGMENTATION_MODEL": str(models / "training/missing.pt"),
               "MDP_EMBEDDED_WORKER": "false", "MDP_EMBEDDED_PIPELINE_WORKER": "false",
               "MDP_WORKER_URL": "", "MDP_TRAINING_ENABLED": "true", "MDP_COOKIE_SECURE": "false",
               "YOLO_OFFLINE": "true", "YOLO_AUTOINSTALL": "false", "YOLO_CONFIG_DIR": str(root / "yolo"),
               "OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2", "MDP_PUBLIC_ORIGIN": ""}
        if args.device == "cpu":
            env["CUDA_VISIBLE_DEVICES"] = ""
        code_generate = "from ultralytics import YOLO; import sys; YOLO('yolo26n.yaml', verbose=False).save(sys.argv[1])"
        subprocess.run([sys.executable, "-c", code_generate, str(models / "training/yolo26n.pt")], env=env, check=True)
        worker_port, web_port, runtime_port = free_port(), free_port(), free_port()
        logs = []
        processes = []
        def launch(name, module, port, environment, python=sys.executable, cwd=PROJECT):
            log = (root / f"{name}.log").open("w+")
            logs.append((name, log))
            process = subprocess.Popen([python, "-m", "uvicorn", module, "--host", "127.0.0.1", "--port", str(port)],
                                       cwd=cwd, env=environment, stdout=log, stderr=subprocess.STDOUT)
            processes.append(process)
            return process
        try:
            worker = launch("worker", "app.worker_server:app", worker_port, env)
            wait_health(f"http://127.0.0.1:{worker_port}/health", worker)
            web = launch("web", "app.main:app", web_port,
                         {**env, "MDP_WORKER_URL": f"http://127.0.0.1:{worker_port}"}, args.web_python)
            wait_health(f"http://127.0.0.1:{web_port}/api/health", web)
            client = httpx.Client(base_url=f"http://127.0.0.1:{web_port}", headers={"X-MDP-Client": "browser"}, timeout=180, trust_env=False)
            def checked(response, expected=200):
                assert response.status_code == expected, (response.status_code, response.text[:1500])
                return response.json()
            checked(client.post("/api/setup", json={"code": code, "username": "owner", "display_name": "Owner", "password": "acceptance-password-123"}), 201)
            checked(client.post("/api/login", json={"username": "owner", "password": "acceptance-password-123"}))
            resources = checked(client.get("/api/training/resources"))
            assert resources["cuda_available"] == args.device.startswith("cuda"), resources
            catalog = checked(client.get("/api/training/catalog"))
            assert any(model["available"] for model in catalog["models"]), catalog
            project = checked(client.post("/api/projects", json={"name": "Acceptance", "task_type": "detect", "labels": ["object"]}), 201)
            detail = checked(client.get(f"/api/projects/{project['id']}"))
            label = detail["labels"][0]["id"]
            from PIL import Image, ImageDraw
            first_asset = None
            for index in range(6):
                image = Image.new("RGB", (64, 64), (index * 20, 20, 40))
                ImageDraw.Draw(image).rectangle([12, 12, 48, 48], fill=(220, 150, 40))
                stream = io.BytesIO(); image.save(stream, "PNG")
                asset = checked(client.post(f"/api/projects/{project['id']}/assets", files={"files": (f"{index}.png", stream.getvalue(), "image/png")}), 201)["asset_ids"][0]
                first_asset = first_asset or asset
                checked(client.post(f"/api/assets/{asset}/annotations", json={"label_id": label, "shape_type": "rectangle", "points": [12, 12, 48, 48]}), 201)
            # This exercises Web -> worker preview without loading a model in Web.
            models_response = checked(client.get("/api/models"))
            model_id = next(model["id"] for model in models_response if model["provider_type"] == "local_yolo")
            preview = client.post(f"/api/projects/{project['id']}/annotation-preview", json={"model_id": model_id, "asset_ids": [first_asset], "prompt": "object"})
            checked(preview)
            version = checked(client.post(f"/api/projects/{project['id']}/versions", json={"approved_only": False, "train_ratio": .67, "valid_ratio": .33, "test_ratio": 0}), 201)
            run = checked(client.post(f"/api/projects/{project['id']}/training-runs", json={
                "version_id": version["id"], "model_arch": "yolo26n.pt", "task": "detect", "epochs": 1,
                "image_size": 64, "batch_size": 2, "workers": 0, "device": args.device,
                "amp": False, "close_mosaic": 0}), 202)
            for _ in range(240):
                result = checked(client.get(f"/api/training-runs/{run['id']}"))
                if result["status"] in {"completed", "failed", "cancelled"}:
                    break
                time.sleep(.5)
            assert result["status"] == "completed", result
            response = client.get(f"/api/training-runs/{run['id']}/deployment")
            assert response.status_code == 200, response.text[:500]
            bundle = root / "bundle"; bundle.mkdir()
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                archive.extractall(bundle)
            manifest = json.loads((bundle / "model/manifest.json").read_text())
            assert hashlib.sha256((bundle / "model/model.pt").read_bytes()).hexdigest() == manifest["sha256"]
            key = secrets.token_urlsafe(32)
            runtime = launch("runtime", "app.runtime.server:app", runtime_port,
                             {**env, "VISIONFORGE_MODEL_DIR": str(bundle / "model"), "VISIONFORGE_API_KEY": key,
                              "VISIONFORGE_DEVICE": args.device}, cwd=bundle)
            wait_health(f"http://127.0.0.1:{runtime_port}/health", runtime)
            endpoint = f"http://127.0.0.1:{runtime_port}/predict"
            assert httpx.post(endpoint, files={"file": ("test.png", stream.getvalue())}, trust_env=False).status_code == 401
            prediction = httpx.post(endpoint, headers={"Authorization": f"Bearer {key}"}, files={"file": ("test.png", stream.getvalue())}, timeout=120, trust_env=False)
            assert prediction.status_code == 200, prediction.text
            assert prediction.json()["task"] == "detect"
            print(f"PASS ({args.device}): first admin → local model → worker preview → queued training → verified deployment ZIP → authenticated inference", flush=True)
        except BaseException:
            for name, log in logs:
                log.flush(); log.seek(0)
                print(f"{name}:\n{log.read()[-18000:]}", file=sys.stderr)
            raise
        finally:
            for process in reversed(processes):
                process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
            for _, log in logs:
                log.close()


if __name__ == "__main__":
    main()
