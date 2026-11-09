from __future__ import annotations

import importlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image


class ProjectAccessTest(unittest.TestCase):
    def setUp(self):
        from app.config import settings
        from app.database import init_db, db, now_iso
        from app.accounts import create_user, UserCreate
        from app.auth import Actor
        self.settings, self.db, self.now = settings, db, now_iso
        self.temp = tempfile.TemporaryDirectory(prefix="mdp-project-access-")
        values = dict(data_dir=Path(self.temp.name), auth_mode="accounts", cookie_secure=False,
                      public_origin="", embedded_worker=False, embedded_pipeline_worker=False)
        self.previous = {key: getattr(settings, key) for key in values}
        for key, value in values.items():
            object.__setattr__(settings, key, value)
        self.addCleanup(self.restore)
        init_db()
        app = importlib.import_module("app.main").app
        self.clients, self.users = {}, {}
        for index, (name, role) in enumerate((("root", "admin"), ("alice", "user"), ("bob", "user"), ("carol", "user"))):
            user = create_user(UserCreate(username=name, display_name=name, password="temporary-test-password-123", role=role), Actor("fixture", "admin"))
            with db() as conn:
                conn.execute("UPDATE users SET must_change_password=0 WHERE id=?", (user["id"],))
            client = TestClient(app, headers={"X-MDP-Client": "browser"}, client=(f"192.0.2.{index+1}", 50000))
            self.addCleanup(client.close)
            response = client.post("/api/login", json={"username": name, "password": "temporary-test-password-123"})
            self.assertEqual(response.status_code, 200, response.text)
            self.clients[name], self.users[name] = client, user
        self.alice, self.bob, self.root = (self.clients[name] for name in ("alice", "bob", "root"))
        self.project = self.create(self.alice, "Alice private")
        self.other = self.create(self.alice, "Alice second")
        self.bobs = self.create(self.bob, "Bob private")

    def restore(self):
        for key, value in self.previous.items():
            object.__setattr__(self.settings, key, value)
        self.temp.cleanup()

    def create(self, client, name):
        response = client.post("/api/projects", json={"name": name, "task_type": "detect", "labels": ["object"]})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def share(self, role="editor", can_export=False):
        response = self.alice.put(f"/api/projects/{self.project['id']}/members", json={"username": "bob", "role": role, "can_export": can_export})
        self.assertEqual(response.status_code, 200, response.text)

    def ids(self, client, query=""):
        response = client.get("/api/projects" + query)
        self.assertEqual(response.status_code, 200, response.text)
        return {p["id"] for p in response.json()}

    def upload(self):
        content = io.BytesIO()
        Image.new("RGB", (64, 48), "red").save(content, "PNG")
        response = self.alice.post(f"/api/projects/{self.project['id']}/assets", files={"files": ("image.png", content.getvalue(), "image/png")})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["asset_ids"][0]

    def test_private_workspaces_and_single_project_sharing(self):
        self.assertEqual(self.ids(self.alice), {self.project["id"], self.other["id"]})
        self.assertEqual(self.ids(self.bob), {self.bobs["id"]})
        self.assertEqual(len(self.ids(self.root, "?scope=all")), 3)
        self.assertEqual(self.bob.get("/api/projects?scope=all").status_code, 403)
        self.assertEqual(self.bob.get(f"/api/projects/{self.project['id']}").status_code, 404)
        self.share()
        self.assertEqual(self.ids(self.bob, "?scope=mine"), {self.bobs["id"]})
        self.assertEqual(self.ids(self.bob, "?scope=shared"), {self.project["id"]})
        self.assertEqual(self.bob.get(f"/api/projects/{self.other['id']}").status_code, 404)
        detail = self.bob.get(f"/api/projects/{self.project['id']}").json()
        self.assertEqual(detail["project_role"], "editor")
        self.assertEqual(detail["permissions"], {"edit": True, "manage": False, "export": True})
        base = f"/api/projects/{self.project['id']}"
        self.assertEqual(self.bob.put(base + "/members", json={"username": "carol"}).status_code, 403)
        self.assertEqual(self.bob.get(base + "/members").status_code, 403)
        self.assertEqual(self.bob.patch(base + "/archive", json={"archived": True}).status_code, 403)
        self.assertEqual(self.bob.request("DELETE", base, json={"confirmation_name": self.project["name"]}).status_code, 403)
        self.assertEqual(self.bob.post(base + "/labels", json={"name": "new-label", "color": "#123456"}).status_code, 201)

    def test_indirect_resources_and_downloads_are_scoped(self):
        asset_id = self.upload()
        project_id = self.project["id"]
        label = self.alice.get(f"/api/projects/{project_id}").json()["labels"][0]["id"]
        annotation = self.alice.post(f"/api/assets/{asset_id}/annotations", json={"label_id": label, "shape_type": "rectangle", "points": [3, 3, 30, 30]})
        self.assertEqual(annotation.status_code, 201, annotation.text)
        version = self.alice.post(f"/api/projects/{project_id}/versions", json={"approved_only": False, "train_ratio": 1, "valid_ratio": 0, "test_ratio": 0})
        self.assertEqual(version.status_code, 201, version.text)
        version_id = version.json()["id"]
        with self.db() as conn:
            job_id = conn.execute("INSERT INTO jobs(project_id,model_id,mode,prompt,status,created_at) VALUES (?,'demo','auto','','completed',?)", (project_id, self.now())).lastrowid
            run_id = conn.execute("""INSERT INTO training_runs(project_id,version_id,name,model_arch,total_epochs,created_by,created_at)
                VALUES (?,?,'private run','test',1,'alice',?)""", (project_id, version_id, self.now())).lastrowid
            conversion_id = conn.execute("""INSERT INTO model_conversions(training_run_id,format,created_by,created_at)
                VALUES (?,'onnx','alice',?)""", (run_id, self.now())).lastrowid
        paths = [f"/api/assets/{asset_id}" + suffix for suffix in ("", "/image", "/thumbnail", "/annotations", "/review-events")]
        paths += [f"/api/jobs/{job_id}", f"/api/versions/{version_id}", f"/api/versions/{version_id}/assets",
                  f"/api/versions/{version_id}/export/json", f"/api/projects/{project_id}/export/json"]
        paths += [f"/api/training-runs/{run_id}" + suffix for suffix in ("", "/logs", "/metrics", "/artifact", "/deployment", "/conversions", "/conversion-options")]
        paths += [f"/api/conversions/{conversion_id}" + suffix for suffix in ("", "/logs", "/artifact", "/manifest")]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.bob.get(path).status_code, 404)
        self.assertEqual(self.bob.patch(f"/api/annotations/{annotation.json()['id']}", json={}).status_code, 404)
        self.assertEqual(self.bob.delete(f"/api/labels/{label}").status_code, 404)
        self.share("viewer")
        self.assertEqual(self.bob.get(f"/api/assets/{asset_id}/image").status_code, 200)
        self.assertEqual(self.bob.get(f"/api/versions/{version_id}").status_code, 200)
        for path in (f"/api/assets/{asset_id}/lock", f"/api/projects/{project_id}/jobs", f"/api/projects/{project_id}/assets"):
            self.assertEqual(self.bob.post(path, json={}).status_code, 403)
        for path in (f"/api/projects/{project_id}/export/json", f"/api/versions/{version_id}/export/json", f"/api/training-runs/{run_id}/artifact"):
            self.assertEqual(self.bob.get(path).status_code, 403)
        self.share("viewer", can_export=True)
        self.assertEqual(self.bob.get(f"/api/projects/{project_id}/export/json").status_code, 200)
        self.assertEqual(self.bob.get(f"/api/versions/{version_id}/export/json").status_code, 200)

    def test_revocation_and_downgrade_release_work_and_cancel_ai(self):
        asset_id = self.upload()
        base = f"/api/projects/{self.project['id']}"
        self.share()
        self.assertEqual(self.alice.post(base + "/work-blocks", json={}).status_code, 201)
        claimed = self.bob.post(base + "/work-blocks/claim", json={})
        self.assertEqual(claimed.status_code, 200, claimed.text)
        self.assertEqual(self.bob.post(f"/api/assets/{asset_id}/lock").status_code, 200)
        with self.db() as conn:
            job = conn.execute("""INSERT INTO jobs(project_id,model_id,mode,prompt,status,settings_json,created_at)
                VALUES (?,'demo','auto','','queued',?,?)""", (self.project["id"], json.dumps({"block_owner_key": f"user:{self.users['bob']['id']}"}), self.now())).lastrowid
        self.share("viewer")
        with self.db() as conn:
            self.assertIsNone(conn.execute("SELECT lock_owner FROM assets WHERE id=?", (asset_id,)).fetchone()[0])
            self.assertIsNone(conn.execute("SELECT owner_user_id FROM work_blocks WHERE id=?", (claimed.json()["id"],)).fetchone()[0])
            self.assertEqual(conn.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job,)).fetchone()[0], 1)
        self.assertEqual(self.bob.post(f"/api/assets/{asset_id}/lock").status_code, 403)
        self.assertEqual(self.alice.delete(base + f"/members/{self.users['bob']['id']}").status_code, 200)
        self.assertEqual(self.bob.get(base).status_code, 404)
        self.assertEqual(self.bob.get(f"/api/assets/{asset_id}/thumbnail").status_code, 404)
        self.assertEqual(self.ids(self.bob, "?scope=shared"), set())

    def test_archive_and_owner_assignment_keep_isolation(self):
        base = f"/api/projects/{self.project['id']}"
        self.assertEqual(self.alice.patch(base + "/archive", json={"archived": True}).status_code, 200)
        self.assertIn(self.project["id"], self.ids(self.alice, "?scope=mine&archived=true"))
        self.assertNotIn(self.project["id"], self.ids(self.bob, "?archived=true"))
        self.assertEqual(self.bob.get(base).status_code, 404)
        self.assertEqual(self.alice.put(base + "/owner", json={"username": "bob"}).status_code, 403)
        self.assertEqual(self.root.put(base + "/owner", json={"username": "bob"}).status_code, 200)
        self.assertEqual(self.alice.get(base).status_code, 404)
        self.assertEqual(self.bob.get(base).json()["project_role"], "owner")

    def test_membership_never_grants_global_administration(self):
        self.share()
        for path in ("/api/users", "/api/registration-requests", "/api/edge/devices", "/api/model-downloads"):
            self.assertEqual(self.bob.get(path).status_code, 403, path)
        self.assertEqual(self.alice.delete("/api/models/other-model").status_code, 403)

    def test_access_revoked_during_upload_cannot_commit_files(self):
        self.share()
        main = importlib.import_module("app.main")
        unpack = main.unpack_upload

        async def unpack_then_revoke(upload):
            stored = await unpack(upload)
            response = self.alice.delete(f"/api/projects/{self.project['id']}/members/{self.users['bob']['id']}")
            self.assertEqual(response.status_code, 200, response.text)
            return stored

        content = io.BytesIO()
        Image.new("RGB", (32, 24), "blue").save(content, "PNG")
        with patch.object(main, "unpack_upload", unpack_then_revoke):
            response = self.bob.post(f"/api/projects/{self.project['id']}/assets", files={"files": ("revoked.png", content.getvalue(), "image/png")})
        self.assertEqual(response.status_code, 404, response.text)
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 0)
        self.assertEqual(list(self.settings.media_dir.iterdir()), [])

    def test_cached_owner_cannot_manage_after_transfer(self):
        from app.auth import Actor, get_actor
        main = importlib.import_module("app.main")
        session = self.alice.get("/api/session").json()
        session_id = self.alice.get("/api/account/sessions").json()["current_session_id"]
        cached = Actor(session["actor"], "user", user_id=self.users["alice"]["id"], session_id=session_id,
                       project_id=self.project["id"], project_role="owner")
        base = f"/api/projects/{self.project['id']}"
        self.assertEqual(self.root.put(base + "/owner", json={"username": "bob"}).status_code, 200)
        main.app.dependency_overrides[get_actor] = lambda: cached
        try:
            self.assertEqual(self.alice.patch(base + "/archive", json={"archived": True}).status_code, 404)
            self.assertEqual(self.alice.post(base + "/labels", json={"name": "unauthorized", "color": "#123456"}).status_code, 404)
            self.assertEqual(self.alice.put(base + "/members", json={"username": "carol"}).status_code, 404)
        finally:
            main.app.dependency_overrides.pop(get_actor, None)
        self.assertIsNone(self.bob.get(base).json()["archived_at"])


if __name__ == "__main__":
    unittest.main()
