from __future__ import annotations

import base64
import hashlib
import importlib
import io
import json
import sqlite3
import unittest
import zipfile
from collections import Counter
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from tests import test_dataset_preview as preview_helpers


class DatasetGenerationTest(unittest.TestCase):
    setUp = preview_helpers.DatasetPreviewTest.setUp
    tearDown = preview_helpers.DatasetPreviewTest.tearDown
    fixture = preview_helpers.DatasetPreviewTest.fixture

    def payload(self, operations=None, copies=1, target_count=None, **extra):
        augmentation = {"seed": 17, "copies_per_image": copies, "operations": operations or [{"type": "horizontal_flip"}, {"type": "noise", "std": 10}]}
        if target_count is not None: augmentation["target_count"] = target_count
        return {"approved_only": False, "train_ratio": .6, "valid_ratio": .2, "test_ratio": .2,
                "augmentation": augmentation, **extra}

    def request(self, project, payload, preview=False):
        return self.client.post(f"/api/projects/{project['id']}/versions" + ("/preview" if preview else ""), json=payload)

    def snapshot(self, version_id):
        module = importlib.import_module("app.dataset_versions")
        return module._load_snapshot_rows(version_id)

    def test_target_count_is_even_deterministic_and_preview_matches_frozen_plan(self):
        project, _, baseline = self.fixture(10, (.6, .2, .2))
        payload = self.payload(copies=20, target_count=11)
        originals = self.snapshot(baseline["id"])[2]
        train_ids = [row["original_asset_id"] for row in originals if row["split"] == "train"]
        with self.db() as conn:
            before = {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")] for table in ("assets", "annotations")}
        before_files = sorted(str(path) for path in self.settings.versions_dir.rglob("*"))
        previews = []
        for asset_id in train_ids:
            response = self.request(project, {**payload, "asset_id": asset_id}, True)
            self.assertEqual(response.status_code, 200, response.text)
            previews.append(response.json())
        quotas = {item["selected_asset"]["id"]: item["selected_asset"]["planned_copies"] for item in previews}
        self.assertEqual(sorted(quotas.values()), [1, 2, 2, 2, 2, 2])
        self.assertEqual(previews[0]["counts"], {"original": 10, "original_train": 6, "valid": 2, "test": 2, "generated": 11, "final": 21, "final_train": 17})
        self.assertTrue(all(not item["preview_truncated"] for item in previews))
        self.assertEqual(before_files, sorted(str(path) for path in self.settings.versions_dir.rglob("*")))
        self.assertEqual(self.one("SELECT COUNT(*) n FROM dataset_versions")["n"], 1)
        module = importlib.import_module("app.dataset_versions")
        _, _, source_assets, _ = module._source_snapshot(project["id"], False)
        assignments = {row["original_asset_id"]: row["split"] for row in originals}
        plan = module._augmentation_copy_plan(source_assets, assignments, payload["augmentation"])
        self.assertEqual(plan, module._augmentation_copy_plan(list(reversed(source_assets)), assignments, payload["augmentation"]))
        self.assertEqual({key: plan[key] for key in train_ids}, quotas)
        self.assertTrue(all(plan[row["original_asset_id"]] == 0 for row in originals if row["split"] != "train"))
        response = self.request(project, {**payload, "expected_source_hash": previews[0]["source_hash"]})
        self.assertEqual(response.status_code, 201, response.text)
        version = response.json(); info = version["stats"]["augmentation"]
        self.assertEqual((version["asset_count"], info["requested_generated"], info["counts"]["generated"], info["skipped_duplicates"], info["allocation_version"]), (21, 11, 11, 0, 1))
        _, _, frozen, annotations = self.snapshot(version["id"])
        self.assertEqual(Counter(row["original_asset_id"] for row in frozen if row["variant_index"]), Counter(quotas))
        self.assertTrue(all(row["split"] == "train" for row in frozen if row["variant_index"]))
        for preview in previews:
            for variant in preview["variants"]:
                row = next(row for row in frozen if row["original_asset_id"] == preview["selected_asset"]["id"] and row["variant_index"] == variant["variant_index"])
                self.assertEqual(hashlib.sha256(base64.b64decode(variant["image_data_url"].split(",", 1)[1])).hexdigest(), row["sha256"])
                annotation = next(item for item in annotations if item["version_asset_id"] == row["id"])
                self.assertEqual(json.loads(annotation["points_json"]), variant["annotations"][0]["points"])
        self.assertEqual(self.request(project, payload).json()["content_hash"], version["content_hash"])
        with self.db() as conn:
            for table, rows in before.items(): self.assertEqual([tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")], rows)
        self.assertEqual(self.client.get(f"/api/versions/{baseline['id']}").json()["content_hash"], baseline["content_hash"])

    def test_small_target_selects_an_allocated_image_and_manual_zero_quota_skips_engine(self):
        project, _, baseline = self.fixture(10, (.6, .2, .2))
        payload = self.payload(target_count=2)
        module = importlib.import_module("app.dataset_versions")
        _, _, assets, _ = module._source_snapshot(project["id"], False)
        assignments = {row["original_asset_id"]: row["split"] for row in self.snapshot(baseline["id"])[2]}
        plan = module._augmentation_copy_plan(assets, assignments, payload["augmentation"])
        positive = sorted(key for key, copies in plan.items() if copies)
        zero = next(key for key, copies in plan.items() if not copies and assignments[key] == "train")
        self.assertEqual(len(positive), 2)
        # Both are allocated; the automatic choice must prefer the annotated one.
        with self.db() as conn: conn.execute("DELETE FROM annotations WHERE asset_id=?", (positive[0],))
        automatic = self.request(project, payload, True)
        self.assertEqual(automatic.status_code, 200, automatic.text)
        self.assertEqual(automatic.json()["selected_asset"]["id"], positive[1])
        self.assertEqual(automatic.json()["selected_asset"]["planned_copies"], 1)
        engine = importlib.import_module("app.dataset_augmentation")
        with patch.object(engine, "augment_image", side_effect=AssertionError("A zero-quota image must not run augmentation")):
            selected = self.request(project, {**payload, "asset_id": zero}, True)
        self.assertEqual(selected.status_code, 200, selected.text)
        self.assertEqual((selected.json()["selected_asset"]["planned_copies"], selected.json()["preview_attempted"], selected.json()["variants"]), (0, 0, []))
        self.assertFalse(selected.json()["preview_truncated"])
        created = self.request(project, payload)
        self.assertEqual(created.status_code, 201, created.text)
        frozen = self.snapshot(created.json()["id"])[2]
        self.assertEqual({row["original_asset_id"] for row in frozen if row["variant_index"]}, set(positive))

    def test_twenty_copy_plan_bounds_preview_work_and_reports_actual_deduplication(self):
        project, _, _ = self.fixture(1, (1, 0, 0))
        payload = self.payload([{"type": "horizontal_flip"}], target_count=20, train_ratio=1, valid_ratio=0, test_ratio=0)
        engine = importlib.import_module("app.dataset_augmentation")
        with patch.object(engine, "augment_image", wraps=engine.augment_image) as augment:
            response = self.request(project, payload, True)
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertEqual(augment.call_count, 3, "Preview must not execute all 20 planned transforms")
        self.assertEqual((preview["selected_asset"]["planned_copies"], preview["preview_attempted"], preview["preview_limit"], preview["preview_truncated"]), (20, 3, 3, True))
        self.assertEqual((len(preview["variants"]), preview["skipped_duplicates"], preview["counts"]["generated"]), (1, 2, 20))
        created = self.request(project, payload)
        self.assertEqual(created.status_code, 201, created.text)
        info = created.json()["stats"]["augmentation"]
        self.assertEqual((info["requested_generated"], info["counts"]["generated"], info["skipped_duplicates"]), (20, 1, 19))
        # The expanded legacy per-image limit remains supported, including the
        # final variant index. None is the same as omitting target_count.
        legacy = self.payload([{"type": "noise"}], copies=20, train_ratio=1, valid_ratio=0, test_ratio=0)
        response = self.request(project, legacy)
        self.assertEqual(response.status_code, 201, response.text)
        variants = [row for row in self.snapshot(response.json()["id"])[2] if row["variant_index"]]
        self.assertEqual([row["variant_index"] for row in variants], list(range(1, 21)))
        self.assertNotIn("allocation_version", response.json()["stats"]["augmentation"])
        self.assertNotIn("target_count", response.json()["augmentation"])
        legacy["augmentation"]["target_count"] = None
        self.assertEqual(self.request(project, legacy).json()["content_hash"], response.json()["content_hash"])

    def test_target_capacity_and_weighted_pixel_limits_reject_before_writing(self):
        project, _, _ = self.fixture(2, (1, 0, 0))
        before_files = sorted(str(path) for path in self.settings.versions_dir.rglob("*"))
        for target in (0, -1, True, 1.5, "2", 41, 5001):
            for preview in (True, False):
                response = self.request(project, self.payload(target_count=target, train_ratio=1, valid_ratio=0, test_ratio=0), preview)
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.request(project, self.payload(copies=21), True).status_code, 422)
        self.assertEqual(self.one("SELECT COUNT(*) n FROM dataset_versions")["n"], 1)
        self.assertEqual(before_files, sorted(str(path) for path in self.settings.versions_dir.rglob("*")))
        module = importlib.import_module("app.dataset_versions")
        schemas = importlib.import_module("app.schemas")
        assets = [{"id": i, "sha256": str(i), "width": i + 2, "height": i + 3} for i in range(3)]
        assignments = {row["id"]: "train" for row in assets}
        payload = schemas.DatasetVersionCreate.model_validate(self.payload(copies=20, target_count=5))
        config = payload.augmentation.model_dump(exclude_none=True)
        plan = module._augmentation_copy_plan(assets, assignments, config)
        pixels = sum(row["width"] * row["height"] * plan[row["id"]] for row in assets)
        with patch.dict(module.AUGMENTATION_LIMITS, {"max_generated_pixels": pixels}):
            _, counts, quotas = module._augmentation_plan(project, assets, [], assignments, payload)
            self.assertEqual((counts["generated"], quotas), (5, plan))
        from fastapi import HTTPException
        with patch.dict(module.AUGMENTATION_LIMITS, {"max_generated_pixels": pixels - 1}):
            with self.assertRaises(HTTPException) as caught: module._augmentation_plan(project, assets, [], assignments, payload)
            self.assertEqual(caught.exception.status_code, 422)
        many = [{"id": i, "sha256": str(i), "width": 1, "height": 1} for i in range(251)]
        all_train = {row["id"]: "train" for row in many}
        allowed = schemas.DatasetVersionCreate.model_validate(self.payload(target_count=5000))
        _, counts, quotas = module._augmentation_plan(project, many, [], all_train, allowed)
        self.assertEqual(counts["generated"], 5000); self.assertLessEqual(max(quotas.values()), 20)
        excessive = schemas.DatasetVersionCreate.model_validate(self.payload(copies=20))
        with self.assertRaises(HTTPException): module._augmentation_plan(project, many, [], all_train, excessive)
        with patch.dict(module.AUGMENTATION_LIMITS, {"max_total_assets": 7}):
            with self.assertRaises(HTTPException): module._augmentation_plan(project, assets, [], assignments, payload)

    def test_new_operation_defaults_ranges_and_irrelevant_parameters(self):
        schemas = importlib.import_module("app.schemas")
        defaults = {"gamma": {"min_gamma": .8, "max_gamma": 1.2}, "hue": {"min_shift": -.05, "max_shift": .05}, "sharpness": {"min_factor": .5, "max_factor": 1.5}, "autocontrast": {"cutoff": 1}, "equalize": {}, "jpeg_compression": {"min_quality": 60, "max_quality": 95}, "downscale": {"min_scale": .5, "max_scale": .9}}
        for kind, fields in defaults.items():
            self.assertEqual(schemas.AugmentationOperation(type=kind).model_dump(exclude_none=True), {"type": kind, "probability": 1, **fields})
        project, _, _ = self.fixture(1)
        options = self.client.get(f"/api/projects/{project['id']}/augmentation-options").json()
        self.assertEqual(options["limits"]["max_copies_per_image"], 20)
        self.assertEqual(len(options["operations"]), 16)
        self.assertTrue(set(defaults) <= {row["type"] for row in options["operations"] if row["available"]})
        invalid = [
            {"type": "gamma", "min_gamma": .19}, {"type": "gamma", "max_gamma": 3.1}, {"type": "gamma", "min_gamma": 2, "max_gamma": 1},
            {"type": "hue", "min_shift": -.51}, {"type": "hue", "max_shift": .51}, {"type": "hue", "min_shift": .2, "max_shift": .1},
            {"type": "sharpness", "min_factor": -.1}, {"type": "sharpness", "max_factor": 3.1}, {"type": "sharpness", "min_factor": 2, "max_factor": 1},
            {"type": "autocontrast", "cutoff": 10.1}, {"type": "autocontrast", "cutoff": -1}, {"type": "equalize", "cutoff": 0},
            {"type": "jpeg_compression", "min_quality": 9}, {"type": "jpeg_compression", "max_quality": 101}, {"type": "jpeg_compression", "min_quality": 60.0}, {"type": "jpeg_compression", "min_quality": 90, "max_quality": 70},
            {"type": "downscale", "min_scale": .09}, {"type": "downscale", "max_scale": 1.1}, {"type": "downscale", "min_scale": .8, "max_scale": .5},
            {"type": "gamma", "min_gamma": True}, {"type": "downscale", "min_scale": ".5"}, {"type": "equalize", "radius": None},
            {"type": "brightness", "min_factor": 0}, {"type": "contrast", "max_factor": 3}, {"type": "saturation", "max_factor": 2.1},
        ]
        for operation in invalid:
            with self.subTest(operation=operation):
                response = self.request(project, self.payload([operation]), True)
                self.assertEqual(response.status_code, 422, response.text)
        all_types = [{"type": row["type"]} for row in options["operations"]]
        self.assertEqual(len(schemas.DatasetAugmentation(operations=all_types).operations), 16)
        response = self.request(project, self.payload([{"type": "sharpness", "min_factor": 0, "max_factor": 3}]), True)
        self.assertEqual(response.status_code, 200, response.text)

    def test_preview_matches_frozen_train_variants_and_is_readonly(self):
        project, _, original = self.fixture(10, (.6, .2, .2))
        payload = self.payload(copies=2)
        before_files = sorted(str(path) for path in self.settings.versions_dir.rglob("*"))
        preview = self.request(project, payload, True)
        self.assertEqual(preview.status_code, 200, preview.text)
        preview = preview.json()
        self.assertEqual(preview["counts"], {"original": 10, "original_train": 6, "valid": 2, "test": 2, "generated": 12, "final": 22, "final_train": 18})
        self.assertEqual(self.one("SELECT COUNT(*) n FROM dataset_versions")["n"], 1)
        self.assertEqual(before_files, sorted(str(path) for path in self.settings.versions_dir.rglob("*")))
        created = self.request(project, payload)
        self.assertEqual(created.status_code, 201, created.text)
        version = created.json(); self.assertEqual(version["asset_count"], 22)
        self.assertEqual(version["stats"]["augmentation"]["counts"], preview["counts"])
        _, _, assets, annotations = self.snapshot(version["id"])
        self.assertEqual(sum(row["variant_index"] > 0 for row in assets), 12)
        self.assertTrue(all(row["split"] == "train" for row in assets if row["variant_index"]))
        for variant in preview["variants"]:
            row = next(row for row in assets if row["original_asset_id"] == preview["selected_asset"]["id"] and row["variant_index"] == variant["variant_index"])
            self.assertEqual(hashlib.sha256(base64.b64decode(variant["image_data_url"].split(",", 1)[1])).hexdigest(), row["sha256"])
            frozen_annotation = next(item for item in annotations if item["version_asset_id"] == row["id"])
            self.assertEqual(json.loads(frozen_annotation["points_json"]), variant["annotations"][0]["points"])
            source = next(item for item in assets if item["id"] == row["source_version_asset_id"])
            self.assertEqual((source["original_asset_id"], source["variant_index"], source["split"]), (row["original_asset_id"], 0, "train"))
        repeated = self.request(project, payload)
        self.assertEqual(repeated.json()["content_hash"], version["content_hash"])
        self.assertEqual(self.client.get(f"/api/versions/{original['id']}").json()["content_hash"], original["content_hash"])
        unaugmented = self.request(project, {**payload, "augmentation": None})
        self.assertEqual(unaugmented.json()["content_hash"], original["content_hash"])

    def test_duplicates_are_skipped_using_pixels_and_annotation_geometry(self):
        project, _, baseline = self.fixture(1, (1, 0, 0))
        # A uniform image is unchanged by flipping, but its off-centre box moves.
        payload = self.payload([{"type": "horizontal_flip"}], copies=3, train_ratio=1, valid_ratio=0, test_ratio=0)
        preview = self.request(project, payload, True).json()
        self.assertEqual((len(preview["variants"]), preview["skipped_duplicates"]), (1, 2))
        version = self.request(project, payload).json()
        self.assertEqual((version["asset_count"], version["stats"]["augmentation"]["skipped_duplicates"]), (2, 2))
        # Two flips restore both pixels and coordinates, including int/float equality.
        none = self.request(project, self.payload([{"type": "rotate90", "degrees": [180]}, {"type": "horizontal_flip"}, {"type": "vertical_flip"}], train_ratio=1, valid_ratio=0, test_ratio=0))
        self.assertEqual(none.status_code, 201, none.text)
        self.assertEqual((none.json()["asset_count"], none.json()["stats"]["augmentation"]["skipped_duplicates"]), (1, 1))
        training = importlib.import_module("app.training")
        for version_id, expected in ((version["id"], True), (none.json()["id"], False), (baseline["id"], False)):
            warnings = training._version_compatibility(self.snapshot(version_id)[0], "detect")["warnings"]
            self.assertEqual(any("在线增强" in message for message in warnings), expected)

    def test_valid_test_preview_never_augments_and_cross_project_is_rejected(self):
        project, _, baseline = self.fixture(5, (.6, .2, .2))
        held_out = next(row for row in self.snapshot(baseline["id"])[2] if row["split"] != "train")
        preview = self.request(project, self.payload(asset_id=held_out["original_asset_id"]), True)
        self.assertEqual(preview.status_code, 200, preview.text); self.assertEqual(preview.json()["variants"], [])
        other, ids, _ = self.fixture(1)
        self.assertEqual(self.request(project, self.payload(asset_id=ids[0]), True).status_code, 404)
        self.actor = self.Actor("annotator", "annotator")
        self.assertEqual(self.request(other, self.payload(), True).status_code, 403)
        self.assertEqual(self.request(other, self.payload()).status_code, 403)

    def test_preview_source_hash_rejects_changed_annotations_labels_and_membership(self):
        project, ids, baseline = self.fixture(3)
        payload = self.payload()
        for change in ("annotation", "label", "added_image", "deleted_image"):
            with self.subTest(change=change):
                preview = self.request(project, payload, True).json()
                if change == "annotation":
                    with self.db() as conn:
                        conn.execute("UPDATE annotations SET points_json='[15,15,65,65]',version=version+1 WHERE asset_id=?", (ids[0],))
                        conn.execute("UPDATE assets SET revision=revision+1 WHERE id=?", (ids[0],))
                elif change == "label":
                    with self.db() as conn: conn.execute("UPDATE labels SET name='renamed' WHERE project_id=?", (project["id"],))
                elif change == "added_image":
                    image = io.BytesIO(); Image.new("RGB", (100, 70), (241, 90, 12)).save(image, "PNG")
                    added = self.client.post(f"/api/projects/{project['id']}/assets", files={"files": ("new.png", image.getvalue(), "image/png")})
                    self.assertEqual(added.status_code, 201)
                else:
                    with self.db() as conn: conn.execute("DELETE FROM assets WHERE id=?", (ids[-1],))
                refused = self.request(project, {**payload, "expected_source_hash": preview["source_hash"]})
                self.assertEqual(refused.status_code, 409, refused.text)
                self.assertEqual(refused.json()["detail"]["code"], "source_changed")
                self.assertEqual(self.one("SELECT COUNT(*) n FROM dataset_versions")["n"], 1)
                self.assertEqual(list(self.settings.versions_dir.glob(".version-staging-*")), [])
        refreshed = self.request(project, payload, True).json()
        accepted = self.request(project, {**payload, "expected_source_hash": refreshed["source_hash"]})
        self.assertEqual(accepted.status_code, 201, accepted.text)
        self.assertEqual(self.client.get(f"/api/versions/{baseline['id']}").json()["content_hash"], baseline["content_hash"])

    def test_edits_after_snapshot_capture_do_not_change_the_previewed_freeze(self):
        project, _, _ = self.fixture(1, (1, 0, 0))
        payload = self.payload(train_ratio=1, valid_ratio=0, test_ratio=0)
        preview = self.request(project, payload, True).json()
        module = importlib.import_module("app.dataset_versions")
        copy_file = module.shutil.copyfile
        changed = False
        def modify_then_copy(source, destination):
            nonlocal changed
            if not changed:
                changed = True
                with self.db() as conn:
                    conn.execute("UPDATE annotations SET points_json='[15,15,65,65]',version=version+1 WHERE asset_id=?", (preview["selected_asset"]["id"],))
                    conn.execute("UPDATE assets SET revision=revision+1 WHERE id=?", (preview["selected_asset"]["id"],))
            return copy_file(source, destination)
        with patch.object(module.shutil, "copyfile", side_effect=modify_then_copy):
            result = self.request(project, {**payload, "expected_source_hash": preview["source_hash"]})
        self.assertEqual(result.status_code, 201, result.text)
        _, _, assets, annotations = self.snapshot(result.json()["id"])
        original = next(asset for asset in assets if asset["variant_index"] == 0)
        frozen = next(row for row in annotations if row["version_asset_id"] == original["id"])
        self.assertEqual(json.loads(frozen["points_json"]), preview["original"]["annotations"][0]["points"])
        self.assertGreater(self.one("SELECT revision FROM assets WHERE id=?", (original["original_asset_id"],))["revision"], original["revision"])

    def test_preview_delete_between_file_check_and_read_reports_conflict(self):
        project, ids, _ = self.fixture(1)
        source = self.settings.media_dir / self.one("SELECT storage_name FROM assets WHERE id=?", (ids[0],))["storage_name"]
        read_bytes = Path.read_bytes
        def delete_then_read(path):
            if path == source: path.unlink()
            return read_bytes(path)
        with patch.object(Path, "read_bytes", new=delete_then_read):
            result = self.request(project, self.payload(), True)
        self.assertEqual(result.status_code, 409, result.text)
        self.assertIn("重新预览", result.text)
        self.assertEqual(self.one("SELECT COUNT(*) n FROM dataset_versions")["n"], 1)

    def test_exif_rotated_and_mirrored_images_never_silently_move_annotations(self):
        for orientation in (2, 6):
            with self.subTest(orientation=orientation):
                project_id = self.client.post("/api/projects", json={"name": f"EXIF {orientation}", "task_type": "detect", "labels": ["object"]}).json()["id"]
                project = self.client.get(f"/api/projects/{project_id}").json()
                image = io.BytesIO(); exif = Image.Exif(); exif[274] = orientation
                Image.new("RGB", (128, 80), (20, 70, 100)).save(image, "JPEG", exif=exif)
                asset_id = self.client.post(f"/api/projects/{project_id}/assets", files={"files": ("oriented.jpg", image.getvalue(), "image/jpeg")}).json()["asset_ids"][0]
                annotated = self.client.post(f"/api/assets/{asset_id}/annotations", json={"label_id": project["labels"][0]["id"], "shape_type": "rectangle", "points": [10, 10, 60, 60]})
                self.assertEqual(annotated.status_code, 201, annotated.text)
                payload = self.payload(train_ratio=1, valid_ratio=0, test_ratio=0)
                for preview, augmentation in ((True, payload["augmentation"]), (True, None), (False, payload["augmentation"])):
                    rejected = self.request(project, {**payload, "augmentation": augmentation}, preview)
                    self.assertEqual(rejected.status_code, 422, rejected.text)
                    self.assertIn("重新导入并确认标注", rejected.text)
                self.assertEqual(self.one("SELECT COUNT(*) n FROM dataset_versions WHERE project_id=?", (project_id,))["n"], 0)
                self.assertEqual(list(self.settings.versions_dir.glob(".version-staging-*")), [])
                # Existing API clients can still freeze the original unmodified data.
                unchanged = self.request(project, {**payload, "augmentation": None})
                self.assertEqual(unchanged.status_code, 201, unchanged.text)
                asset = self.snapshot(unchanged.json()["id"])[2][0]
                self.assertEqual((asset["width"], asset["height"]), (128, 80))
                source = self.settings.versions_dir / f"version-{unchanged.json()['id']}" / asset["storage_name"]
                self.assertEqual(source.read_bytes(), image.getvalue())

    def test_schema_resource_limits_and_pose_flip_are_validated(self):
        project, _, _ = self.fixture(2)
        bad = [{"type": "crop"}, {"type": "noise", "radius": 2}, {"type": "brightness", "min_factor": 1.5, "max_factor": .5}, {"type": "rotate90", "degrees": [90.0]}, {"type": "blur", "radius": True}, {"type": "noise", "probability": 0}, {"type": "grayscale", "unexpected": 1}]
        for operation in bad:
            response = self.request(project, self.payload([operation]), True)
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.request(project, self.payload([{"type": "noise"}, {"type": "noise"}]), True).status_code, 422)
        module = importlib.import_module("app.dataset_versions")
        with patch.dict(module.AUGMENTATION_LIMITS, {"max_generated_assets": 0}):
            self.assertEqual(self.request(project, self.payload()).status_code, 422)
        for limit in ("max_generated_pixels", "max_total_assets"):
            with patch.dict(module.AUGMENTATION_LIMITS, {limit: 1}):
                self.assertEqual(self.request(project, self.payload(), True).status_code, 422)
        with self.db() as conn:
            conn.execute("UPDATE projects SET task_type='pose',keypoint_schema_json=? WHERE id=?", (json.dumps({"names": ["left", "right"]}), project["id"]))
        options = self.client.get(f"/api/projects/{project['id']}/augmentation-options").json()
        self.assertFalse(next(item for item in options["operations"] if item["type"] == "horizontal_flip")["available"])
        denied = self.request(project, self.payload(), True)
        self.assertEqual(denied.status_code, 422); self.assertIn("flip_idx", denied.text)

    def test_failed_transform_rolls_back_rows_files_and_does_not_remove_existing_version(self):
        project, _, baseline = self.fixture(2)
        before = sorted(str(path) for path in self.settings.versions_dir.rglob("*"))
        module = importlib.import_module("app.dataset_augmentation")
        with patch.object(module, "augment_image", side_effect=RuntimeError("injected transform failure")):
            with self.assertRaisesRegex(RuntimeError, "injected transform failure"):
                self.request(project, self.payload())
        self.assertEqual(self.one("SELECT COUNT(*) n FROM dataset_versions")["n"], 1)
        self.assertEqual(before, sorted(str(path) for path in self.settings.versions_dir.rglob("*")))
        with self.db() as conn:
            conn.execute("CREATE TRIGGER abort_snapshot BEFORE INSERT ON dataset_version_annotations BEGIN SELECT RAISE(ABORT, 'injected snapshot write failure'); END")
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "injected snapshot write failure"):
                self.request(project, self.payload())
        finally:
            with self.db() as conn: conn.execute("DROP TRIGGER abort_snapshot")
        self.assertEqual(self.one("SELECT COUNT(*) n FROM dataset_versions")["n"], 1)
        self.assertEqual(before, sorted(str(path) for path in self.settings.versions_dir.rglob("*")))
        # An existing directory must never be deleted by cleanup for a failed rename.
        collision = self.settings.versions_dir / f"version-{baseline['id'] + 1}"
        collision.mkdir(); sentinel = collision / "owned-by-another-attempt"; sentinel.write_text("keep")
        with self.assertRaisesRegex(RuntimeError, "版本目录已存在"):
            self.request(project, self.payload())
        self.assertEqual(sentinel.read_text(), "keep")

    def test_augmented_exports_use_unique_ids_and_survive_original_deletion(self):
        project, original_ids, _ = self.fixture(5, (.6, .2, .2))
        version = self.request(project, self.payload(copies=2)).json()
        with self.db() as conn: conn.execute("DELETE FROM assets WHERE project_id=?", (project["id"],))
        for path in self.settings.media_dir.rglob("*"):
            if path.is_file(): path.unlink()
        for format in ("coco", "yolo", "json"):
            response = self.client.get(f"/api/versions/{version['id']}/export/{format}")
            self.assertEqual(response.status_code, 200, response.text[:100] if response.status_code != 200 else "")
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                self.assertEqual(len(archive.namelist()), len(set(archive.namelist())))
                images = [name for name in archive.namelist() if name.startswith("images/")]
                self.assertEqual(len(images), 11)
                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(manifest["augmentation"]["seed"], 17)
                self.assertEqual({row["original_asset_id"] for row in manifest["assets"]}, set(original_ids))
                if format == "coco":
                    coco = json.loads(archive.read("annotations/instances_train.json"))
                    ids = [row["id"] for row in coco["images"]]
                    self.assertEqual(len(ids), len(set(ids)))
                    self.assertEqual({row["image_id"] for row in coco["annotations"]}, set(ids))
                if format == "yolo":
                    self.assertEqual(sum(name.startswith("labels/") for name in archive.namelist()), 11)

    def test_four_tasks_are_frozen_and_consumed_by_training_preparation(self):
        for task in ("detect", "pose", "segment", "classify"):
            with self.subTest(task=task):
                spec = {"name": task, "task_type": task, "labels": ["object"]}
                if task == "pose": spec["keypoint_schema"] = {"names": ["left", "right"], "flip_idx": [1, 0]}
                project_id = self.client.post("/api/projects", json=spec).json()["id"]
                project = self.client.get(f"/api/projects/{project_id}").json()
                image = io.BytesIO(); Image.new("RGB", (100, 80), (80, 120, 200)).save(image, "PNG")
                asset_id = self.client.post(f"/api/projects/{project_id}/assets", files={"files": ("task.png", image.getvalue(), "image/png")}).json()["asset_ids"][0]
                shape = {"detect": {"shape_type": "rectangle", "points": [10, 10, 40, 60]}, "classify": {"shape_type": "classification", "points": []}, "segment": {"shape_type": "polygon", "points": [10, 10, 40, 10, 40, 60, 10, 60]}, "pose": {"shape_type": "keypoints", "points": [10, 10, 40, 60], "keypoints": [{"x": 15, "y": 20, "visibility": 2}, {"x": 30, "y": 40, "visibility": 1}]}}[task]
                response = self.client.post(f"/api/assets/{asset_id}/annotations", json={"label_id": project["labels"][0]["id"], **shape})
                self.assertEqual(response.status_code, 201, response.text)
                operations = [{"type": "horizontal_flip"}, {"type": "rotate90", "degrees": [90]}, {"type": "brightness", "min_factor": .5, "max_factor": .5}]
                operations += [{"type": kind} for kind in ("gamma", "hue", "sharpness", "autocontrast", "equalize", "jpeg_compression", "downscale")]
                payload = self.payload(operations, target_count=1, train_ratio=1, valid_ratio=0, test_ratio=0)
                preview_response = self.request(project, payload, True)
                self.assertEqual(preview_response.status_code, 200, preview_response.text)
                preview = preview_response.json()
                version_response = self.request(project, payload)
                self.assertEqual(version_response.status_code, 201, version_response.text)
                version = version_response.json(); self.assertEqual((version["asset_count"], version["annotation_count"]), (2, 2))
                _, _, assets, annotations = self.snapshot(version["id"])
                derived = next(row for row in assets if row["variant_index"])
                self.assertEqual((derived["width"], derived["height"]), (80, 100))
                self.assertEqual(hashlib.sha256(base64.b64decode(preview["variants"][0]["image_data_url"].split(",", 1)[1])).hexdigest(), derived["sha256"])
                annotation = next(row for row in annotations if row["version_asset_id"] == derived["id"])
                if task == "pose":
                    self.assertEqual(json.loads(annotation["keypoints_json"]), [{"x": 40, "y": 30, "visibility": 1}, {"x": 20, "y": 15, "visibility": 2}])
                elif task == "segment": self.assertEqual(json.loads(annotation["points_json"]), [10, 10, 10, 40, 60, 40, 60, 10])
                elif task == "detect": self.assertEqual(json.loads(annotation["points_json"]), [10, 10, 60, 40])
                else: self.assertEqual(json.loads(annotation["points_json"]), [])
                exported = self.client.get(f"/api/versions/{version['id']}/export/yolo")
                self.assertEqual(exported.status_code, 200)
                with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
                    self.assertEqual(sum(name.endswith(".png") for name in archive.namelist()), 2)
                    if task == "pose": self.assertIn("flip_idx: [1, 0]", archive.read("data.yaml").decode())
                training = importlib.import_module("app.training")
                run_dir = Path(self.temp.name) / f"train-preparation-{task}"; run_dir.mkdir()
                prepared = training._prepare_training_dataset(1, version["id"], run_dir, task)
                dataset = prepared if task == "classify" else prepared.parent
                images = list(dataset.rglob("*.png"))
                self.assertEqual(len(images), 2)
                if task == "classify":
                    self.assertTrue(all(path.parent.name.startswith("0000_") and path.parent.parent.name == "train" for path in images))
                    self.assertEqual(json.loads((dataset / "classes.json").read_text())[0]["name"], "object")
                else:
                    image_stems = {path.stem for path in images}
                    label_files = list((dataset / "labels" / "train").glob("*.txt"))
                    self.assertEqual({path.stem for path in label_files}, image_stems)
                    self.assertTrue(all(path.read_text().strip() for path in label_files))
                    self.assertIn(str(dataset.resolve()), prepared.read_text())
                    if task == "pose":
                        self.assertIn("flip_idx: [1, 0]", prepared.read_text())
                        self.assertTrue(all(len(path.read_text().split()) == 11 for path in label_files))

    def old_database(self, missing_columns=False):
        from app import database
        project, _, version = self.fixture(2)
        destination = Path(self.temp.name) / "legacy"; destination.mkdir()
        isolated = replace(self.settings, data_dir=destination)
        with sqlite3.connect(self.settings.db_path) as source:
            dump = "\n".join(source.iterdump())
        dump = dump.replace("UNIQUE(version_id, original_asset_id, variant_index)", "UNIQUE(version_id, original_asset_id)").replace("UNIQUE(version_asset_id, original_annotation_id)", "UNIQUE(version_id, original_annotation_id)")
        with sqlite3.connect(isolated.db_path) as conn:
            conn.executescript(dump)
            conn.execute("DELETE FROM schema_migrations WHERE version=10")
            conn.execute("UPDATE sqlite_sequence SET seq=99 WHERE name='dataset_version_assets'")
            conn.execute("UPDATE sqlite_sequence SET seq=199 WHERE name='dataset_version_annotations'")
            if missing_columns:
                conn.execute("ALTER TABLE dataset_versions DROP COLUMN augmentation_json")
                for column in ("source_version_asset_id", "augmentation_json", "variant_index"):
                    conn.execute(f"ALTER TABLE dataset_version_assets DROP COLUMN {column}")
        return database, isolated, version

    def test_variant_migration_preserves_old_snapshots_foreign_keys_and_sequences(self):
        database, isolated, version = self.old_database(missing_columns=True)
        with sqlite3.connect(isolated.db_path) as conn:
            before = {table: conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() for table in ("dataset_versions", "dataset_version_assets", "dataset_version_annotations")}
            columns = {table: ",".join(row[1] for row in conn.execute(f"PRAGMA table_info({table})")) for table in before}
        with patch.object(database, "settings", isolated): database.init_db(); database.init_db()
        with sqlite3.connect(isolated.db_path) as conn:
            for table, rows in before.items(): self.assertEqual(conn.execute(f"SELECT {columns[table]} FROM {table} ORDER BY id").fetchall(), rows)
            self.assertEqual(conn.execute("SELECT DISTINCT variant_index,source_version_asset_id,augmentation_json FROM dataset_version_assets").fetchall(), [(0, None, "{}")])
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("SELECT seq FROM sqlite_sequence WHERE name='dataset_version_assets'").fetchone()[0], 99)
            self.assertEqual(conn.execute("SELECT seq FROM sqlite_sequence WHERE name='dataset_version_annotations'").fetchone()[0], 199)
            self.assertEqual(conn.execute("SELECT content_hash FROM dataset_versions WHERE id=?", (version["id"],)).fetchone()[0], version["content_hash"])
            self.assertIn("original_asset_id, variant_index", conn.execute("SELECT sql FROM sqlite_master WHERE name='dataset_version_assets'").fetchone()[0])

    def test_variant_migration_failure_is_atomic_and_empty_sequences_survive(self):
        database, isolated, _ = self.old_database()
        class FailingConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql.startswith("ALTER TABLE dataset_version_annotations_variants RENAME"):
                    raise sqlite3.OperationalError("injected migration failure")
                return super().execute(sql, *args)
        with self.assertRaisesRegex(sqlite3.OperationalError, "injected"):
            with sqlite3.connect(isolated.db_path, factory=FailingConnection) as conn:
                conn.execute("PRAGMA foreign_keys=OFF"); conn.execute("BEGIN IMMEDIATE")
                database._migrate_dataset_variants(conn)
        with sqlite3.connect(isolated.db_path) as conn:
            self.assertIn("UNIQUE(version_id, original_asset_id)", conn.execute("SELECT sql FROM sqlite_master WHERE name='dataset_version_assets'").fetchone()[0])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM dataset_version_assets").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM dataset_version_annotations").fetchone()[0], 2)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            conn.execute("DELETE FROM dataset_version_annotations"); conn.execute("DELETE FROM dataset_version_assets")
        with patch.object(database, "settings", isolated): database.init_db()
        with sqlite3.connect(isolated.db_path) as conn:
            self.assertEqual(conn.execute("SELECT seq FROM sqlite_sequence WHERE name='dataset_version_assets'").fetchone()[0], 99)
            self.assertEqual(conn.execute("SELECT seq FROM sqlite_sequence WHERE name='dataset_version_annotations'").fetchone()[0], 199)


if __name__ == "__main__": unittest.main()
