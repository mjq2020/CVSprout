from __future__ import annotations

import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


class TrainingInitializationTest(unittest.TestCase):
    def setUp(self):
        from app.config import settings
        from app.database import db, one, now_iso
        from app.main import app
        self.settings, self.db, self.one, self.now = settings, db, one, now_iso
        self.training = importlib.import_module('app.training')
        self.registry = importlib.import_module('app.training_models')
        self.temp = tempfile.TemporaryDirectory(prefix='cvsprout-training-init-')
        root = Path(self.temp.name)
        changes = dict(data_dir=root/'data', models_dir=root/'models', auth_mode='disabled',
                       embedded_worker=False, embedded_pipeline_worker=False, worker_url='', training_enabled=True,
                       training_base_model=root/'custom'/'absent.pt', training_segmentation_model=root/'custom'/'absent-seg.pt',
                       training_model_dirs_raw=str(root/'custom'))
        self.previous = {key: getattr(settings, key) for key in changes}
        for key, value in changes.items(): object.__setattr__(settings, key, value)
        self.client = TestClient(app, headers={'X-MDP-Client': 'browser'}).__enter__()
        self.backend = patch.object(self.training, '_backend_info', return_value={'installed':True,'name':'ultralytics','version':'test'}); self.backend.start()
        self.availability = patch.object(self.training, 'training_availability', return_value={'available':True,'backend':'ultralytics','backend_version':'test'}); self.availability.start()
        self.resources = patch.object(self.training, 'training_resources', return_value={'cuda_available':False,'devices':[{'id':'cpu','available':True}]}); self.resources.start()
        with db() as conn:
            self.project = conn.execute('INSERT INTO projects(name,task_type,created_at,updated_at) VALUES (?,?,?,?)', ('Initialization','detect',now_iso(),now_iso())).lastrowid
            stats = {'task_type':'detect','shape_types':{'rectangle':2},'splits':{'train':{'asset_count':1,'annotation_count':1},'valid':{'asset_count':1,'annotation_count':1}}}
            self.version = conn.execute('''INSERT INTO dataset_versions(project_id,version_number,name,train_ratio,valid_ratio,test_ratio,content_hash,
                asset_count,annotation_count,task_type,stats_json,created_by,created_at) VALUES (?,1,'Frozen',0.5,0.5,0,'data-hash',2,2,'detect',?,'test',?)''', (self.project,json.dumps(stats),now_iso())).lastrowid
        self.official = self.registry.get_builtin_training_model('yolo11n.pt')
        self.assertIsNotNone(self.official)

    def tearDown(self):
        self.resources.stop(); self.availability.stop(); self.backend.stop()
        self.client.__exit__(None,None,None)
        for key,value in self.previous.items(): object.__setattr__(self.settings,key,value)
        self.temp.cleanup()

    def create(self, **values):
        body = {'version_id':self.version,'model_arch':'yolo11n.pt','task':'detect','device':'cpu','epochs':1,'workers':0,'amp':False,**values}
        return self.client.post(f'/api/projects/{self.project}/training-runs',json=body)

    def claim(self, **values):
        response = self.create(**values); self.assertEqual(response.status_code,202,response.text)
        run_id = response.json()['id']
        with self.db() as conn:
            conn.execute("UPDATE training_runs SET status='running',claimed_by='test-worker',claim_generation=1 WHERE id=?",(run_id,))
        return run_id

    def row(self, run_id): return self.one('SELECT * FROM training_runs WHERE id=?',(run_id,))

    def test_missing_official_models_are_selectable_without_network_or_weight_probing(self):
        with patch.object(self.registry,'ensure_pretrained_model',side_effect=AssertionError('catalog downloads')), patch.object(self.training,'_checkpoint_probe',side_effect=AssertionError('missing weights probed')):
            models = self.training.model_catalog()
        model = next(item for item in models if item['id']=='yolo11n.pt')
        self.assertTrue(model['available']); self.assertFalse(model['installed']); self.assertFalse(model['loadable'])
        self.assertTrue(model['supports_scratch']); self.assertEqual(model['source'],'builtin')
        self.assertEqual(model['sha256'],self.official['sha256'])

    def test_standard_import_directory_is_scanned_and_local_same_name_wins(self):
        path=self.settings.models_dir/'training'/'yolo11n.pt';path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'custom-same-name')
        with patch.object(self.training,'_checkpoint_probe',return_value={'task':'segment','loadable':True,'parameters':1,'trainable_parameters':1,'error':''}):
            models=self.training.model_catalog()
            item=next(model for model in models if model['id']==path.name)
            self.assertEqual(item['source'],'local');self.assertEqual(item['task'],'segment')
            self.assertIsNone(self.training._resolve_base_model(path.name,'detect'))
        self.assertIn(path.resolve(),self.training._model_candidates())

    def test_create_and_retry_freeze_pretrained_download_without_fetching_it(self):
        with patch.object(self.registry,'ensure_pretrained_model',side_effect=AssertionError('API downloads')):
            response=self.create();self.assertEqual(response.status_code,202,response.text);run=response.json()
            config=run['settings'];self.assertIs(config['pretrained'],True)
            snapshot=config['model_initialization']['builtin']
            for key in ('url','sha256','size_bytes','architecture','relative_path'):self.assertEqual(snapshot[key],self.official[key])
            self.assertFalse(self.official['path'].exists())
            with self.db() as conn:conn.execute("UPDATE training_runs SET status='failed' WHERE id=?",(run['id'],))
            retry=self.client.post(f"/api/training-runs/{run['id']}/retry",json={})
            self.assertEqual(retry.status_code,202,retry.text);self.assertEqual(retry.json()['config_hash'],run['config_hash'])

    def test_scratch_freezes_architecture_without_checkpoint_hash(self):
        response=self.create(pretrained=False);self.assertEqual(response.status_code,202,response.text)
        config=response.json()['settings'];self.assertIsNone(config['base_model_sha256']);self.assertEqual(config['base_model_size_bytes'],0)
        self.assertFalse(config['pretrained']);self.assertEqual(config['model_initialization']['architecture'],'yolo11n.yaml')
        self.assertEqual(self.create(model_arch='https://example.invalid/a.pt').status_code,422)
        self.assertEqual(self.create(model_arch='../a.pt').status_code,422)
        self.assertEqual(self.create(download_url='https://example.invalid/a.pt').status_code,422)

    def test_custom_scratch_requires_reconstructible_architecture(self):
        path=self.settings.models_dir/'training'/'custom.pt';path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'custom')
        probe={'task':'detect','loadable':True,'parameters':1,'trainable_parameters':1,'error':'','supports_scratch':False}
        with patch.object(self.training,'_checkpoint_probe',return_value=probe):
            self.assertEqual(self.create(model_arch='custom.pt',pretrained=False).status_code,409)
            self.assertEqual(self.create(model_arch='custom.pt').status_code,202)
            probe.update(supports_scratch=True,architecture={'backbone':[[-1,1,'Conv',[8,3,2]]],'head':[[[-1],1,'Detect',[1]]]})
            result=self.create(model_arch='custom.pt',pretrained=False);self.assertEqual(result.status_code,202,result.text)
            self.assertEqual(result.json()['settings']['model_initialization']['source'],'local')

    def test_worker_prepares_and_reports_download_before_leasing_device(self):
        run_id=self.claim();events=[]
        def download(model,progress,cancelled):
            events.append('download');cancelled();progress(64,model['size_bytes'])
            path=self.official['path'];path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'checked')
            progress(model['size_bytes'],model['size_bytes']);return path
        def resource(*args):events.append('resource');raise self.training.ResourceBusy('busy')
        with patch.object(self.registry,'ensure_pretrained_model',side_effect=download),patch.object(self.training,'_file_sha256',return_value=self.official['sha256']),patch.object(self.training,'_checkpoint_probe',return_value={'task':'detect'}),patch.object(self.training,'_acquire_resource_lease',side_effect=resource):
            self.training.run_training(run_id,'test-worker',1)
        self.assertEqual(events,['download','resource']);row=self.row(run_id);self.assertEqual(row['status'],'queued')
        progress=json.loads(row['resource_json']);self.assertEqual(progress['downloaded_bytes'],self.official['size_bytes'])
        self.assertEqual(self.one("SELECT COUNT(*) AS n FROM training_run_logs WHERE run_id=? AND event='model_download_completed'",(run_id,))['n'],1)

    def test_download_cancel_and_stale_claim_never_acquire_device(self):
        for lost in (False,True):
            with self.subTest(lost=lost):
                run_id=self.claim()
                def download(model,progress,cancelled):
                    with self.db() as conn:
                        if lost:conn.execute("UPDATE training_runs SET claimed_by='other',claim_generation=2 WHERE id=?",(run_id,))
                        else:conn.execute('UPDATE training_runs SET cancel_requested=1 WHERE id=?',(run_id,))
                    if cancelled():raise self.registry.TrainingModelDownloadCancelled('cancelled')
                    raise AssertionError('cancel callback returned')
                with patch.object(self.registry,'ensure_pretrained_model',side_effect=download),patch.object(self.training,'_acquire_resource_lease') as resource:
                    self.training.run_training(run_id,'test-worker',1);resource.assert_not_called()
                self.assertEqual(self.row(run_id)['status'],'running' if lost else 'cancelled')
                if lost:self.assertEqual(self.row(run_id)['claimed_by'],'other')

    def test_download_cancellation_keeps_shared_state_interrupted(self):
        from app import model_downloads
        for via_progress in (False, True):
            with self.subTest(via_progress=via_progress):
                run_id=self.claim()
                def download(item, report, cancelled):
                    report(64)
                    with self.db() as conn:
                        conn.execute('UPDATE training_runs SET cancel_requested=1 WHERE id=?',(run_id,))
                    if via_progress:report(128)
                    else:model_downloads.check_cancelled(cancelled)
                    raise AssertionError('cancel was ignored')
                with patch.object(model_downloads,'download_file',side_effect=download),patch.object(self.training,'_acquire_resource_lease') as resource:
                    self.training.run_training(run_id,'test-worker',1)
                    resource.assert_not_called()
                self.assertEqual(self.row(run_id)['status'],'cancelled')
                state=json.loads((model_downloads.state_dir()/f"{self.official['download_id']}.json").read_text())
                self.assertEqual(state['status'],'interrupted')
                self.assertEqual(state['downloaded'],128 if via_progress else 64)

    def test_network_failure_does_not_override_concurrent_cancel_or_takeover(self):
        for lost in (False, True):
            with self.subTest(lost=lost):
                run_id=self.claim()
                def download(*args, **kwargs):
                    with self.db() as conn:
                        if lost:conn.execute("UPDATE training_runs SET claimed_by='other',claim_generation=2 WHERE id=?",(run_id,))
                        else:conn.execute('UPDATE training_runs SET cancel_requested=1 WHERE id=?',(run_id,))
                    raise OSError('network interrupted')
                with patch.object(self.registry,'ensure_pretrained_model',side_effect=download),patch.object(self.training,'_acquire_resource_lease') as resource:
                    self.training.run_training(run_id,'test-worker',1)
                    resource.assert_not_called()
                self.assertEqual(self.row(run_id)['status'],'running' if lost else 'cancelled')

    def test_download_failure_and_tampered_configuration_fail_before_gpu(self):
        for tampered in (False,True):
            with self.subTest(tampered=tampered):
                run_id=self.claim()
                if tampered:
                    config=json.loads(self.row(run_id)['settings_json']);config['seed']=123
                    with self.db() as conn:conn.execute('UPDATE training_runs SET settings_json=? WHERE id=?',(json.dumps(config),run_id))
                with patch.object(self.registry,'ensure_pretrained_model',side_effect=OSError('network unavailable')) as download,patch.object(self.training,'_acquire_resource_lease') as resource:
                    self.training.run_training(run_id,'test-worker',1);resource.assert_not_called()
                    self.assertEqual(download.call_count,0 if tampered else 1)
                self.assertEqual(self.row(run_id)['status'],'failed')

    def test_scratch_and_resume_never_download_official_weights(self):
        for scratch,resuming in ((True,False),(False,True)):
            run_id=self.claim(pretrained=not scratch);run=self.row(run_id);config=json.loads(run['settings_json'])
            with patch.object(self.registry,'ensure_pretrained_model',side_effect=AssertionError('unexpected weights download')):
                self.assertIsNone(self.training._prepare_training_model(run,config,'test-worker',1,resuming=resuming))

    def test_fresh_scratch_model_constructs_yaml_and_keeps_selected_custom_scale(self):
        calls=[];module=types.ModuleType('ultralytics');module.YOLO=lambda source,**kwargs:calls.append((source,kwargs)) or object()
        directory=Path(self.temp.name)
        with patch.dict(sys.modules,{'ultralytics':module}):
            config={'pretrained':False,'model_initialization':{'architecture':'yolo11n.yaml'}}
            self.training._fresh_training_model(config,None,directory,'detect')
            self.assertEqual(calls[-1][0],'yolo11n.yaml')
            architecture={'backbone':[[-1,1,'Conv',[8,3,2]]],'head':[[[-1],1,'Detect',[1]]],'scales':{'m':[1,1,512]},'scale':'m'}
            config['model_initialization']['architecture']=architecture
            self.training._fresh_training_model(config,None,directory,'detect')
            self.assertEqual(json.loads(Path(calls[-1][0]).read_text())['scales'],{'m':[1,1,512]})
            self.assertTrue(all(not str(source).endswith('.pt') for source,_ in calls))

    def test_custom_scratch_normalizes_explicit_and_default_torchvision_weights(self):
        architecture={'nc':2,'backbone':[[-1,1,'TorchVision',[512,'resnet18','DEFAULT',True,2]]],
                      'head':[[-1,1,'TorchVision',[512,'resnet18']],[-1,1,'Classify',[2]]]}
        with patch.object(self.training,'_torchvision_scratch_supported',return_value=True):
            frozen=self.training._scratch_architecture(architecture)
            self.assertIsNone(frozen['backbone'][0][3][2])
            self.assertEqual(frozen['head'][0][3],[512,'resnet18',None])
            self.assertEqual(architecture['backbone'][0][3][2],'DEFAULT')
            self.assertEqual(self.training._scratch_architecture(frozen),frozen)
            ultralytics=types.ModuleType('ultralytics');ultralytics.YOLO=lambda *args,**kwargs:self.fail('unsafe architecture was constructed')
            with patch.dict(sys.modules,{'ultralytics':ultralytics}),self.assertRaisesRegex(RuntimeError,'预训练骨干'):
                self.training._fresh_training_model({'pretrained':False,'model_initialization':{'architecture':architecture}},None,Path(self.temp.name),'classify')
        with patch.object(self.training,'_torchvision_scratch_supported',return_value=False):
            self.assertIsNone(self.training._scratch_architecture(architecture))

    def test_torchvision_implicit_pretrained_backbone_disables_scratch(self):
        def classification(*, weights=None):pass
        def detection(*, weights=None, weights_backbone='IMAGENET'):pass
        def legacy_detection(*, pretrained=False, pretrained_backbone=True):pass
        def no_pretrained_default(*, weights=None, weights_backbone=None):pass
        builders={'classification':classification,'detection':detection,'legacy':legacy_detection,'empty':no_pretrained_default}
        models=types.ModuleType('torchvision.models');models.get_model_builder=builders.__getitem__
        torchvision=types.ModuleType('torchvision');torchvision.models=models
        with patch.dict(sys.modules,{'torchvision':torchvision,'torchvision.models':models}):
            self.assertTrue(self.training._torchvision_scratch_supported('classification'))
            self.assertTrue(self.training._torchvision_scratch_supported('empty'))
            self.assertFalse(self.training._torchvision_scratch_supported('detection'))
            self.assertFalse(self.training._torchvision_scratch_supported('legacy'))

    def test_custom_scratch_freezes_only_the_actual_checkpoint_scale(self):
        architecture={'backbone':[[-1,1,'Conv',[32,3,2]]],'head':[[[-1],1,'Detect',[1]]],
                      'scales':{'n':[0.25,0.25,512],'s':[0.5,0.5,512],'m':[1,1,512]}}
        for selected in ('s','m'):
            with self.subTest(scale=selected):
                frozen=self.training._scratch_architecture({**architecture,'scale':selected})
                self.assertEqual(frozen['scales'],{selected:architecture['scales'][selected]})
                self.assertEqual(frozen['scale'],selected)
        self.assertEqual(len(architecture['scales']),3)


if __name__=='__main__':unittest.main()
