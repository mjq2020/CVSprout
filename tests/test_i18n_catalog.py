"""Catalog completeness checks run with the ordinary Python test suite."""
import ast
import json
from pathlib import Path
import re
import unittest

from scripts.build_locales import compiled_catalog, compiled_api_rules, compiled_api_module, python_messages

ROOT = Path(__file__).resolve().parents[1]
HAN = re.compile(r"[\u3400-\u9fff]")
CALL = re.compile(r'\bt(?:a)?\(\s*("(?:\\.|[^"\\])*")')


class CatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads((ROOT / "static/locales/en.json").read_text())

    def test_browser_bundle_matches_shared_catalog(self):
        self.assertEqual((ROOT / "static/locales/en.js").read_text(), compiled_catalog())

    def test_api_rules_are_current(self):
        self.assertEqual((ROOT / "static/locales/api.json").read_text(), compiled_api_rules())
        self.assertEqual((ROOT / "static/locales/api.js").read_text(), compiled_api_module())
        for source in json.loads(compiled_api_rules())["templates"]:
            self.assertIn(source, self.catalog)

    def test_all_frontend_messages_have_translations(self):
        missing = []
        for path in [*(ROOT / "static").glob("*.js"), ROOT / "app/runtime/index.html"]:
            for match in CALL.finditer(path.read_text()):
                source = json.loads(match.group(1))
                if source not in self.catalog:
                    missing.append((str(path.relative_to(ROOT)), source))
        self.assertFalse(missing, missing)

    def test_system_messages_and_model_catalog_have_translations(self):
        missing = []
        for path in (ROOT / "app").rglob("*.py"):
            for source in python_messages(ast.parse(path.read_text())):
                if source not in self.catalog:
                    missing.append((str(path.relative_to(ROOT)), source))

        def check(value):
            if isinstance(value, str) and HAN.search(value) and value not in self.catalog:
                missing.append(("model-catalog/models.json", value))
            elif isinstance(value, list):
                for item in value: check(item)
            elif isinstance(value, dict):
                for item in value.values(): check(item)

        check(json.loads((ROOT / "model-catalog/models.json").read_text()))
        self.assertFalse(missing, missing)

    def test_english_is_complete_and_keeps_interpolation_slots(self):
        for source, target in self.catalog.items():
            with self.subTest(source=source):
                self.assertTrue(target.strip())
                self.assertFalse(HAN.search(target), target)
                self.assertEqual(sorted(re.findall(r"\{\d+\}", source)), sorted(re.findall(r"\{\d+\}", target)))
