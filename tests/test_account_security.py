from __future__ import annotations

import hashlib
import importlib
import json
import secrets
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

INITIAL = "initial-password-123"
PERSONAL = "personal-password-456"
REPLACEMENT = "replacement-password-789"


class AccountSecurityTest(unittest.TestCase):
    def setUp(self):
        # Delay app imports so discovery can initialize the legacy API suite's
        # explicit test-only environment before settings is instantiated.
        global UserCreate, check_password, create_user, Actor, settings
        global all_rows, db, init_db, now_iso, one, main
        from app.accounts import UserCreate, check_password, create_user
        from app.auth import Actor
        from app.config import settings
        from app.database import all_rows, db, init_db, now_iso, one
        main = importlib.import_module("app.main")
        self.temp = tempfile.TemporaryDirectory(prefix="mdp-account-security-")
        values = dict(data_dir=Path(self.temp.name), embedded_worker=False,
                      embedded_pipeline_worker=False, enable_demo_model=False,
                      auth_mode="accounts", cookie_secure=False)
        self.previous = {key: getattr(settings, key) for key in values}
        for key, value in values.items():
            object.__setattr__(settings, key, value)
        self.clients = []
        self.server = TestClient(main.app, headers={"X-MDP-Client": "browser"}).__enter__()
        self.admin_user = self.create("admin", "admin")
        self.admin = self.client()
        self.sign_in(self.admin, "admin", finish=True)

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.server.__exit__(None, None, None)
        for key, value in self.previous.items():
            object.__setattr__(settings, key, value)
        self.temp.cleanup()

    def client(self, ip="testclient", secure=False):
        client = TestClient(main.app, headers={"X-MDP-Client": "browser"}, client=(ip, 50000),
                            base_url="https://testserver" if secure else "http://testserver")
        self.clients.append(client)
        return client

    def create(self, username, role="annotator"):
        return create_user(UserCreate(username=username, display_name=username, password=INITIAL, role=role),
                           Actor("setup-cli", "admin"))

    def sign_in(self, client, username, password=INITIAL, finish=False):
        result = client.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(result.status_code, 200, result.text)
        if finish:
            result = client.post("/api/account/password", json={"current_password": password, "new_password": PERSONAL})
            self.assertEqual(result.status_code, 200, result.text)
        return result

    def asset(self):
        project = self.admin.post("/api/projects", json={"name": "session-lock-fixture"})
        self.assertEqual(project.status_code, 201, project.text)
        with db() as conn:
            cursor = conn.execute("""INSERT INTO assets
                (project_id,filename,storage_name,mime_type,width,height,size_bytes,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)""", (project.json()["id"], "fixture.png", secrets.token_hex(16),
                "image/png", 100, 80, 1, now_iso(), now_iso()))
            return cursor.lastrowid

    def test_initial_password_enforced_by_server_on_reads_and_writes(self):
        self.create("alice")
        client = self.client()
        initial = self.sign_in(client, "alice")
        self.assertTrue(initial.json()["must_change_password"])
        for method, path in (("get", "/api/projects"), ("get", "/api/models"), ("get", "/api/users"),
                             ("post", "/api/projects"), ("patch", "/api/account")):
            result = getattr(client, method)(path, **({"json": {"name": "blocked", "display_name": "blocked"}}
                                                     if method != "get" else {}))
            self.assertEqual(result.status_code, 403, result.text)
            self.assertEqual(result.json()["detail"]["code"], "password_change_required")
        for path in ("/api/session", "/api/account", "/api/account/sessions", "/api/account/events"):
            self.assertEqual(client.get(path).status_code, 200, path)
        old_token = client.cookies.get("mdp_session")
        for current, new in (("wrong", PERSONAL), (INITIAL, INITIAL)):
            response = client.post("/api/account/password", json={"current_password": current, "new_password": new})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(client.cookies.get("mdp_session"), old_token)
            self.assertEqual(client.get("/api/session").status_code, 200)
        changed = client.post("/api/account/password", json={"current_password": INITIAL, "new_password": PERSONAL})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertFalse(changed.json()["must_change_password"])
        self.assertNotEqual(client.cookies.get("mdp_session"), old_token)
        self.assertEqual(client.get("/api/projects").status_code, 200)
        replay = self.client(); replay.cookies.set("mdp_session", old_token)
        self.assertEqual(replay.get("/api/session").status_code, 401)

    def test_password_change_revokes_all_old_sessions_and_locks(self):
        user = self.create("alice")
        first, second = self.client(), self.client("192.0.2.2")
        self.sign_in(first, "alice", finish=True)
        self.sign_in(second, "alice", PERSONAL)
        asset = self.asset()
        self.assertEqual(second.post(f"/api/assets/{asset}/lock").status_code, 200)
        old_token = first.cookies.get("mdp_session")
        changed = first.post("/api/account/password", json={"current_password": PERSONAL, "new_password": REPLACEMENT})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertNotEqual(first.cookies.get("mdp_session"), old_token)
        self.assertEqual(second.get("/api/session").status_code, 401)
        self.assertIsNone(one("SELECT lock_owner FROM assets WHERE id=?", (asset,))["lock_owner"])
        self.assertEqual(one("SELECT COUNT(*) AS n FROM user_sessions WHERE user_id=?", (user["id"],))["n"], 1)
        self.assertEqual(first.get("/api/projects").status_code, 200)

    def test_session_ids_are_safe_and_revocation_is_scoped(self):
        alice = self.create("alice"); self.create("bob")
        first, second, bob = self.client(), self.client("192.0.2.2"), self.client("192.0.2.3")
        self.sign_in(first, "alice", finish=True)
        self.sign_in(second, "alice", PERSONAL)
        self.sign_in(bob, "bob", finish=True)
        sessions = first.get("/api/account/sessions").json()
        self.assertEqual(len(sessions["sessions"]), 2)
        hashes = {row["token_hash"] for row in all_rows("SELECT token_hash FROM user_sessions")}
        for row in sessions["sessions"]:
            self.assertEqual(len(row["id"]), 32)
            self.assertNotIn(row["id"], hashes)
            self.assertNotIn("token", json.dumps(row))
            self.assertTrue(row["created_at"] and row["last_seen_at"] and row["expires_at"])
        victim_id = next(row["id"] for row in sessions["sessions"] if not row["current"])
        self.assertEqual(bob.delete(f"/api/account/sessions/{victim_id}").status_code, 404)
        self.assertEqual(bob.get(f"/api/users/{alice['id']}/sessions").status_code, 403)
        self.assertEqual(bob.delete(f"/api/users/{alice['id']}/sessions").status_code, 403)
        asset = self.asset(); second.post(f"/api/assets/{asset}/lock")
        result = first.delete(f"/api/account/sessions/{victim_id}")
        self.assertEqual(result.json()["revoked_count"], 1)
        self.assertFalse(result.json()["current_session_revoked"])
        self.assertIsNone(one("SELECT lock_owner FROM assets WHERE id=?", (asset,))["lock_owner"])
        self.assertEqual(second.get("/api/session").status_code, 401)
        self.assertEqual(first.get("/api/session").status_code, 200)
        revoked = first.delete("/api/account/sessions?keep_current=false")
        self.assertTrue(revoked.json()["current_session_revoked"])
        self.assertEqual(first.get("/api/session").status_code, 401)

    def test_logout_releases_only_current_session_locks(self):
        self.create("alice")
        first, second = self.client(), self.client("192.0.2.2")
        self.sign_in(first, "alice", finish=True); self.sign_in(second, "alice", PERSONAL)
        a, b = self.asset(), self.asset()
        first.post(f"/api/assets/{a}/lock"); second.post(f"/api/assets/{b}/lock")
        self.assertEqual(first.delete("/api/session").status_code, 200)
        self.assertIsNone(one("SELECT lock_owner FROM assets WHERE id=?", (a,))["lock_owner"])
        self.assertIsNotNone(one("SELECT lock_owner FROM assets WHERE id=?", (b,))["lock_owner"])
        self.assertEqual(second.get("/api/session").status_code, 200)

    def test_admin_reset_disable_and_revoke_take_effect_immediately(self):
        self.assertEqual(self.admin.patch(f"/api/users/{self.admin_user['id']}", json={"password": REPLACEMENT}).status_code, 400)
        user = self.create("alice")
        client = self.client(); self.sign_in(client, "alice", finish=True)
        asset = self.asset(); client.post(f"/api/assets/{asset}/lock")
        reset = self.admin.patch(f"/api/users/{user['id']}", json={"password": REPLACEMENT})
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertTrue(reset.json()["must_change_password"])
        self.assertEqual(client.get("/api/projects").status_code, 401)
        self.assertIsNone(one("SELECT lock_owner FROM assets WHERE id=?", (asset,))["lock_owner"])
        self.sign_in(client, "alice", REPLACEMENT)
        self.assertEqual(client.get("/api/projects").status_code, 403)
        changed = client.post("/api/account/password", json={"current_password": REPLACEMENT, "new_password": PERSONAL})
        self.assertEqual(changed.status_code, 200, changed.text)
        revoked = self.admin.delete(f"/api/users/{user['id']}/sessions")
        self.assertEqual(revoked.json()["revoked_count"], 1)
        self.assertEqual(client.get("/api/session").status_code, 401)
        self.sign_in(client, "alice", PERSONAL)
        self.assertEqual(self.admin.patch(f"/api/users/{user['id']}", json={"enabled": False}).status_code, 200)
        self.assertEqual(client.get("/api/session").status_code, 401)
        self.assertEqual(client.post("/api/login", json={"username": "alice", "password": PERSONAL}).status_code, 401)

    def test_login_racing_admin_password_reset_does_not_issue_stale_session(self):
        user = self.create("alice")
        client = self.client(); self.sign_in(client, "alice", finish=True)
        candidate = self.client("192.0.2.5")
        verified, release = threading.Event(), threading.Event()
        original_check = check_password

        def pause_after_verification(password, encoded):
            result = original_check(password, encoded)
            verified.set()
            if not release.wait(10):
                raise AssertionError("password-reset race test did not release login")
            return result

        with patch("app.accounts.check_password", side_effect=pause_after_verification):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(candidate.post, "/api/login", json={"username": "alice", "password": PERSONAL})
                try:
                    self.assertTrue(verified.wait(10))
                    reset = self.admin.patch(f"/api/users/{user['id']}", json={"password": REPLACEMENT})
                    self.assertEqual(reset.status_code, 200, reset.text)
                finally:
                    release.set()
                self.assertEqual(future.result().status_code, 401)
        self.assertEqual(one("SELECT COUNT(*) AS n FROM user_sessions WHERE user_id=?", (user["id"],))["n"], 0)

    def test_display_name_search_pagination_and_noop_keep_sessions(self):
        alice = self.create("alice"); self.create("bob", "reviewer"); self.create("carol")
        client = self.client(); self.sign_in(client, "alice", finish=True)
        token = client.cookies.get("mdp_session")
        response = client.patch("/api/account", json={"display_name": "  张三  "})
        self.assertEqual(response.json()["display_name"], "张三")
        self.assertEqual(client.get("/api/session").json()["display_name"], "张三")
        self.assertEqual(self.admin.patch(f"/api/users/{alice['id']}", json={"display_name": "小张"}).status_code, 200)
        self.assertEqual(client.get("/api/session").status_code, 200)
        self.assertEqual(client.cookies.get("mdp_session"), token)
        self.assertIsInstance(self.admin.get("/api/users").json(), list)
        page = self.admin.get("/api/users?page=1&page_size=2").json()
        self.assertEqual(page["total"], 4); self.assertEqual(len(page["items"]), 2)
        self.assertEqual(page["enabled_admin_count"], 1)
        found = self.admin.get("/api/users", params={"q": "小张", "role": "annotator"}).json()
        self.assertEqual([item["id"] for item in found["items"]], [alice["id"]])
        self.assertEqual(self.admin.get("/api/users?q=%").json()["total"], 0)
        self.assertEqual(client.patch("/api/account", json={"display_name": " "}).status_code, 422)
        self.assertEqual(client.patch("/api/account", json={"display_name": "ok", "role": "admin"}).status_code, 422)
        self.assertEqual(self.admin.post("/api/users", json={"username": "ALICE", "display_name": "copy", "password": INITIAL}).status_code, 409)

    def test_account_throttle_atomic_and_admin_unlock(self):
        user = self.create("alice")
        clients = [self.client(f"192.0.2.{i+1}") for i in range(12)]
        with ThreadPoolExecutor(max_workers=12) as pool:
            codes = list(pool.map(lambda client: client.post("/api/login", json={"username": "ALICE", "password": "wrong"}).status_code, clients))
        self.assertEqual(codes.count(401), 8, codes)
        self.assertEqual(codes.count(429), 4, codes)
        listed = self.admin.get("/api/users?q=alice").json()["items"][0]
        self.assertEqual(listed["failed_login_count"], 8); self.assertIsNotNone(listed["locked_until"])
        renamed = self.admin.patch(f"/api/users/{user['id']}", json={"display_name": "Alice"})
        self.assertEqual(renamed.json()["failed_login_count"], 8)
        self.assertIsNotNone(renamed.json()["locked_until"])
        retry = clients[0].post("/api/login", json={"username": "alice", "password": INITIAL})
        self.assertEqual(retry.status_code, 429); self.assertIn("retry-after", retry.headers)
        self.assertEqual(self.admin.post(f"/api/users/{user['id']}/unlock").status_code, 200)
        self.sign_in(clients[0], "alice")

    def test_ip_throttle_cannot_be_bypassed_with_usernames_or_forwarded_headers(self):
        client = self.client("192.0.2.8")
        with patch("app.accounts.check_password", return_value=False) as verify:
            for index in range(40):
                response = client.post("/api/login", json={"username": f"missing{index}", "password": "wrong"},
                                       headers={"X-Forwarded-For": f"198.51.100.{index}"})
                self.assertEqual(response.status_code, 401, response.text)
            response = client.post("/api/login", json={"username": "another", "password": "wrong"})
            self.assertEqual(response.status_code, 429, response.text)
            self.assertEqual(verify.call_count, 40)
            fresh = self.client("192.0.2.9")
            self.assertEqual(fresh.post("/api/login", json={"username": "another", "password": "wrong"}).status_code, 401)

    def test_last_admin_protection_under_concurrent_changes(self):
        second_user = self.create("secondadmin", "admin")
        second = self.client("192.0.2.2"); self.sign_in(second, "secondadmin", finish=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(client.patch, f"/api/users/{user_id}", json={"role": "annotator"})
                       for client, user_id in ((self.admin, self.admin_user["id"]), (second, second_user["id"]))]
            codes = sorted(f.result().status_code for f in futures)
        self.assertEqual(codes, [200, 409])
        self.assertEqual(one("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND enabled=1")["n"], 1)

    def test_audits_are_scoped_and_do_not_include_credentials(self):
        alice = self.create("alice"); bob = self.create("bob")
        client = self.client("192.0.2.4")
        client.post("/api/login", json={"username": "alice", "password": "incorrect-secret"})
        self.sign_in(client, "alice", finish=True)
        token = client.cookies.get("mdp_session")
        events = client.get("/api/account/events").json()["events"]
        actions = {event["action"] for event in events}
        self.assertTrue({"account_created", "login_failed", "login_success", "password_changed"} <= actions)
        self.assertTrue(any(event["ip_address"] == "192.0.2.4" for event in events))
        self.assertEqual(client.get(f"/api/users/{bob['id']}/events").status_code, 403)
        self.assertEqual(self.admin.get(f"/api/users/{alice['id']}/events?limit=2").json()["events"], events[:2])
        raw = json.dumps(all_rows("SELECT actor,payload_json FROM audit_logs"))
        for secret in (INITIAL, PERSONAL, "incorrect-secret", token, hashlib.sha256(token.encode()).hexdigest()):
            self.assertNotIn(secret, raw)

    def test_cookie_security_and_expired_session_rejection(self):
        object.__setattr__(settings, "cookie_secure", True)
        client = self.client(secure=True)
        response = self.sign_in(client, "admin", PERSONAL)
        cookie = response.headers["set-cookie"]
        for flag in ("HttpOnly", "Secure", "SameSite=strict", "Max-Age=28800"):
            self.assertIn(flag, cookie)
        session_id = response.json()["session_id"]
        with db() as conn:
            conn.execute("UPDATE user_sessions SET expires_at=? WHERE session_id=?", ((datetime.now(UTC)-timedelta(seconds=1)).isoformat(), session_id))
        self.assertEqual(client.get("/api/session").status_code, 401)

    def test_validation_errors_do_not_echo_password_inputs(self):
        too_long = "secret-validation-marker-" + "x" * 260
        short = "secr3t!"
        cases = (
            (self.admin, "/api/users", {"username": "newuser", "display_name": "New", "password": short}),
            (self.admin, "/api/account/password", {"current_password": PERSONAL, "new_password": too_long}),
            (self.client(), "/api/login", {"username": "unknown", "password": too_long}),
        )
        for client, path, payload in cases:
            response = client.post(path, json=payload)
            self.assertEqual(response.status_code, 422, response.text)
            for secret in (too_long, short, PERSONAL, "secret-validation-marker-"):
                self.assertNotIn(secret, response.text)
            for error in response.json()["detail"]:
                self.assertLessEqual(set(error), {"loc", "msg", "type"})

    def test_legacy_migration_preserves_users_and_upgrades_hash_after_login(self):
        legacy_dir = Path(self.temp.name) / "legacy"
        legacy_dir.mkdir()
        salt = "a" * 32
        digest = hashlib.scrypt(INITIAL.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
        legacy_hash = f"scrypt${salt}${digest}"
        with sqlite3.connect(legacy_dir / "mdp.sqlite3") as conn:
            conn.executescript("""CREATE TABLE users (id INTEGER PRIMARY KEY,username TEXT UNIQUE,display_name TEXT,
                password_hash TEXT,role TEXT,enabled INTEGER DEFAULT 1,created_at TEXT);
                CREATE TABLE user_sessions (token_hash TEXT PRIMARY KEY,user_id INTEGER,expires_at TEXT);""")
            conn.execute("INSERT INTO users VALUES (1,'legacy','Legacy',?,'admin',1,?)", (legacy_hash, now_iso()))
            conn.execute("INSERT INTO user_sessions VALUES (?,1,?)", ("f" * 64, (datetime.now(UTC)+timedelta(hours=1)).isoformat()))
        original_dir = settings.data_dir
        object.__setattr__(settings, "data_dir", legacy_dir)
        try:
            init_db(); init_db()
            user = one("SELECT * FROM users WHERE id=1")
            self.assertFalse(user["must_change_password"])
            self.assertEqual(user["password_hash"], legacy_hash)
            self.assertEqual(len(one("SELECT session_id FROM user_sessions")["session_id"]), 32)
            client = self.client(); result = self.sign_in(client, "legacy")
            self.assertFalse(result.json()["must_change_password"])
            upgraded = one("SELECT password_hash FROM users WHERE id=1")["password_hash"]
            self.assertTrue(upgraded.startswith("scrypt-v2$"))
            self.assertTrue(check_password(INITIAL, upgraded))
            self.assertEqual(client.get("/api/projects").status_code, 200)
        finally:
            object.__setattr__(settings, "data_dir", original_dir)


if __name__ == "__main__":
    unittest.main()
