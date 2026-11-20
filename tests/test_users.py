"""用户管理接口、密码策略与登录限速的测试。"""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.auth import LoginLimiter, hash_password, validate_password
from gateway.config import Settings
from gateway.storage import Store


class UserAdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ydd-users-test-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = root / "gateway.sqlite3"
        self.settings = Settings(provider="demo", model="demo", auth_mode="local",
                                 db_path=self.db, routes_path=root / "no-routes.json")
        self.seeded = False

    def client(self, login=None):
        client = TestClient(create_app(self.settings))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        if not self.seeded:
            Store(self.db).create_user("root", hash_password("root-password-1"), "admin")
            self.seeded = True
        if login:
            response = client.post("/login",
                                   data={"username": login,
                                         "password": f"{login}-password-1"},
                                   follow_redirects=False)
            self.assertEqual(response.status_code, 303)
        return client

    def test_create_update_and_reset_password(self):
        admin = self.client("root")
        created = admin.post("/users", json={"username": "alice",
                                             "password": "alice-password-1",
                                             "role": "member"})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertNotIn("password_hash", created.json())

        listed = admin.get("/users").json()
        self.assertEqual([user["username"] for user in listed], ["root", "alice"])

        alice = self.client("alice")
        self.assertEqual(alice.get("/sessions").status_code, 200)

        updated = admin.patch(f"/users/{created.json()['id']}", json={"role": "viewer"})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["role"], "viewer")
        self.assertEqual(alice.post("/sessions", json={}).status_code, 403)

        reset = admin.post(f"/users/{created.json()['id']}/reset-password",
                           json={"password": "alice-password-2"})
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(alice.get("/sessions").status_code, 401)

        actions = [event["action"] for event in admin.get("/audit").json()]
        self.assertIn("user.create", actions)
        self.assertIn("user.update", actions)
        self.assertIn("user.reset_password", actions)

    def test_last_admin_cannot_be_disabled(self):
        admin = self.client("root")
        me = [user for user in admin.get("/users").json() if user["username"] == "root"][0]
        denied = admin.patch(f"/users/{me['id']}", json={"status": "disabled"})
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(denied.json()["error"]["code"], "last_admin")

        admin.post("/users", json={"username": "second",
                                   "password": "second-password-1",
                                   "role": "admin"})
        allowed = admin.patch(f"/users/{me['id']}", json={"status": "disabled"})
        self.assertEqual(allowed.status_code, 200)

    def test_disabled_user_cannot_login_and_sessions_are_revoked(self):
        admin = self.client("root")
        user = admin.post("/users", json={"username": "bob",
                                          "password": "bob-password-1"}).json()
        bob = self.client("bob")
        self.assertEqual(bob.get("/sessions").status_code, 200)

        admin.patch(f"/users/{user['id']}", json={"status": "disabled"})
        self.assertEqual(bob.get("/sessions").status_code, 401)
        denied = self.client().post("/login", data={"username": "bob",
                                                    "password": "bob-password-1"},
                                    follow_redirects=False)
        self.assertEqual(denied.headers["location"], "/login?error=invalid")

    def test_member_cannot_manage_users(self):
        admin = self.client("root")
        admin.post("/users", json={"username": "carol", "password": "carol-password-1"})
        carol = self.client("carol")
        self.assertEqual(carol.get("/users").status_code, 403)
        self.assertEqual(carol.post("/users", json={"username": "x",
                                                    "password": "xxxxxxxxxx1"}).status_code, 403)

    def test_self_password_change_revokes_other_sessions(self):
        admin = self.client("root")
        admin.post("/users", json={"username": "dave", "password": "dave-password-1"})
        first = self.client("dave")
        second = self.client("dave")
        wrong = first.post("/auth/password", json={"current_password": "nope",
                                                   "new_password": "dave-password-2"})
        self.assertEqual(wrong.status_code, 403)

        changed = first.post("/auth/password", json={"current_password": "dave-password-1",
                                                     "new_password": "dave-password-2"})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(second.get("/sessions").status_code, 401)
        self.assertEqual(first.get("/sessions").status_code, 200)


class PasswordAndLimiterTests(unittest.TestCase):
    def test_password_policy_is_shared(self):
        for bad, username in (("short", ""), ("alice-password-1", "alice-password-1"),
                              ("password123", "")):
            with self.assertRaises(ValueError):
                validate_password(bad, username)
        validate_password("alice-password-1", "alice")

    def test_ip_level_limiter_stops_username_spraying(self):
        limiter = LoginLimiter(max_failures=3, window_seconds=300, lock_seconds=900,
                               ip_max_failures=4)
        for index in range(4):
            limiter.record_failure("192.168.1.9", f"user{index}")
        self.assertGreater(limiter.retry_after("192.168.1.9", "another"), 0)
        self.assertEqual(limiter.retry_after("192.168.1.10", "another"), 0)


if __name__ == "__main__":
    unittest.main()
