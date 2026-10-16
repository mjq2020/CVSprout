import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.security import browser_security


class BrowserSecurityTest(unittest.TestCase):
    def setUp(self):
        self.previous = {key: getattr(settings, key) for key in ("auth_mode", "public_origin")}
        object.__setattr__(settings, "auth_mode", "accounts")
        object.__setattr__(settings, "public_origin", "")
        self.calls = 0
        app = FastAPI()
        app.middleware("http")(browser_security)

        @app.post("/api/login")
        @app.post("/api/projects")
        def write():
            self.calls += 1
            return {"ok": True}

        @app.get("/api/account")
        def read():
            return {"display_name": "Member"}

        self.client = TestClient(app)
        self.client.cookies.set("mdp_session", "account.test")

    def tearDown(self):
        self.client.close()
        for key, value in self.previous.items():
            object.__setattr__(settings, key, value)

    def test_rejects_cross_origin_and_missing_source_before_mutation(self):
        for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"}, {}, {"Referer": "http://testserver.evil/"}):
            self.assertEqual(self.client.post("/api/projects", headers=headers).status_code, 403)
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.client.post("/api/projects", headers={"Origin": "http://testserver"}).status_code, 200)
        self.assertEqual(self.client.post("/api/projects", headers={"Referer": "http://testserver/#/"}).status_code, 200)
        self.assertEqual(self.calls, 2)

    def test_login_is_protected_even_without_session(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.post("/api/login", headers={"Origin": "https://evil.example"}).status_code, 403)
        self.assertEqual(self.client.post("/api/login", headers={"X-MDP-Client": "browser"}).status_code, 200)

    def test_custom_header_cannot_override_cross_origin_browser(self):
        self.assertEqual(self.client.post("/api/projects", headers={"X-MDP-Client": "browser"}).status_code, 200)
        self.assertEqual(self.client.post("/api/projects", headers={"X-MDP-Client": "browser", "Origin": "http://other.testserver"}).status_code, 403)
        self.assertEqual(self.client.post("/api/projects", headers={"X-MDP-Client": "browser", "Sec-Fetch-Site": "same-site"}).status_code, 403)

    def test_explicit_proxy_origin_and_bearer_clients(self):
        object.__setattr__(settings, "public_origin", "https://vision.example")
        self.assertEqual(self.client.post("/api/projects", headers={"Origin": "https://vision.example"}).status_code, 200)
        self.assertEqual(self.client.post("/api/projects", headers={"Origin": "http://testserver"}).status_code, 403)
        self.assertEqual(self.client.post("/api/projects", headers={"Authorization": "Bearer example"}).status_code, 200)

    def test_sensitive_responses_cannot_be_cached_or_framed(self):
        response = self.client.get("/api/account")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_invalid_public_port_fails_closed(self):
        for value in ("https://example.test:bad", "https://example.test:70000"):
            object.__setattr__(settings, "public_origin", value)
            with self.assertRaises(RuntimeError):
                settings.validate()
            self.assertEqual(self.client.post("/api/projects", headers={"Origin": "null"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
