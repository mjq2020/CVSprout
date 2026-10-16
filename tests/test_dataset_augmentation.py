from __future__ import annotations

import copy
import hashlib
import unittest

import numpy as np
from PIL import Image

from app.dataset_augmentation import OPERATION_TYPES, augment_image


class DatasetAugmentationGeometryTest(unittest.TestCase):
    def test_large_noise_image_matches_seeded_pixel_sequence(self):
        image = Image.new("RGB", (1000, 400), (110, 90, 130))
        result, _, metadata = augment_image(image, [], {"seed": 11, "operations": [{"type": "noise", "std": 12}]},
            task="detect", keypoint_schema={}, source_key="large-image", variant_index=1)
        random = np.random.default_rng(int(metadata["derived_seed"], 16))
        random.random()  # Application probability decision.
        expected = np.clip(np.rint(np.asarray(image) + random.normal(0, 12, (400, 1000, 3))), 0, 255).astype(np.uint8)
        np.testing.assert_array_equal(np.asarray(result), expected)

    def setUp(self):
        self.image = Image.new("RGB", (12, 8), (0, 0, 0))
        pixels = np.asarray(self.image).copy(); pixels[1:4, 2:5] = (240, 100, 30)
        self.image = Image.fromarray(pixels)
        self.box = {"id": 17, "label_id": 3, "shape_type": "rectangle", "points": [2.0, 1.0, 5.0, 4.0], "attributes": {"source": "keep"}}

    def apply(self, operations, rows=None, *, task="detect", schema=None, seed=42, variant=1, source="17:sha"):
        return augment_image(self.image, rows if rows is not None else [self.box],
                             {"seed": seed, "copies_per_image": 5, "operations": operations},
                             task=task, keypoint_schema=schema or {}, source_key=source, variant_index=variant)

    def test_pixels_and_boxes_follow_right_angle_geometry(self):
        cases = [({"type": "horizontal_flip"}, [7, 1, 10, 4], (12, 8)),
                 ({"type": "vertical_flip"}, [2, 4, 5, 7], (12, 8)),
                 ({"type": "rotate90", "degrees": [90]}, [1, 7, 4, 10], (8, 12)),
                 ({"type": "rotate90", "degrees": [180]}, [7, 4, 10, 7], (12, 8)),
                 ({"type": "rotate90", "degrees": [270]}, [4, 2, 7, 5], (8, 12))]
        original = copy.deepcopy(self.box); original_pixels = self.image.tobytes()
        for operation, box, size in cases:
            with self.subTest(operation=operation):
                image, rows, metadata = self.apply([operation])
                self.assertEqual(image.size, size); self.assertEqual(rows[0]["points"], box)
                ys, xs = np.nonzero(np.asarray(image)[:, :, 0])
                self.assertEqual([int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1], box)
                self.assertEqual(rows[0]["id"], 17); self.assertEqual(rows[0]["attributes"], {"source": "keep"})
                self.assertEqual(metadata["output_size"], list(size))
        self.assertEqual(self.box, original); self.assertEqual(self.image.tobytes(), original_pixels)

    def test_polygon_vertices_compose_with_image_dimensions(self):
        polygon = {"shape_type": "polygon", "points": [2, 1, 5, 1, 5, 4, 2, 4], "label_id": 9}
        image, rows, _ = self.apply([{"type": "horizontal_flip"}, {"type": "rotate90", "degrees": [90]}], [polygon], task="segment")
        self.assertEqual(image.size, (8, 12))
        self.assertEqual(rows[0]["points"], [1, 2, 1, 5, 4, 5, 4, 2])
        xs, ys = rows[0]["points"][::2], rows[0]["points"][1::2]
        self.assertTrue(all(0 <= x <= 8 for x in xs)); self.assertTrue(all(0 <= y <= 12 for y in ys))

    def test_pose_horizontal_flip_swaps_semantics_and_preserves_missing_points(self):
        schema = {"names": ["left", "right", "center"], "flip_idx": [1, 0, 2]}
        row = {"shape_type": "keypoints", "points": [2, 1, 5, 4], "keypoints": [
            {"x": 2, "y": 1, "visibility": 2}, {"x": 5, "y": 4, "visibility": 1}, {"x": 0, "y": 0, "visibility": 0}]}
        _, rows, _ = self.apply([{"type": "horizontal_flip"}], [row], task="pose", schema=schema)
        self.assertEqual(rows[0]["keypoints"], [{"x": 7, "y": 4, "visibility": 1}, {"x": 10, "y": 1, "visibility": 2}, {"x": 0, "y": 0, "visibility": 0}])
        _, rows, _ = self.apply([{"type": "rotate90", "degrees": [90]}], [row], task="pose", schema=schema)
        self.assertEqual(rows[0]["keypoints"][0], {"x": 1, "y": 10, "visibility": 2})
        self.assertEqual(rows[0]["keypoints"][2], {"x": 0, "y": 0, "visibility": 0})
        with self.assertRaisesRegex(ValueError, "flip_idx"):
            self.apply([{"type": "horizontal_flip"}], [row], task="pose", schema={"names": schema["names"]})

    def test_classification_preserves_label_without_geometry(self):
        row = {"shape_type": "classification", "label_id": 7, "points": [], "keypoints": []}
        image, rows, _ = self.apply([{"type": "rotate90", "degrees": [90]}, {"type": "grayscale"}], [row], task="classify")
        self.assertEqual(rows, [row]); self.assertEqual(image.size, (8, 12))
        pixels = np.asarray(image); np.testing.assert_array_equal(pixels[:, :, 0], pixels[:, :, 1])

    def test_photometric_changes_preserve_geometry_and_are_reproducible(self):
        operations = [{"type": "brightness", "min_factor": .7, "max_factor": 1.3},
                      {"type": "contrast"}, {"type": "saturation"}, {"type": "blur", "radius": .5}, {"type": "noise", "std": 8}]
        first = self.apply(operations); second = self.apply(operations)
        self.assertEqual(first[0].tobytes(), second[0].tobytes()); self.assertEqual(first[1:], second[1:])
        self.assertEqual(first[1], [self.box]); self.assertNotEqual(first[0].tobytes(), self.image.tobytes())
        self.assertNotEqual(first[0].tobytes(), self.apply(operations, variant=2)[0].tobytes())
        self.assertNotEqual(first[0].tobytes(), self.apply(operations, seed=43)[0].tobytes())
        self.assertNotEqual(first[2]["derived_seed"], self.apply(operations, source="18:sha")[2]["derived_seed"])
        self.assertTrue(all(item["applied"] for item in first[2]["operations"]))

    def test_probability_misses_are_explicit_for_deduplication(self):
        image, rows, metadata = self.apply([{"type": "noise", "probability": 1e-12}])
        self.assertEqual(image.tobytes(), self.image.tobytes()); self.assertEqual(rows, [self.box])
        self.assertEqual(metadata["applied"], []); self.assertFalse(metadata["operations"][0]["applied"])

    def test_invalid_parameters_and_geometry_fail_before_generation(self):
        for operations in [[{"type": "noise", "std": float("nan")}], [{"type": "brightness", "min_factor": 2, "max_factor": 1}],
                           [{"type": "rotate90", "degrees": [45]}], [{"type": "blur", "radius": 999}],
                           [{"type": "horizontal_flip", "std": 1}], [{"type": "grayscale", "probability": 0}],
                           [{"type": "grayscale"}, {"type": "grayscale"}], [{"type": "unrecognized"}]]:
            with self.subTest(operations=operations), self.assertRaises(ValueError): self.apply(operations)
        with self.assertRaises(ValueError): self.apply([{"type": "grayscale"}], [{**self.box, "points": [-1, 1, 5, 4]}])


class DatasetAugmentationPhotometricTest(unittest.TestCase):
    NEW_OPERATIONS = ["gamma", "hue", "sharpness", "autocontrast", "equalize", "jpeg_compression", "downscale"]

    def setUp(self):
        # A bounded, colorful texture exercises histogram changes, local detail,
        # JPEG artifacts and resolution loss without relying on external images.
        y, x = np.indices((48, 64))
        pixels = np.stack((35 + (x * 7 + y * 3) % 170,
                           45 + (x * 2 + y * 9) % 150,
                           55 + (x * 11 + y * 5) % 130), axis=2).astype(np.uint8)
        self.image = Image.fromarray(pixels)
        self.box = {"shape_type": "rectangle", "points": [3, 4, 31, 27], "label_id": 2,
                    "attributes": {"review": {"approved": True}}, "confidence": .9}

    def apply(self, operations, *, image=None, rows=None, task="detect", schema=None,
              seed=42, variant=1, source="photo-source", **config):
        return augment_image(image if image is not None else self.image,
                             [self.box] if rows is None else rows,
                             {"seed": seed, "operations": operations, **config},
                             task=task, keypoint_schema=schema or {}, source_key=source, variant_index=variant)

    def test_each_new_method_changes_pixels_and_preserves_all_four_task_annotations(self):
        operations = [{"type": "gamma", "min_gamma": 1.8, "max_gamma": 1.8},
                      {"type": "hue", "min_shift": .2, "max_shift": .2},
                      {"type": "sharpness", "min_factor": 0, "max_factor": 0},
                      {"type": "autocontrast", "cutoff": 1}, {"type": "equalize"},
                      {"type": "jpeg_compression", "min_quality": 20, "max_quality": 20},
                      {"type": "downscale", "min_scale": .3, "max_scale": .3}]
        fixtures = [
            ("detect", self.box, {}),
            ("segment", {**self.box, "shape_type": "polygon", "points": [3, 4, 31, 4, 31, 27, 3, 27]}, {}),
            ("classify", {**self.box, "shape_type": "classification", "points": [], "keypoints": []}, {}),
            ("pose", {**self.box, "shape_type": "keypoints", "keypoints": [
                {"x": 3, "y": 4, "visibility": 2}, {"x": 31, "y": 27, "visibility": 1},
                {"x": 0, "y": 0, "visibility": 0}]}, {"names": ["left", "right", "missing"]}),
        ]
        original_pixels = self.image.tobytes()
        for operation in operations:
            for task, row, schema in fixtures:
                with self.subTest(operation=operation["type"], task=task):
                    original = copy.deepcopy(row)
                    result, rows, metadata = self.apply([operation], rows=[row], task=task, schema=schema)
                    self.assertNotEqual(result.tobytes(), original_pixels)
                    self.assertEqual(result.mode, "RGB"); self.assertEqual(result.size, self.image.size)
                    self.assertEqual(rows, [original]); self.assertEqual(row, original)
                    self.assertIsNot(rows[0], row)
                    self.assertIsNot(rows[0]["attributes"], row["attributes"])
                    self.assertEqual(metadata["input_size"], metadata["output_size"])
                    self.assertEqual(metadata["operations"], [{**metadata["applied"][0], "applied": True}])
                    self.assertEqual(metadata["applied"][0]["type"], operation["type"])
        self.assertEqual(self.image.tobytes(), original_pixels)

    def test_defaults_replay_exactly_and_record_real_parameters(self):
        ranges = {"gamma": ("gamma", .8, 1.2), "hue": ("shift", -.05, .05),
                  "sharpness": ("factor", .5, 1.5), "autocontrast": ("cutoff", 1, 1),
                  "jpeg_compression": ("quality", 60, 95), "downscale": ("scale", .5, .9)}
        for kind in self.NEW_OPERATIONS:
            with self.subTest(kind=kind):
                operations = [{"type": kind}]
                first = self.apply(operations); second = self.apply(operations)
                self.assertEqual(first[0].tobytes(), second[0].tobytes())
                self.assertEqual(first[1:], second[1:])
                actual = first[2]["applied"][0]
                if kind in ranges:
                    key, low, high = ranges[kind]
                    self.assertGreaterEqual(actual[key], low); self.assertLessEqual(actual[key], high)
                if kind == "jpeg_compression": self.assertIs(type(actual["quality"]), int)
                if kind == "hue": self.assertIs(type(actual["hue_offset"]), int)
                if kind == "downscale":
                    self.assertEqual(actual["reduced_size"], [round(64 * actual["scale"]), round(48 * actual["scale"])])
                self.assertEqual(operations, [{"type": kind}], "Normalization must not mutate callers' recipes")
        operations = [{"type": kind} for kind in self.NEW_OPERATIONS]
        first = self.apply(operations)
        for kwargs in ({"seed": 43}, {"variant": 2}, {"source": "another-image"}):
            changed = self.apply(operations, **kwargs)
            self.assertNotEqual(first[2]["derived_seed"], changed[2]["derived_seed"])
            self.assertNotEqual(first[0].tobytes(), changed[0].tobytes())

    def test_gamma_direction_and_endpoints(self):
        image = Image.new("RGB", (1, 1), (0, 128, 255))
        dark, _, actual = self.apply([{"type": "gamma", "min_gamma": 2, "max_gamma": 2}], image=image, rows=[])
        self.assertEqual(dark.getpixel((0, 0)), (0, 64, 255))
        self.assertEqual(actual["applied"][0]["gamma"], 2)
        light, _, _ = self.apply([{"type": "gamma", "min_gamma": .5, "max_gamma": .5}], image=image, rows=[])
        self.assertEqual(light.getpixel((0, 0)), (0, 181, 255))

    def test_hue_wraps_in_both_directions_without_changing_grays(self):
        red = Image.new("RGB", (1, 1), (255, 0, 0))
        outputs = []
        for shift in (-.5, .5):
            result, _, metadata = self.apply([{"type": "hue", "min_shift": shift, "max_shift": shift}], image=red, rows=[])
            outputs.append(result.tobytes())
            r, g, b = result.getpixel((0, 0))
            self.assertEqual(r, 0); self.assertGreater(g, 250); self.assertGreater(b, 250)
            self.assertEqual(metadata["applied"][0]["hue_offset"], round(shift * 256))
        self.assertEqual(*outputs)
        gray = Image.new("RGB", (3, 2), (105, 105, 105))
        result, _, _ = self.apply([{"type": "hue", "min_shift": .2, "max_shift": .2}], image=gray, rows=[])
        self.assertEqual(result.tobytes(), gray.tobytes())

    def test_neutral_settings_and_tiny_resolution_are_safe(self):
        for kind, parameter, value in (("gamma", "gamma", 1), ("hue", "shift", 0),
                                        ("sharpness", "factor", 1), ("downscale", "scale", 1)):
            with self.subTest(kind=kind):
                result, _, _ = self.apply([{"type": kind, f"min_{parameter}": value, f"max_{parameter}": value}])
                self.assertEqual(result.tobytes(), self.image.tobytes())
        tiny = Image.new("RGB", (1, 2), (150, 80, 30))
        result, _, metadata = self.apply([{"type": "downscale", "min_scale": .1, "max_scale": .1}], image=tiny, rows=[])
        self.assertEqual(result.size, tiny.size)
        self.assertEqual(metadata["applied"][0]["reduced_size"], [1, 1])

    def test_jpeg_quality_limits_are_inclusive_and_affect_fidelity(self):
        errors = []
        for quality in (10, 100):
            result, _, metadata = self.apply([{"type": "jpeg_compression", "min_quality": quality, "max_quality": quality}])
            self.assertEqual(metadata["applied"][0]["quality"], quality)
            errors.append(np.mean((np.asarray(result, dtype=float) - np.asarray(self.image, dtype=float)) ** 2))
        self.assertGreater(errors[0], errors[1])

    def test_new_operations_probability_misses_are_recorded_without_changes(self):
        result, rows, metadata = self.apply([{"type": kind, "probability": 1e-12} for kind in self.NEW_OPERATIONS])
        self.assertEqual(result.tobytes(), self.image.tobytes()); self.assertEqual(rows, [self.box])
        self.assertEqual(metadata["applied"], [])
        self.assertEqual(metadata["operations"], [{"type": kind, "applied": False} for kind in self.NEW_OPERATIONS])

    def test_numeric_bounds_are_inclusive_and_invalid_pairs_are_rejected(self):
        parameters = [("gamma", "gamma", .2, 3), ("hue", "shift", -.5, .5),
                      ("sharpness", "factor", 0, 3), ("downscale", "scale", .1, 1),
                      ("jpeg_compression", "quality", 10, 100)]
        for kind, name, low, high in parameters:
            for value in (low, high):
                with self.subTest(kind=kind, valid=value):
                    self.apply([{"type": kind, f"min_{name}": value, f"max_{name}": value}])
            invalid = [low - 1, high + 1, float("nan"), float("inf"), True, "1", None, 10 ** 400]
            if kind == "jpeg_compression": invalid.append(60.0)
            for field in (f"min_{name}", f"max_{name}"):
                for value in invalid:
                    with self.subTest(kind=kind, field=field, invalid=value), self.assertRaises(ValueError):
                        self.apply([{"type": kind, field: value}])
            with self.subTest(kind=kind, inverted=True), self.assertRaises(ValueError):
                self.apply([{"type": kind, f"min_{name}": high, f"max_{name}": low}])
        for cutoff in (0, 10): self.apply([{"type": "autocontrast", "cutoff": cutoff}])
        for cutoff in (-.01, 10.01, float("nan"), float("inf"), True, "1", None):
            with self.subTest(cutoff=cutoff), self.assertRaises(ValueError): self.apply([{"type": "autocontrast", "cutoff": cutoff}])
        for kind in self.NEW_OPERATIONS:
            with self.subTest(kind=kind, unknown=True), self.assertRaises(ValueError): self.apply([{"type": kind, "unknown": 1}])
            with self.subTest(kind=kind, duplicated=True), self.assertRaises(ValueError): self.apply([{"type": kind}, {"type": kind}])
        with self.assertRaises(ValueError): self.apply([{"type": "equalize", "cutoff": 1}])

    def test_all_sixteen_methods_can_be_composed(self):
        self.assertEqual(len(OPERATION_TYPES), 16)
        _, rows, metadata = self.apply([{"type": kind} for kind in sorted(OPERATION_TYPES)])
        self.assertEqual(len(metadata["applied"]), 16)
        self.assertEqual(rows[0]["label_id"], self.box["label_id"])

    def test_global_target_does_not_change_seed_or_override_per_image_limits(self):
        operation = [{"type": "noise"}]
        baseline = self.apply(operation)
        for target in (None, 1, 5000):
            result = self.apply(operation, copies_per_image=20, target_count=target)
            self.assertEqual(result[0].tobytes(), baseline[0].tobytes())
            self.assertEqual(result[1:], baseline[1:])
        result = self.apply(operation, copies_per_image=20, target_count=5000, variant=20)
        self.assertEqual(result[2]["variant_index"], 20)
        self.assertNotEqual(result[0].tobytes(), baseline[0].tobytes())
        for field, invalids in (("copies_per_image", (0, 21, 1.0, True, "2", None)),
                               ("target_count", (0, 5001, 1.0, True, "2")),
                               ("variant", (0, 21, 1.0, True, "2", None))):
            for value in invalids:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.apply(operation, **{field: value})
        with self.assertRaises(ValueError): self.apply(operation, unsupported_count=5)

    def test_legacy_v1_recipe_keeps_pre_extension_pixels_seed_and_random_draws(self):
        # Golden output captured before the seven additional operations existed.
        pixels = np.zeros((8, 12, 3), dtype=np.uint8); pixels[1:4, 2:5] = (240, 100, 30)
        kinds = ["horizontal_flip", "vertical_flip", "rotate90", "brightness", "contrast", "saturation", "grayscale", "blur", "noise"]
        result, _, metadata = self.apply([{"type": kind} for kind in kinds], image=Image.fromarray(pixels),
                                          rows=[], source="17:sha", copies_per_image=5)
        self.assertEqual(hashlib.sha256(result.tobytes()).hexdigest(), "75b97979814637cb42757e99d113d217d17e387c340d6d10d2ca889eddc76992")
        self.assertEqual(metadata["algorithm_version"], "visionforge-augmentation-v1")
        self.assertEqual(metadata["derived_seed"], "298b4885d515c1f9")
        self.assertEqual(metadata["output_size"], [8, 12])
        self.assertEqual(metadata["applied"][2]["degrees"], 270)
        self.assertEqual([item["factor"] for item in metadata["applied"] if "factor" in item],
                         [0.8689784869793076, 1.0702549578681075, 0.9864302578763661])


if __name__ == "__main__": unittest.main()
