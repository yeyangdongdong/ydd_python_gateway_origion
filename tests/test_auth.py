"""阶段 3：本地账号、会话 Cookie、CSRF 与登录限速的测试。"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.auth import hash_password, verify_password
from gateway.config import Settings
from gateway.storage import Store


class PasswordTests(unittest.TestCase):
    def test_hash_and_verify(self):
        stored = hash_password("correct-horse")
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertNotIn("correct-horse", stored)
        self.assertTrue(verify_password("correct-horse", stored))
        self.assertFalse(verify_password("wrong", stored))
        self.assertFalse(verify_password("correct-horse", "not-a-hash"))


class LocalAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ydd-auth-test-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = root / "gateway.sqlite3"
        self.settings = Settings(
            provider="demo", model="demo", auth_mode="local",
            db_path=self.db, routes_path=root / "no-routes.json",
            login_max_failures=3, login_window_seconds=300, login_lock_seconds=900,
        )

    def client(self, settings=None):
        return TestClient(create_app(settings or self.settings))

    def seed(self, username="alice", password="correct-horse", role="admin"):
        return Store(self.db).create_user(username, hash_password(password), role)

    def login(self, client, username="alice", password="correct-horse", next_path="/"):
        return client.post("/login",
                           data={"username": username, "password": password, "next": next_path},
                           follow_redirects=False)

    def test_login_logout_and_cookie_auth(self):
        with self.client() as client:
            self.seed()
            page = client.get("/login")
            self.assertEqual(page.status_code, 200)
            self.assertIn("登录", page.text)

            response = self.login(client)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/")
            self.assertIn("ydd_session", response.cookies)
            self.assertIn("HttpOnly", response.headers["set-cookie"])

            self.assertEqual(client.get("/agents").status_code, 200)
            status = client.get("/auth/status").json()
            self.assertTrue(status["authenticated"])
            self.assertEqual(status["user"]["username"], "alice")

            events = [e["action"] for e in client.get("/audit").json()]
            self.assertIn("auth.login", events)

            logout = client.post("/logout", follow_redirects=False)
            self.assertEqual(logout.status_code, 303)
            self.assertEqual(client.get("/agents").status_code, 401)

    def test_wrong_password_is_audited_and_rate_limited(self):
        with self.client() as client:
            self.seed()
            for _ in range(3):
                response = self.login(client, password="wrong")
                self.assertEqual(response.headers["location"], "/login?error=invalid")
            locked = self.login(client)
            self.assertEqual(locked.headers["location"], "/login?error=locked")

    def test_audit_failure_records_login(self):
        # 用旧 token 查审计，验证登录失败事件确实落库。
        settings = Settings(provider="demo", model="demo", auth_mode="local",
                            gateway_token="audit-token", db_path=self.db,
                            routes_path=Path(self.tmp.name) / "no-routes.json")
        with self.client(settings) as client:
            self.seed()
            self.login(client, password="wrong")
            events = client.get("/audit", headers={"Authorization": "Bearer audit-token"}).json()
        failures = [e for e in events if e["action"] == "auth.login" and e["result"] == "failed"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["details"]["username"], "alice")

    def execute(self, sql, parameters=()):
        """直接改库做边界测试；显式关闭连接，否则 Windows 下临时目录删不掉。"""
        connection = sqlite3.connect(self.db)
        try:
            with connection:
                connection.execute(sql, parameters)
        finally:
            connection.close()

    def test_csrf_rejects_cross_site_mutation(self):
        with self.client() as client:
            self.seed()
            self.login(client)
            denied = client.post("/sessions", json={},
                                 headers={"Origin": "http://evil.example"})
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(denied.json()["error"]["code"], "csrf_rejected")

            allowed = client.post("/sessions", json={},
                                  headers={"Origin": "http://testserver"})
            self.assertEqual(allowed.status_code, 201, allowed.text)

    def test_revoked_and_expired_sessions_are_rejected(self):
        with self.client() as client:
            self.seed()
            self.login(client)
            self.assertEqual(client.get("/agents").status_code, 200)

            self.execute("UPDATE auth_sessions SET revoked_at = '2026-01-01T00:00:00+00:00'")
            self.assertEqual(client.get("/agents").status_code, 401)

            client.cookies.clear()
            self.login(client)
            self.execute("UPDATE auth_sessions SET expires_at = '2000-01-01T00:00:00+00:00'")
            self.assertEqual(client.get("/agents").status_code, 401)

    def test_legacy_token_still_works_in_local_mode(self):
        settings = Settings(provider="demo", model="demo", auth_mode="local",
                            gateway_token="legacy-token", db_path=self.db,
                            routes_path=Path(self.tmp.name) / "no-routes.json")
        with self.client(settings) as client:
            self.assertEqual(client.get("/agents").status_code, 401)
            response = client.get("/agents",
                                  headers={"Authorization": "Bearer legacy-token"})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(client.get("/auth/status").json()["authenticated"])

    def test_websocket_accepts_session_cookie(self):
        with self.client() as client:
            self.seed()
            self.login(client)
            session = client.post("/sessions", json={}).json()["id"]
            with client.websocket_connect(f"/sessions/{session}/stream") as websocket:
                websocket.send_json({"content": "cookie ws"})
                kinds = []
                while True:
                    event = websocket.receive_json()
                    kinds.append(event["type"])
                    if event["type"] in {"done", "error"}:
                        break
            self.assertEqual(kinds[0], "start")
            self.assertEqual(kinds[-1], "done")

    def test_next_redirect_is_restricted(self):
        with self.client() as client:
            self.seed()
            external = self.login(client, next_path="//evil.example/steal")
            self.assertEqual(external.headers["location"], "/")
            client.cookies.clear()
            internal = self.login(client, next_path="/sessions")
            self.assertEqual(internal.headers["location"], "/sessions")

    def test_login_page_is_disabled_in_legacy_mode(self):
        settings = Settings(provider="demo", model="demo", auth_mode="legacy",
                            db_path=self.db, routes_path=Path(self.tmp.name) / "no-routes.json")
        with self.client(settings) as client:
            self.assertEqual(client.get("/login").status_code, 404)


if __name__ == "__main__":
    unittest.main()
