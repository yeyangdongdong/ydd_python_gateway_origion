"""CORS：预检、精确 origin、与 CSRF 白名单的联动。"""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.auth import hash_password
from gateway.config import Settings
from gateway.storage import Store

ORIGIN = "http://localhost:5173"


class CorsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ydd-cors-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def settings(self, **overrides):
        base = dict(provider="demo", model="demo", auth_mode="local",
                    db_path=self.root / "g.sqlite3",
                    routes_path=self.root / "no-routes.json",
                    cors_origins=(ORIGIN,))
        base.update(overrides)
        return Settings(**base)

    def test_preflight_and_actual_request(self):
        with TestClient(create_app(self.settings())) as client:
            preflight = client.options(
                "/sessions",
                headers={"Origin": ORIGIN,
                         "Access-Control-Request-Method": "POST",
                         "Access-Control-Request-Headers": "authorization,content-type"})
            self.assertIn(preflight.status_code, (200, 204))
            self.assertEqual(preflight.headers["access-control-allow-origin"], ORIGIN)
            self.assertEqual(preflight.headers["access-control-allow-credentials"], "true")
            self.assertIn("POST", preflight.headers["access-control-allow-methods"])

            actual = client.get("/health", headers={"Origin": ORIGIN})
            self.assertEqual(actual.headers["access-control-allow-origin"], ORIGIN)

            denied = client.get("/health", headers={"Origin": "http://evil.example"})
            self.assertNotIn("access-control-allow-origin", denied.headers)

    def test_csrf_accepts_configured_origin(self):
        settings = self.settings()
        with TestClient(create_app(settings)) as client:
            Store(settings.db_path).create_user("alice", hash_password("alice-password-1"),
                                                "member")
            client.post("/login", data={"username": "alice",
                                        "password": "alice-password-1"},
                        follow_redirects=False)
            allowed = client.post("/sessions", json={}, headers={"Origin": ORIGIN})
            self.assertEqual(allowed.status_code, 201, allowed.text)
            denied = client.post("/sessions", json={},
                                 headers={"Origin": "http://evil.example"})
            self.assertEqual(denied.status_code, 403)

    def test_cors_is_off_by_default(self):
        with TestClient(create_app(self.settings(cors_origins=()))) as client:
            response = client.get("/health", headers={"Origin": ORIGIN})
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_invalid_origins_are_rejected(self):
        for origin in ("*", "http://example.com/path", "ftp://example.com"):
            with self.assertRaises(ValueError):
                Settings(provider="demo", model="demo", cors_origins=(origin,))


if __name__ == "__main__":
    unittest.main()
