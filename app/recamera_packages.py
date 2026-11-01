"""Build reCamera packages with the pinned official builder, without host SDK imports."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

from .config import BASE_DIR

ROOT = BASE_DIR / "deploy" / "recamera"
OFFICIAL = ROOT / "official"
MAX_PACKAGE = 200 * 1024 * 1024


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def official_contract():
    source = json.loads((OFFICIAL / "source.json").read_text())
    for name, expected in source["files"].items():
        if sha256(OFFICIAL / name) != expected:
            raise ValueError("官方打包器完整性校验失败，请恢复发行文件")
    spec = importlib.util.spec_from_file_location("cvsprout_recamera_manifest", OFFICIAL / "market/appmgr/manifest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def inspect_archive(path: Path) -> dict:
    """Stream hashes; never extract or execute package members."""
    if not 0 < path.stat().st_size <= MAX_PACKAGE:
        raise ValueError("应用包大小必须在 200 MiB 内")
    contract = official_contract()
    records, metadata, total = {}, {}, 0
    with tarfile.open(path, "r:gz") as archive:
        seen = set()
        for entry in archive:
            contract.validate_package_member_path(entry.name)
            if not entry.isfile() or entry.name in seen or len(seen) >= 4096:
                raise ValueError("应用包包含重复、链接或无效文件")
            seen.add(entry.name)
            total += entry.size
            if total > 400 * 1024 * 1024:
                raise ValueError("解压后应用超过大小限制")
            stream = archive.extractfile(entry)
            if entry.name in {"manifest.json", "release.lock.json", "files.sha256"}:
                if entry.size > 1024 * 1024:
                    raise ValueError("应用元数据过大")
                data = stream.read()
                metadata[entry.name] = data
                digest = hashlib.sha256(data).hexdigest()
            else:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if entry.name not in {"release.lock.json", "files.sha256"}:
                records[entry.name] = {"size": entry.size, "sha256": digest}
        manifest = json.loads(metadata["manifest.json"])
        lock = json.loads(metadata["release.lock.json"])
        contract.validate_manifest(manifest)
        contract.validate_package_files(manifest, records)
        contract.verify_release_metadata(manifest, lock, metadata["files.sha256"], records)
    return {"manifest": manifest, "release_id": lock["release_id"], "sha256": sha256(path), "size": path.stat().st_size}


def build_package(source: Path, destination: Path) -> Path:
    official_contract()
    completed = subprocess.run([sys.executable, str(OFFICIAL / "market/packaging/build.py"),
                                str(source), "--out", str(destination)],
                               capture_output=True, text=True, timeout=90, check=False)
    if completed.returncode:
        raise ValueError("官方 App Center 打包校验失败：" + (completed.stderr or completed.stdout)[-2000:])
    manifest = json.loads((source / "manifest.json").read_text())
    archive = destination / f"{manifest['id']}-{manifest['version']}-arm64.tar.gz"
    inspect_archive(archive)
    return archive


def model_contract(conversion: dict, run: dict) -> dict:
    """Admit tensor contracts, not a model name guessed from a filename."""
    if conversion.get("status") != "completed" or conversion.get("format") != "rknn":
        raise ValueError("请先完成 RKNN 模型转换")
    metadata = json.loads(conversion.get("artifact_metadata_json") or "{}")
    deployment = metadata.get("deployment") or {}
    if deployment.get("target_platform") != "rv1126b":
        raise ValueError("reCamera Pro 需要 RV1126B 模型，请重新选择转换芯片")
    if run.get("task") != "detect" or deployment.get("task") != "detect":
        raise ValueError("当前设备推理应用支持目标检测，其他任务尚未适配")
    post = deployment.get("postprocessing") or {}
    if (post.get("family") != "ultralytics_original_graph" or post.get("end2end") is not False
            or post.get("nms_in_graph") is not False or post.get("box_format") != "xywh"):
        raise ValueError("需要无内置 NMS、非端到端的 YOLO 已解码 xywh 输出")
    labels = deployment.get("labels") or []
    if not labels or any(row.get("category_index") != i or not isinstance(row.get("name"), str)
                         or not row["name"].strip() or "\n" in row["name"] or "\r" in row["name"]
                         for i, row in enumerate(labels)):
        raise ValueError("转换模型缺少连续、有效的类别名称")
    names = [row["name"] for row in labels]
    if len(set(names)) != len(names):
        raise ValueError("模型类别名称不能重复")
    source = deployment.get("source_onnx") or {}
    inputs, outputs = source.get("inputs") or [], source.get("outputs") or []
    shape = inputs[0].get("shape", []) if len(inputs) == 1 else []
    if (len(shape) != 4 or inputs[0].get("dtype") != "FLOAT" or shape[:2] != [1, 3] or type(shape[2]) is not int
            or not 32 <= shape[2] <= 2048 or shape[2] != shape[3] or shape[2] % 32):
        raise ValueError("模型输入必须是静态 1×3×S×S，S 为 32 的倍数")
    out = outputs[0].get("shape", []) if len(outputs) == 1 else []
    if (len(out) != 3 or out[0] != 1 or out[1] != 4 + len(names)
            or type(out[2]) is not int or out[2] <= 0 or source.get("contains_non_max_suppression") is not False):
        raise ValueError("模型输出必须是 [1, 4+类别数, 候选框数]，且不含 NMS")
    pre = deployment.get("preprocessing") or {}
    if (pre.get("input_color") != "rgb" or pre.get("mean_values") != [0, 0, 0]
            or pre.get("std_values") != [255, 255, 255]
            or pre.get("normalization_location") != "rknn_input_preprocessing"
            or pre.get("padding_value") != 114 or pre.get("auto_padding") is not False
            or pre.get("resize") != "letterbox" or pre.get("image_size") != [shape[2], shape[2]]):
        raise ValueError("模型预处理与设备 RGB uint8 / letterbox / 114 填充约定不一致")
    run_meta = json.loads(run.get("artifact_metadata_json") or "{}")
    arch = str(run_meta.get("model_arch", "yolo"))
    family = re.match(r"^(yolov8|yolo11|yolo26)", arch.lower())
    if not family:
        raise ValueError("当前已适配 YOLOv8、YOLO11、YOLO26 的非端到端检测输出")
    arch = family[1]
    return {"schema_version": 1, "task": "detect", "architecture": arch,
            "target_platform": "rv1126b", "input_size": shape[2], "classes": names,
            "output_layout": "yolo_decoded_xywh", "end2end": False, "nms": False,
            "precision": deployment.get("precision", "int8"), "source_output_shape": out,
            "training_run_id": run["id"], "conversion_id": conversion["id"],
            "model_sha256": metadata.get("sha256"), "board_validated": False}


def stage_inference(destination: Path, model: Path, contract: dict, version: str, sequence: int) -> None:
    if model.stat().st_size > 180 * 1024 * 1024:
        raise ValueError("当前设备应用打包支持最大 180 MiB 的 RKNN 模型")
    shutil.copytree(ROOT / "templates/cvsprout-inference", destination)
    (destination / "models").mkdir(exist_ok=True)
    shutil.copyfile(model, destination / "models/model.rknn")
    copied = destination / "models/model.rknn"
    if sha256(copied) != contract["model_sha256"]:
        raise ValueError("模型在打包期间发生变化")
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.update(version=version, release={"sequence": sequence, "channel": "stable"})
    manifest["resources"]["limits"]["storage_mb"] = max(64, math.ceil(copied.stat().st_size / 1024**2) + 16)
    for model_row in manifest["models"]:
        model_row.update(file="models/model.rknn", input=[1, contract["input_size"], contract["input_size"], 3],
                         classes=contract["classes"], quant="int8" if contract["precision"] == "int8" else "fp16")
    manifest["artifacts"] = [{"id": "model", "kind": "rknn", "source": "bundled", "required": True, "share_scope": "private",
                              "file": "models/model.rknn", "mount": "models/model.rknn",
                              "sha256": sha256(copied), "size": copied.stat().st_size}]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    (destination / "model_contract.json").write_text(json.dumps(contract, ensure_ascii=False, indent=2) + "\n")
    (destination / "labels.txt").write_text("\n".join(contract["classes"]) + "\n")
    (destination / "MODEL_PROVENANCE.md").write_text(
        f"CVSprout training run {contract['training_run_id']}, conversion {contract['conversion_id']}.\n"
        f"SHA-256: {contract['model_sha256']}\n\n"
        "This trained Ultralytics model retains its upstream model license (AGPL-3.0 or your applicable commercial license).\n"
        "The Apache-2.0 application license does not relicense the model. Device accuracy/performance are not yet verified.\n")
