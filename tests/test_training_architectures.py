from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_training_initialization as fixtures


class TrainingArchitectureTest(unittest.TestCase):
    setUp = fixtures.TrainingInitializationTest.setUp
    tearDown = fixtures.TrainingInitializationTest.tearDown
    create = fixtures.TrainingInitializationTest.create
    row = fixtures.TrainingInitializationTest.row

    def completed_rtdetr(self):
        response = self.create(model_arch='rtdetr-l.pt', image_size=256)
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()['id']
        path = self.settings.training_dir / f'run-{run_id}' / 'best.pt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'isolated-artifact')
        metadata = {'model_backend': 'rtdetr', 'sha256': self.training._file_sha256(path), 'size_bytes': path.stat().st_size}
        with self.db() as conn:
            conn.execute("UPDATE training_runs SET status='completed',artifact_path=?,artifact_metadata_json=? WHERE id=?",
                         (str(path), json.dumps(metadata), run_id))
        return run_id

    def formats(self):
        return {'conversion_formats': {name: {'available': True, 'reason': '', 'label': name}
                                       for name in ('onnx', 'torchscript', 'engine', 'rknn')}}

    def test_expanded_catalog_preserves_four_tasks_and_large_model_defaults(self):
        models = {model['id']: model for model in self.training.model_catalog()}
        for family in ('yolov8', 'yolo11', 'yolo26'):
            for suffix in ('', '-seg', '-cls', '-pose'):
                batches = []
                for scale in ('n', 's', 'm', 'l', 'x'):
                    model = models[f'{family}{scale}{suffix}.pt']
                    self.assertTrue(model['available'])
                    self.assertFalse(model['installed'])
                    batches.append(model['defaults']['batch_size'])
                self.assertEqual(batches, [16, 8, 4, 2, 1])
        for scale, batch in (('l', 2), ('x', 1)):
            model = models[f'rtdetr-{scale}.pt']
            self.assertEqual(model['model_backend'], 'rtdetr')
            self.assertEqual(model['constraints'], {'min_image_size': 128})
            self.assertEqual(model['defaults']['batch_size'], batch)
            self.assertFalse(model['defaults']['amp'])
            self.assertFalse(model['defaults']['deterministic'])
            self.assertTrue(model['warnings'])
        self.assertEqual(models['yolov5nu.pt']['family'], 'YOLOv5u')
        self.assertEqual(models['yolov9t.pt']['family'], 'YOLOv9')
        self.assertTrue(models['yolo12n.pt']['warnings'])

    def test_api_defaults_apply_only_to_omitted_parameters(self):
        body = {'version_id': self.version, 'model_arch': 'rtdetr-l.pt', 'device': 'cpu'}
        for explicit in (False, True):
            payload = {**body, **({'amp': True, 'deterministic': True, 'batch_size': 7} if explicit else {})}
            response = self.client.post(f'/api/projects/{self.project}/training-runs', json=payload)
            self.assertEqual(response.status_code, 202, response.text)
            config = response.json()['settings']
            self.assertEqual(config['model_initialization']['model_backend'], 'rtdetr')
            self.assertEqual(config['amp'], explicit)
            self.assertEqual(config['deterministic'], explicit)
            self.assertEqual(config['batch_size'], 7 if explicit else 2)
        response = self.create(model_arch='yolo26x.pt')
        self.assertEqual(response.json()['settings']['batch_size'], 1)

    def test_rtdetr_training_rejects_too_few_spatial_candidates(self):
        for size in (32, 64, 96):
            response = self.create(model_arch='rtdetr-l.pt', image_size=size)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn('128', response.text)
        self.assertEqual(self.create(model_arch='rtdetr-l.pt', image_size=128).status_code, 202)
        self.assertEqual(self.create(model_arch='yolov8n.pt', image_size=64).status_code, 202)

    def test_rtdetr_scratch_selects_specialized_model_for_builtin_and_custom_yaml(self):
        calls = []
        module = types.ModuleType('ultralytics')
        module.YOLO = lambda *args, **kwargs: self.fail('RT-DETR scratch used generic YOLO model')
        module.RTDETR = lambda source: calls.append(source) or object()
        custom = {'nc': 1, 'backbone': [[-1, 1, 'Conv', [16, 3, 2]]],
                  'head': [[[-1], 1, 'RTDETRDecoder', [1]]]}
        with patch.dict(sys.modules, {'ultralytics': module}):
            for architecture in ('rtdetr-l.yaml', custom):
                config = {'pretrained': False, 'model_initialization': {'architecture': architecture}}
                self.training._fresh_training_model(config, None, Path(self.temp.name), 'detect')
        self.assertEqual(calls[0], 'rtdetr-l.yaml')
        self.assertEqual(json.loads(Path(calls[1]).read_text()), custom)

    def test_checkpoint_backend_detection_uses_actual_head(self):
        rtdetr_head = type('RTDETRDecoder', (), {})()
        model = types.SimpleNamespace(model=types.SimpleNamespace(model=[rtdetr_head]))
        self.assertEqual(self.training._checkpoint_model_backend(model), 'rtdetr')
        model.model.model = [type('Detect', (), {})()]
        self.assertEqual(self.training._checkpoint_model_backend(model), 'yolo')

    def test_legacy_run_backend_is_inspected_and_filename_is_not_trusted(self):
        path = self.settings.training_dir / 'run-legacy' / 'ordinary-name.pt'
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'fixture')
        run = {'artifact_path': str(path), 'model_arch': 'ordinary-name.pt', 'settings_json': '{}'}
        with patch.object(self.training, '_checkpoint_probe', return_value={'model_backend': 'rtdetr'}):
            self.assertEqual(self.training._run_model_backend(run), 'rtdetr')
        run['model_arch'] = 'rtdetr-l.pt'
        with patch.object(self.training, '_checkpoint_probe', return_value={'model_backend': 'yolo'}):
            self.assertEqual(self.training._run_model_backend(run), 'yolo')
        run['artifact_metadata_json'] = json.dumps({'model_backend': 'rtdetr'})
        with patch.object(self.training, '_checkpoint_probe', side_effect=AssertionError('new metadata was ignored')):
            self.assertEqual(self.training._run_model_backend(run), 'rtdetr')

    def test_conversion_options_and_create_enforce_rtdetr_limits(self):
        run_id = self.completed_rtdetr()
        availability = self.formats()
        with patch.object(self.training, 'training_availability', return_value=availability), patch.object(self.training.conversion_tools, 'calibration_assets', return_value=[]):
            options = self.client.get(f'/api/training-runs/{run_id}/conversion-options')
            self.assertEqual(options.status_code, 200, options.text)
            options = options.json()
            self.assertFalse(options['formats']['rknn']['available'])
            self.assertIn('RT-DETR', options['formats']['rknn']['reason'])
            self.assertEqual(options['constraints'], {'min_image_size': 128, 'dynamic': False})
            self.assertTrue(availability['conversion_formats']['rknn']['available'], 'shared environment catalog was mutated')
            for extra in ({'format': 'rknn', 'rknn': {'target_platform': 'rk3588'}}, {'image_size': 64}, {'dynamic': True}):
                payload = {'format': 'onnx', 'image_size': 128, 'device': 'cpu', **extra}
                response = self.client.post(f'/api/training-runs/{run_id}/conversions', json=payload)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn('RT-DETR', response.text)
            response = self.client.post(f'/api/training-runs/{run_id}/conversions', json={'format': 'onnx', 'image_size': 128, 'device': 'cpu'})
            self.assertEqual(response.status_code, 202, response.text)

    def test_worker_rechecks_legacy_rtdetr_conversion_before_leasing_device(self):
        run_id = self.completed_rtdetr()
        with patch.object(self.training, 'training_availability', return_value=self.formats()):
            response = self.client.post(f'/api/training-runs/{run_id}/conversions', json={'format': 'onnx', 'image_size': 128, 'device': 'cpu'})
        self.assertEqual(response.status_code, 202, response.text)
        conversion_id = response.json()['id']
        with self.db() as conn:
            conn.execute("UPDATE model_conversions SET format='rknn',status='running',claimed_by='test-worker',claim_generation=1 WHERE id=?", (conversion_id,))
        with patch.object(self.training, '_acquire_resource_lease') as acquire:
            self.training.run_conversion(conversion_id, 'test-worker', 1)
            acquire.assert_not_called()
        result = self.one('SELECT status,error FROM model_conversions WHERE id=?', (conversion_id,))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('RKNN', result['error'])


if __name__ == '__main__':
    unittest.main()
