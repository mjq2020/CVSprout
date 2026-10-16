from __future__ import annotations

import asyncio
import hashlib
import importlib
import io
import json
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from app.config import settings
from app.database import all_rows, db, now_iso, one
from app.jobs import claim_next_job, run_annotation_job
from app.providers.base import Prediction, normalize_predictions
from app.providers.demo import DemoProvider
from app.training import _build_artifact_metadata, _prepare_training_dataset

main = importlib.import_module("app.main")


class CollaborationTaskTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mdp-regression-")
        values = dict(data_dir=Path(self.temp.name), embedded_worker=False,
                      embedded_pipeline_worker=False, enable_demo_model=True, auth_mode="disabled")
        self.previous = {key:getattr(settings,key) for key in values}
        for key,value in values.items():
            object.__setattr__(settings,key,value)
        self.client = TestClient(main.app, headers={"X-MDP-Client": "browser"}).__enter__()

    def tearDown(self):
        self.client.__exit__(None,None,None)
        for key,value in self.previous.items():
            object.__setattr__(settings,key,value)
        self.temp.cleanup()

    def fixture(self, task="mixed", count=2):
        payload = {"name":f"{task} project", "task_type":task, "labels":["object","other"]}
        if task == "pose":
            payload["keypoint_schema"] = {"names":["tip","base"],"skeleton":[[0,1]],"flip_idx":[1,0]}
        response = self.client.post("/api/projects",json=payload)
        self.assertEqual(response.status_code,201,response.text)
        project = self.client.get(f"/api/projects/{response.json()['id']}").json()
        assets=[]
        for index in range(count):
            image=io.BytesIO()
            Image.new("RGB",(100,80),(20+index*20,40,60)).save(image,"PNG")
            result=self.client.post(f"/api/projects/{project['id']}/assets",files={"files":("image.png",image.getvalue(),"image/png")})
            assets.append(result.json()["asset_ids"][0])
        return project,assets

    def annotation(self,project,asset,**extra):
        task=project["task_type"]
        shape={"shape_type":"rectangle","points":[10,10,50,60]}
        if task == "segment":
            shape={"shape_type":"polygon","points":[10,10,50,10,50,60,10,60]}
        elif task == "classify":
            shape={"shape_type":"classification","points":[]}
        elif task == "pose":
            shape={"shape_type":"keypoints","points":[10,10,50,60],"keypoints":[{"x":20,"y":20,"visibility":2},{"x":30,"y":30,"visibility":1}]}
        response=self.client.post(f"/api/assets/{asset}/annotations",json={"label_id":project["labels"][0]["id"],**shape,**extra})
        self.assertEqual(response.status_code,201,response.text)
        return response.json()

    def job(self,project,assets,model="demo"):
        return self.client.post(f"/api/projects/{project['id']}/jobs",json={"model_id":model,"prompt":"object","asset_ids":assets})

    def approve(self,asset,client=None):
        client=client or self.client
        for action in ["submit","approve"]:
            revision=client.get(f"/api/assets/{asset}").json()["revision"]
            response=client.post(f"/api/assets/{asset}/workflow",json={"action":action,"expected_revision":revision})
            self.assertEqual(response.status_code,200,response.text)

    def test_archive_restore_preserves_data_and_new_empty_projects(self):
        project, assets = self.fixture(count=4)
        annotation = self.annotation(project, assets[0])
        empty = self.client.post("/api/projects", json={"name": "New empty project"}).json()
        listed = self.client.get("/api/projects").json()
        preview = next(row for row in listed if row["id"] == project["id"])
        self.assertEqual(preview["preview_asset_ids"], list(reversed(assets))[:3])
        self.assertEqual(preview["asset_count"], 4)
        url = f"/api/projects/{project['id']}/archive"
        self.assertEqual(self.client.patch(url, json={"archived": True}).status_code, 200)
        self.assertEqual([row["id"] for row in self.client.get("/api/projects").json()], [empty["id"]])
        self.assertEqual([row["id"] for row in self.client.get("/api/projects?archived=true").json()], [project["id"]])
        self.assertEqual(len(self.client.get(f"/api/projects/{project['id']}/assets").json()), 4)
        self.assertEqual(self.client.get(f"/api/assets/{assets[0]}/annotations").json()[0]["id"], annotation["id"])
        self.assertEqual(self.client.get(f"/api/assets/{assets[0]}/image").status_code, 200)
        self.assertEqual(self.client.patch(url, json={"archived": False}).status_code, 200)
        self.assertEqual(len(self.client.get("/api/projects").json()), 2)
        self.assertEqual(self.client.get("/api/projects?archived=true").json(), [])
        self.assertEqual(self.client.patch("/api/projects/9999/archive", json={"archived": True}).status_code, 404)

    def test_expired_cancel_does_not_strand_project(self):
        project,assets=self.fixture()
        job=self.job(project,assets).json()["id"]
        self.assertEqual(claim_next_job("lost-worker"),job)
        self.client.post(f"/api/jobs/{job}/cancel")
        with db() as conn:
            conn.execute("UPDATE jobs SET lease_expires_at='2000-01-01' WHERE id=?",(job,))
        self.assertIsNone(claim_next_job("new-worker"))
        self.assertEqual(self.client.get(f"/api/jobs/{job}").json()["status"],"cancelled")
        self.assertEqual(self.client.post(f"/api/assets/{assets[0]}/lock").status_code,200)
        self.client.delete(f"/api/assets/{assets[0]}/lock")
        second=self.job(project,assets).json()["id"]
        claim_next_job("lost-worker")
        with db() as conn:
            conn.execute("UPDATE jobs SET lease_expires_at='2000-01-01' WHERE id=?",(second,))
        self.assertEqual(self.client.post(f"/api/jobs/{second}/cancel").json()["status"],"cancelled")

    def test_retry_conflicts_but_disjoint_images_run_in_parallel(self):
        project,assets=self.fixture(count=3)
        first=self.job(project,[assets[0]]).json()["id"]
        self.client.post(f"/api/jobs/{first}/cancel")
        active=self.job(project,[assets[0]]).json()["id"]
        self.assertEqual(self.client.post(f"/api/jobs/{first}/retry").status_code,409)
        second=self.job(project,[assets[1]])
        self.assertEqual(second.status_code,202,second.text)
        self.assertEqual({claim_next_job("a"),claim_next_job("b")},{active,second.json()["id"]})
        self.assertEqual(self.client.post(f"/api/assets/{assets[2]}/lock").status_code,200)
        self.assertEqual(self.job(project,[assets[2]]).status_code,409)

    def test_ai_guards_all_write_endpoints_only_on_target_images(self):
        project,assets=self.fixture()
        item=self.annotation(project,assets[0])
        job=self.job(project,[assets[0]])
        self.assertEqual(job.status_code,202,job.text)
        self.assertEqual(self.client.post(f"/api/assets/{assets[0]}/annotations",json={"label_id":item["label_id"],"shape_type":"rectangle","points":[1,1,20,20]}).status_code,409)
        self.assertEqual(self.client.patch(f"/api/annotations/{item['id']}",json={"expected_version":1,"points":[1,1,20,20]}).status_code,409)
        self.assertEqual(self.client.delete(f"/api/annotations/{item['id']}?expected_version=1").status_code,409)
        self.assertEqual(self.client.post(f"/api/assets/{assets[0]}/workflow",json={"action":"submit","expected_revision":1}).status_code,409)
        self.annotation(project,assets[1])

    def test_annotation_update_cannot_overwrite_concurrent_approval(self):
        project,assets=self.fixture(count=1)
        item=self.annotation(project,assets[0])
        ready,resume=threading.Event(),threading.Event()
        original=main._validate_shape
        def delayed(*args,**kwargs):
            original(*args,**kwargs); ready.set()
            self.assertTrue(resume.wait(10))
        with patch.object(main,"_validate_shape",delayed), ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.client.patch,f"/api/annotations/{item['id']}",json={"expected_version":1,"points":[12,12,52,62]})
            self.assertTrue(ready.wait(10))
            try:
                self.approve(assets[0])
            finally:
                resume.set()
            self.assertEqual(future.result(timeout=10).status_code,409)
        self.assertEqual(self.client.get(f"/api/assets/{assets[0]}").json()["review_status"],"approved")

    def test_human_corrections_survive_ai_replacement_and_retry_is_idempotent(self):
        project,assets=self.fixture()
        first=self.job(project,assets).json()["id"]
        claim_next_job("worker")
        calls=[]
        async def fail_on_second(*args,**kwargs):
            calls.append(1)
            if len(calls)==2:
                raise RuntimeError("temporary provider failure")
            return [Prediction("object","rectangle",[10,10,50,60],.9)]
        with patch.object(DemoProvider,"predict",fail_on_second):
            asyncio.run(run_annotation_job(first,"worker"))
            self.assertEqual(claim_next_job("worker"),first)
            asyncio.run(run_annotation_job(first,"worker"))
        self.assertEqual(len(calls),3)
        item=self.client.get(f"/api/assets/{assets[0]}/annotations").json()[0]
        changed=self.client.patch(f"/api/annotations/{item['id']}",json={"expected_version":1,"points":[12,12,45,50]}).json()
        self.assertTrue(changed["human_modified"])
        second=self.job(project,[assets[0]]).json()["id"]
        claim_next_job("worker")
        asyncio.run(run_annotation_job(second,"worker"))
        items=self.client.get(f"/api/assets/{assets[0]}/annotations").json()
        self.assertIn(item["id"],[value["id"] for value in items])
        self.assertEqual(next(value for value in items if value["id"]==item["id"])["points"],[12,12,45,50])

    def test_classification_and_pose_ai_snapshots_exports_and_training_inputs(self):
        for task in ["classify","pose"]:
            project,assets=self.fixture(task,count=4)
            model_id=f"remote-{task}"
            response=self.client.post("/api/models",json={"id":model_id,"name":task,"family":"test","provider_type":"remote_classification" if task=="classify" else "remote_keypoints","endpoint":"http://127.0.0.1/predict","capabilities":[task],"modes":["auto"]})
            self.assertEqual(response.status_code,201,response.text)
            raw={"label":"object","confidence":.95}
            if task=="pose":
                raw.update(bbox=[100,100,500,600],keypoints=[[200,200,2],[300,300,1]])
            predictions=normalize_predictions({"objects":[raw]},100,80,"normalized_1000",task)
            from app.providers.remote import RemoteVisionProvider
            async def predict(*args,**kwargs): return predictions
            job=self.job(project,assets,model_id).json()["id"]
            claim_next_job("worker")
            with patch.object(RemoteVisionProvider,"predict",predict):
                asyncio.run(run_annotation_job(job,"worker"))
            self.assertEqual(self.client.get(f"/api/jobs/{job}").json()["created_count"],4)
            for asset in assets:
                self.approve(asset)
            version=self.client.post(f"/api/projects/{project['id']}/versions",json={"train_ratio":.5,"valid_ratio":.5,"test_ratio":0}).json()
            self.assertEqual(version["stats"]["task_type"],task)
            from app.training import _version_compatibility
            self.assertTrue(_version_compatibility(one("SELECT * FROM dataset_versions WHERE id=?",(version["id"],)),task)["compatible"])
            exported=self.client.get(f"/api/versions/{version['id']}/export/yolo")
            self.assertEqual(exported.status_code,200,exported.text)
            with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
                if task=="classify":
                    self.assertIn("train/0000_object/",archive.namelist())
                    self.assertEqual(json.loads(archive.read("classes.json"))[0]["name"],"object")
                else:
                    self.assertIn("kpt_shape: [2, 3]",archive.read("data.yaml").decode())
                    line=archive.read(next(name for name in archive.namelist() if name.startswith("labels/") and name.endswith(".txt"))).decode()
                    self.assertEqual(len(line.split()),11)
                    self.assertEqual(line.split()[-1],"1")
            directory=Path(self.temp.name)/f"train-{task}"
            directory.mkdir()
            data=_prepare_training_dataset(1,version["id"],directory,task)
            self.assertTrue(data.is_dir() if task=="classify" else data.is_file())
            live=self.client.get(f"/api/projects/{project['id']}/export/yolo")
            self.assertEqual(live.status_code,200,live.text)
            for fmt in (["json"] if task=="classify" else ["json","coco"]):
                response=self.client.get(f"/api/versions/{version['id']}/export/{fmt}")
                self.assertEqual(response.status_code,200,response.text)
            self.assertFalse(self.client.get(f"/api/projects/{project['id']}/quality").json().get("tiny_annotation_count"))

    def test_task_geometry_and_classification_uniqueness(self):
        project,assets=self.fixture("classify",count=1)
        self.annotation(project,assets[0])
        duplicate=self.client.post(f"/api/assets/{assets[0]}/annotations",json={"label_id":project["labels"][1]["id"],"shape_type":"classification"})
        self.assertEqual(duplicate.status_code,409)
        invalid=self.client.post(f"/api/assets/{assets[0]}/annotations",json={"label_id":project["labels"][0]["id"],"shape_type":"rectangle","points":[1,1,2,2]})
        self.assertEqual(invalid.status_code,422)
        project,assets=self.fixture("pose",count=1)
        invalid=self.client.post(f"/api/assets/{assets[0]}/annotations",json={"label_id":project["labels"][0]["id"],"shape_type":"keypoints","points":[1,1,50,50],"keypoints":[{"x":10,"y":10,"visibility":2}]})
        self.assertEqual(invalid.status_code,422)

    def test_corrupt_training_artifact_is_not_downloadable(self):
        project,assets=self.fixture(count=1)
        self.annotation(project,assets[0])
        version=self.client.post(f"/api/projects/{project['id']}/versions",json={"approved_only":False}).json()["id"]
        path=settings.training_dir/"best.pt"
        path.write_bytes(b"checkpoint")
        metadata=_build_artifact_metadata(path)
        with db() as conn:
            run=conn.execute("""INSERT INTO training_runs (project_id,version_id,name,model_arch,status,total_epochs,artifact_path,artifact_metadata_json,created_by,created_at) VALUES (?,?,'test','test.pt','completed',1,?,?,'test',?)""",(project["id"],version,str(path),json.dumps(metadata),now_iso())).lastrowid
        self.assertEqual(self.client.get(f"/api/training-runs/{run}/artifact").status_code,200)
        path.write_bytes(b"corrupted!")
        self.assertEqual(self.client.get(f"/api/training-runs/{run}/artifact").status_code,409)
        details=self.client.get(f"/api/training-runs/{run}").json()
        self.assertFalse(details["artifact"]["available"])
        self.assertEqual(details["artifact"]["integrity"],"failed")

    def test_independent_accounts_roles_sessions_and_collaborative_locks(self):
        project,assets=self.fixture()
        for username,role in [("admin","admin"),("alice","annotator"),("bob","annotator"),("review","reviewer")]:
            response=self.client.post("/api/users",json={"username":username,"display_name":username,"password":"test-password-123","role":role})
            self.assertEqual(response.status_code,201,response.text)
        object.__setattr__(settings,"auth_mode","accounts")
        self.assertEqual(self.client.get("/api/projects").status_code,401)
        clients=[TestClient(main.app, headers={"X-MDP-Client": "browser"}) for _ in range(4)]
        admin,alice,bob,review=clients
        for client,username in zip(clients,["admin","alice","bob","review"]):
            result=client.post("/api/login",json={"username":username,"password":"test-password-123"})
            self.assertEqual(result.status_code,200,result.text)
            self.assertIn("HttpOnly",result.headers["set-cookie"])
            self.assertTrue(result.json()["must_change_password"])
            self.assertEqual(client.get("/api/projects").status_code,403)
            changed=client.post("/api/account/password",json={"current_password":"test-password-123","new_password":"personal-password-123"})
            self.assertEqual(changed.status_code,200,changed.text)
            self.assertFalse(changed.json()["must_change_password"])
        self.assertNotEqual(alice.get("/api/session").json()["actor"],bob.get("/api/session").json()["actor"])
        self.assertEqual(alice.get("/api/users").status_code,403)
        self.assertEqual(alice.post("/api/projects",json={"name":"forbidden"}).status_code,403)
        for client in (alice, review):
            self.assertEqual(client.patch(f"/api/projects/{project['id']}/archive", json={"archived": True}).status_code, 403)
            self.assertEqual(client.get("/api/projects?archived=true").status_code, 403)
        with patch.object(main,"get_provider",return_value=DemoProvider()), patch.object(main,"get_model",return_value={"provider_type":"local_grounding_dino","tasks":["detect"]}):
            response=alice.post(f"/api/projects/{project['id']}/jobs",json={"model_id":"grounding-dino","prompt":"new-class","target_labels":["new-class"],"asset_ids":[assets[0]]})
            self.assertEqual(response.status_code,403,response.text)
        self.assertNotIn("new-class",[label["name"] for label in admin.get(f"/api/projects/{project['id']}").json()["labels"]])
        self.assertEqual(alice.post(f"/api/projects/{project['id']}/training-runs",json={"version_id":1}).status_code,403)
        self.assertEqual(alice.post(f"/api/assets/{assets[0]}/lock").status_code,200)
        self.assertEqual(bob.post(f"/api/assets/{assets[0]}/lock").status_code,423)
        self.assertEqual(bob.post(f"/api/assets/{assets[1]}/lock").status_code,200)
        item=alice.post(f"/api/assets/{assets[0]}/annotations",json={"label_id":project["labels"][0]["id"],"shape_type":"rectangle","points":[1,1,20,20]}).json()
        result=alice.post(f"/api/assets/{assets[0]}/workflow",json={"action":"submit","expected_revision":item["asset_revision"]})
        self.assertEqual(result.status_code,200,result.text)
        payload={"action":"approve","expected_revision":result.json()["revision"]}
        self.assertEqual(alice.post(f"/api/assets/{assets[0]}/workflow",json=payload).status_code,403)
        self.assertEqual(review.post(f"/api/assets/{assets[0]}/workflow",json=payload).status_code,200)
        self.assertEqual(alice.post(f"/api/assets/{assets[0]}/lock").status_code,409)
        revision=review.get(f"/api/assets/{assets[0]}").json()["revision"]
        self.assertEqual(alice.post(f"/api/assets/{assets[0]}/workflow",json={"action":"reopen","expected_revision":revision}).status_code,403)
        self.assertEqual(alice.post(f"/api/projects/{project['id']}/versions",json={}).status_code,403)
        users=admin.get("/api/users").json()
        bob_id=next(user["id"] for user in users if user["username"]=="bob")
        self.assertEqual(admin.patch(f"/api/users/{bob_id}",json={"enabled":False}).status_code,200)
        self.assertEqual(bob.get("/api/projects").status_code,401)
        token=alice.cookies.get("mdp_session")
        alice.delete("/api/session")
        alice.cookies.set("mdp_session",token)
        self.assertEqual(alice.get("/api/projects").status_code,401)
        for client in clients:
            client.close()

    def test_login_throttle_password_reset_and_last_admin_protection(self):
        response=self.client.post("/api/users",json={"username":"admin","display_name":"Admin","password":"test-password-123","role":"admin"})
        user_id=response.json()["id"]
        object.__setattr__(settings,"auth_mode","accounts")
        credentials={"username":"admin","password":"test-password-123"}
        for _ in range(8):
            self.assertEqual(self.client.post("/api/login",json={**credentials,"password":"wrong"}).status_code,401)
        self.assertEqual(self.client.post("/api/login",json=credentials).status_code,429)
        with db() as conn:
            conn.execute("UPDATE login_attempts SET window_start=0")
        self.assertEqual(self.client.post("/api/login",json=credentials).status_code,200)
        changed=self.client.post("/api/account/password",json={"current_password":credentials["password"],"new_password":"personal-password-123"})
        self.assertEqual(changed.status_code,200,changed.text)
        credentials["password"]="personal-password-123"
        self.assertEqual(self.client.patch(f"/api/users/{user_id}",json={"role":"annotator"}).status_code,409)
        self.assertEqual(self.client.patch(f"/api/users/{user_id}",json={"enabled":False}).status_code,409)
        self.assertEqual(self.client.patch(f"/api/users/{user_id}",json={"password":"replacement-password-123"}).status_code,400)
        self.assertEqual(self.client.post("/api/users",json={"username":"secondadmin","display_name":"Second admin","password":"test-password-123","role":"admin"}).status_code,201)
        secondadmin=TestClient(main.app,headers={"X-MDP-Client":"browser"})
        self.assertEqual(secondadmin.post("/api/login",json={"username":"secondadmin","password":"test-password-123"}).status_code,200)
        self.assertEqual(secondadmin.post("/api/account/password",json={"current_password":"test-password-123","new_password":"personal-password-123"}).status_code,200)
        self.assertEqual(secondadmin.patch(f"/api/users/{user_id}",json={"password":"replacement-password-123"}).status_code,200)
        secondadmin.close()
        self.assertEqual(self.client.get("/api/session").status_code,401)
        self.assertEqual(self.client.post("/api/login",json=credentials).status_code,401)
        self.assertEqual(self.client.post("/api/login",json={**credentials,"password":"replacement-password-123"}).status_code,200)
        self.assertEqual(self.client.get("/api/projects").status_code,403)
        self.assertTrue(self.client.get("/api/session").json()["must_change_password"])

    def test_json_export_media_paths_and_pose_task_mismatch(self):
        project,assets=self.fixture("pose",count=2)
        for asset in assets:
            self.annotation(project,asset)
        version=self.client.post(f"/api/projects/{project['id']}/versions",json={"approved_only":False}).json()
        response=self.client.get(f"/api/versions/{version['id']}/export/json")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            for asset in json.loads(archive.read("annotations.json"))["assets"]:
                self.assertIn(asset["file_name"],archive.namelist())
        from app.dataset_versions import build_version_export
        with self.assertRaises(ValueError):
            build_version_export(version["id"],"yolo",Path(self.temp.name)/"invalid.zip",yolo_task="detect")


if __name__ == "__main__":
    unittest.main()
