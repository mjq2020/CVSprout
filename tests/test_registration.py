from __future__ import annotations

import importlib
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

PASSWORD = "registration-password-123"
OTHER_PASSWORD = "other-password-456789"


class RegistrationTest(unittest.TestCase):
    def setUp(self):
        from app import accounts
        from app.auth import Actor
        from app.config import settings
        from app.database import db, one, all_rows, init_db
        self.accounts, self.Actor = accounts, Actor
        self.settings, self.db, self.one, self.all_rows, self.init_db = settings, db, one, all_rows, init_db
        self.main = importlib.import_module("app.main")
        self.temp = tempfile.TemporaryDirectory(prefix="mdp-registration-")
        values = {"data_dir": Path(self.temp.name), "embedded_worker": False,
                  "embedded_pipeline_worker": False, "enable_demo_model": False,
                  "auth_mode": "accounts", "cookie_secure": False, "public_origin": ""}
        self.previous = {key: getattr(settings, key) for key in values}
        for key, value in values.items():
            object.__setattr__(settings, key, value)
        self.clients = []
        self.server = TestClient(self.main.app, headers={"X-MDP-Client": "browser"}).__enter__()
        self.admin_user = self.create("admin", "admin")
        self.admin = self.client("192.0.2.1")
        self.assertEqual(self.login(self.admin, "admin").status_code, 200)
        self.applicant = self.client("192.0.2.2")

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.server.__exit__(None, None, None)
        for key, value in self.previous.items():
            object.__setattr__(self.settings, key, value)
        self.temp.cleanup()

    def client(self, ip="192.0.2.2"):
        client = TestClient(self.main.app, headers={"X-MDP-Client": "browser"}, client=(ip, 50000))
        self.clients.append(client)
        return client

    def create(self, username, role="annotator"):
        row = self.accounts.create_user(self.accounts.UserCreate(username=username, display_name=username,
                                      password=PASSWORD, role=role), self.Actor("fixture", "admin"))
        with self.db() as conn:
            conn.execute("UPDATE users SET must_change_password=0 WHERE id=?", (row["id"],))
        return row

    def login(self, client, username, password=PASSWORD):
        return client.post("/api/login", json={"username": username, "password": password})

    def register(self, username="alice", client=None, **extra):
        return (client or self.applicant).post("/api/register", json={"username": username, "display_name": "Alice",
                        "password": PASSWORD, "note": "加入标注团队", **extra})

    def review(self, row, *, action="approve", role="annotator", reason="", client=None, revision=None):
        body = {"action": action, "expected_revision": row["revision"] if revision is None else revision, "reason": reason}
        if role is not None:
            body["role"] = role
        return (client or self.admin).post(f"/api/registration-requests/{row['id']}/review", json=body)

    def application(self, username="alice"):
        response = self.register(username)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def test_pending_is_not_a_user_and_cannot_login_or_access_business_routes(self):
        before = self.one("SELECT COUNT(*) AS n FROM user_sessions")["n"]
        response = self.register("Alice")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["username"], "alice")
        self.assertEqual(response.json()["status"], "pending")
        self.assertEqual(response.json()["note"], "加入标注团队")
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIsNone(self.one("SELECT * FROM users WHERE username='alice'"))
        pending = self.login(self.applicant, "alice")
        self.assertEqual(pending.status_code, 403, pending.text)
        self.assertEqual(pending.json()["detail"]["code"], "registration_pending")
        self.assertFalse(pending.json()["detail"]["registration"]["can_resubmit"])
        self.assertEqual(pending.json()["detail"]["registration"]["note"], "加入标注团队")
        self.assertNotIn("set-cookie", pending.headers)
        self.assertFalse(self.applicant.cookies.get("mdp_session"))
        self.assertEqual(self.applicant.get("/api/projects").status_code, 401)
        self.assertEqual(self.one("SELECT COUNT(*) AS n FROM user_sessions")["n"], before)
        wrong = self.login(self.applicant, "alice", OTHER_PASSWORD)
        missing = self.login(self.applicant, "missing", OTHER_PASSWORD)
        self.assertEqual((wrong.status_code, wrong.json()), (missing.status_code, missing.json()))
        self.assertNotIn("pending", wrong.text)
        self.assertNotIn("加入标注团队", wrong.text)

    def test_approval_assigns_role_and_allows_chosen_password_without_initial_change(self):
        application = self.application()
        stored_hash = self.one("SELECT password_hash FROM registration_requests")["password_hash"]
        result = self.review(application, role="reviewer")
        self.assertEqual(result.status_code, 200, result.text)
        body = result.json()
        self.assertEqual(body["registration"]["status"], "approved")
        self.assertEqual(body["registration"]["revision"], 2)
        self.assertEqual(body["user"]["role"], "reviewer")
        self.assertFalse(body["user"]["must_change_password"])
        user = self.one("SELECT * FROM users WHERE username='alice'")
        self.assertEqual(user["password_hash"], stored_hash)
        self.assertEqual(self.one("SELECT password_hash FROM registration_requests")["password_hash"], "")
        login = self.login(self.applicant, "alice")
        self.assertEqual(login.status_code, 200, login.text)
        self.assertEqual(login.json()["role"], "reviewer")
        self.assertFalse(login.json()["must_change_password"])
        self.assertIn("HttpOnly", login.headers["set-cookie"])
        self.assertEqual(self.applicant.get("/api/projects").status_code, 200)
        self.assertEqual(self.applicant.get("/api/users").status_code, 403)
        self.assertEqual(self.review(application).status_code, 409)
        self.assertEqual(self.one("SELECT COUNT(*) AS n FROM users WHERE username='alice'")["n"], 1)

    def test_rejection_is_private_and_only_original_password_can_resubmit(self):
        application = self.application()
        rejected = self.review(application, action="reject", role=None, reason="请注明所属团队")
        self.assertEqual(rejected.status_code, 200, rejected.text)
        status = self.login(self.applicant, "alice")
        self.assertEqual(status.status_code, 403, status.text)
        details = status.json()["detail"]
        self.assertEqual(details["code"], "registration_rejected")
        self.assertEqual(details["registration"]["reason"], "请注明所属团队")
        self.assertEqual(details["registration"]["note"], "加入标注团队")
        self.assertTrue(details["registration"]["can_resubmit"])
        self.assertEqual(self.login(self.applicant, "alice", OTHER_PASSWORD).status_code, 401)
        before = self.one("SELECT * FROM registration_requests")
        malicious = self.register(password=OTHER_PASSWORD, display_name="Impostor", note="replace")
        self.assertEqual(malicious.status_code, 409)
        self.assertNotIn("请注明", malicious.text)
        self.assertNotIn("加入标注团队", malicious.text)
        self.assertEqual(self.one("SELECT * FROM registration_requests"), before)
        resubmitted = self.register(display_name="Alice Zhang", note="团队 A")
        self.assertEqual(resubmitted.status_code, 202, resubmitted.text)
        self.assertEqual(resubmitted.json()["id"], application["id"])
        self.assertEqual(resubmitted.json()["revision"], 3)
        self.assertEqual(resubmitted.json()["status"], "pending")
        self.assertEqual(resubmitted.json()["note"], "团队 A")
        row = self.one("SELECT * FROM registration_requests")
        self.assertEqual((row["display_name"], row["note"], row["reason"]), ("Alice Zhang", "团队 A", ""))
        self.assertIsNone(row["reviewed_at"])
        self.assertEqual(self.review(rejected.json()["registration"]).status_code, 409)
        self.assertEqual(self.review(resubmitted.json()).status_code, 200)
        actions = [row["action"] for row in self.all_rows("SELECT * FROM audit_logs WHERE resource_type='registration_request'")]
        self.assertIn("registration_rejected", actions)
        self.assertIn("registration_resubmitted", actions)
        self.assertIn("registration_approved", actions)

    def test_duplicate_pending_is_idempotent_and_existing_accounts_are_preserved(self):
        application = self.application()
        before = self.one("SELECT * FROM registration_requests")
        duplicate = self.register(display_name="Changed", note="Different")
        self.assertEqual(duplicate.status_code, 202, duplicate.text)
        self.assertEqual(duplicate.json()["id"], application["id"])
        self.assertEqual(self.one("SELECT * FROM registration_requests"), before)
        denied = self.register(password=OTHER_PASSWORD)
        user_before = self.one("SELECT * FROM users WHERE username='admin'")
        existing = self.register("admin")
        self.assertEqual((denied.status_code, denied.json()), (existing.status_code, existing.json()))
        self.assertEqual(self.one("SELECT * FROM users WHERE username='admin'"), user_before)
        self.assertIsNone(self.one("SELECT * FROM registration_requests WHERE username='admin'"))

    def test_public_payload_cannot_assign_roles_or_set_status_and_requires_valid_fields(self):
        invalid = [{"role": "admin"}, {"status": "approved"}, {"user_id": 1}, {"enabled": True},
                   {"password": "short"}, {"display_name": "   "}, {"username": "a"}, {"username": "bad space"},
                   {"note": "x" * 1001}]
        for body in invalid:
            result = self.register(**body)
            self.assertEqual(result.status_code, 422, result.text)
            self.assertNotIn(PASSWORD, result.text)
            self.assertNotIn('"input"', result.text)
        self.assertEqual(self.one("SELECT COUNT(*) AS n FROM registration_requests")["n"], 0)
        for mode in ("disabled", "token"):
            with self.subTest(mode=mode):
                object.__setattr__(self.settings, "auth_mode", mode)
                self.assertEqual(self.register().status_code, 409)
        object.__setattr__(self.settings, "auth_mode", "accounts")

    def test_only_admin_can_list_and_review_and_stale_admin_session_is_rechecked(self):
        application = self.application()
        self.assertEqual(self.applicant.get("/api/registration-requests").status_code, 401)
        for role in ("annotator", "reviewer"):
            self.create(role, role)
            client = self.client()
            self.assertEqual(self.login(client, role).status_code, 200)
            self.assertEqual(client.get("/api/registration-requests").status_code, 403)
            self.assertEqual(self.review(application, client=client).status_code, 403)
        session = self.admin.get("/api/session").json()
        actor = self.Actor(session["actor"], "admin", user_id=self.admin_user["id"], username="admin", session_id=self.admin.get("/api/account/sessions").json()["current_session_id"])
        with self.db() as conn:
            conn.execute("UPDATE users SET role='annotator' WHERE id=?", (self.admin_user["id"],))
        self.main.app.dependency_overrides[self.accounts.get_actor] = lambda: actor
        try:
            self.assertEqual(self.admin.get("/api/registration-requests").status_code, 403)
            self.assertEqual(self.review(application).status_code, 403)
        finally:
            self.main.app.dependency_overrides.pop(self.accounts.get_actor, None)
        self.assertEqual(self.one("SELECT status FROM registration_requests")["status"], "pending")

    def test_review_requires_explicit_role_rejection_reason_and_revision(self):
        application = self.application()
        bad = [{"action": "approve", "expected_revision": 1},
               {"action": "approve", "role": "owner", "expected_revision": 1},
               {"action": "reject", "reason": " ", "expected_revision": 1},
               {"action": "reject", "reason": "why", "expected_revision": True},
               {"action": "reject", "reason": "why", "expected_revision": "1"},
               {"action": "approve", "role": "admin", "expected_revision": 1, "password": OTHER_PASSWORD}]
        for payload in bad:
            response = self.admin.post(f"/api/registration-requests/{application['id']}/review", json=payload)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertNotIn(OTHER_PASSWORD, response.text)
        self.assertEqual(self.review(application, revision=2).status_code, 409)
        self.assertEqual(self.review({"id": 999999, "revision": 1}).status_code, 404)
        self.assertEqual(self.one("SELECT status FROM registration_requests")["status"], "pending")

    def test_list_pagination_filters_and_global_counts_never_expose_credentials(self):
        first = self.application("alice")
        second = self.application("bob")
        third = self.application("carol")
        self.assertEqual(self.review(first, role="admin").status_code, 200)
        self.assertEqual(self.review(second, action="reject", role=None, reason="missing team").status_code, 200)
        for status in ("pending", "approved", "rejected", "all"):
            response = self.admin.get(f"/api/registration-requests?status={status}&page_size=1")
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body["counts"], {"pending": 1, "approved": 1, "rejected": 1, "total": 3})
            self.assertEqual(body["total"], 3 if status == "all" else 1)
            self.assertEqual(len(body["items"]), 1)
            self.assertNotIn("password", response.text)
            self.assertNotIn("scrypt", response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.admin.get("/api/registration-requests?status=all&q=carol").json()["items"][0]["id"], third["id"])
        self.assertEqual(self.admin.get("/api/registration-requests?status=all&q=%25").json()["total"], 0)
        self.assertEqual(len(self.admin.get("/api/registration-requests?status=all&page_size=2&page=2").json()["items"]), 1)
        raw = json.dumps(self.all_rows("SELECT * FROM audit_logs"))
        self.assertNotIn(PASSWORD, raw)
        self.assertNotIn("scrypt-v2$", raw)
        self.assertNotIn("account.", raw)

    def test_registration_csrf_ip_and_rate_limits_apply_before_password_work(self):
        denied = self.applicant.post("/api/register", headers={"Origin": "https://evil.example"}, json={
            "username": "alice", "display_name": "Alice", "password": PASSWORD})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.one("SELECT COUNT(*) AS n FROM registration_requests")["n"], 0)
        self.applicant.headers["X-Forwarded-For"] = "203.0.113.99"
        with patch.object(self.accounts, "REGISTRATION_IP_LIMIT", 2):
            self.application("alice")
            self.application("bob")
            with patch.object(self.accounts, "hash_password") as hashed, patch.object(self.accounts, "check_password") as checked:
                limited = self.register("carol")
                self.assertEqual(limited.status_code, 429, limited.text)
                hashed.assert_not_called()
                checked.assert_not_called()
            self.assertGreater(int(limited.headers["retry-after"]), 0)
        self.assertEqual(self.one("SELECT ip_address FROM registration_requests WHERE username='alice'")["ip_address"], "192.0.2.2")
        with self.db() as conn:
            conn.execute("UPDATE registration_ip_attempts SET window_start=0")
        self.assertEqual(self.register("carol").status_code, 202)

    def test_wrong_password_registration_attempts_share_login_account_budget(self):
        self.application()
        for index in range(self.accounts.ACCOUNT_ATTEMPT_LIMIT):
            result = self.register(client=self.client(f"192.0.2.{index + 10}"), password=OTHER_PASSWORD)
            self.assertEqual(result.status_code, 409, result.text)
        result = self.login(self.applicant, "alice")
        self.assertEqual(result.status_code, 429, result.text)
        self.assertGreater(int(result.headers["retry-after"]), 0)
        self.assertIsNone(self.one("SELECT * FROM users WHERE username='alice'"))

    def test_double_review_is_atomic_and_creates_only_one_user(self):
        application = self.application()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.review, application, role=role) for role in ("annotator", "reviewer")]
            results = [future.result() for future in futures]
        self.assertEqual(sorted(result.status_code for result in results), [200, 409])
        self.assertEqual(self.one("SELECT COUNT(*) AS n FROM users WHERE username='alice'")["n"], 1)
        self.assertEqual(self.one("SELECT COUNT(*) AS n FROM audit_logs WHERE action='registration_approved'")["n"], 1)
        self.assertEqual(self.login(self.applicant, "alice").status_code, 200)

    def test_register_create_user_and_approval_cannot_claim_same_name_twice(self):
        payload = {"username": "alice", "display_name": "Manual Alice", "password": OTHER_PASSWORD, "role": "reviewer"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.register)
            second = pool.submit(self.admin.post, "/api/users", json=payload)
            submitted, created = first.result(), second.result()
        self.assertEqual(int(submitted.status_code == 202) + int(created.status_code == 201), 1,
                         (submitted.text, created.text))
        self.assertIn(409, (submitted.status_code, created.status_code))
        if submitted.status_code == 202:
            with ThreadPoolExecutor(max_workers=2) as pool:
                approve = pool.submit(self.review, submitted.json())
                create = pool.submit(self.admin.post, "/api/users", json=payload)
                self.assertEqual(approve.result().status_code, 200)
                self.assertEqual(create.result().status_code, 409)
            self.assertEqual(self.login(self.applicant, "alice").status_code, 200)
        self.assertEqual(self.one("SELECT COUNT(*) AS n FROM users WHERE username='alice'")["n"], 1)

    def test_concurrent_duplicate_registration_cannot_replace_credentials(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.register, password=PASSWORD)
            second = pool.submit(self.register, password=OTHER_PASSWORD)
            results = [first.result(), second.result()]
        self.assertEqual(sorted(result.status_code for result in results), [202, 409])
        self.assertEqual(self.one("SELECT COUNT(*) AS n FROM registration_requests")["n"], 1)
        stored = self.one("SELECT * FROM registration_requests")
        winner_password = PASSWORD if results[0].status_code == 202 else OTHER_PASSWORD
        self.assertTrue(self.accounts.check_password(winner_password, stored["password_hash"]))
        loser_password = OTHER_PASSWORD if results[0].status_code == 202 else PASSWORD
        self.assertFalse(self.accounts.check_password(loser_password, stored["password_hash"]))

    def test_schema_upgrade_is_idempotent_and_preserves_existing_users_sessions(self):
        before_users = self.all_rows("SELECT * FROM users")
        before_sessions = self.all_rows("SELECT * FROM user_sessions")
        with self.db() as conn:
            conn.execute("DROP TABLE registration_requests")
            conn.execute("DROP TABLE registration_ip_attempts")
            conn.execute("DELETE FROM schema_migrations WHERE version=11")
        self.init_db()
        self.init_db()
        self.assertEqual(self.all_rows("SELECT * FROM users"), before_users)
        self.assertEqual(self.all_rows("SELECT * FROM user_sessions"), before_sessions)
        self.assertEqual(self.one("SELECT COUNT(*) AS n FROM schema_migrations WHERE version=11")["n"], 1)
        self.assertEqual(self.one("SELECT COUNT(*) AS n FROM registration_requests")["n"], 0)
        self.assertEqual(self.admin.get("/api/projects").status_code, 200)
        self.assertEqual(self.register().status_code, 202)


if __name__ == "__main__":
    unittest.main()
