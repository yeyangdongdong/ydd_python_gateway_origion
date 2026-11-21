"""阶段 4：API Key、scope 与鉴权的测试。"""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.auth import hash_password
from gateway.config import Settings
from gateway.storage import Store


class ApiKeyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ydd-key-test-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = root / "gateway.sqlite3"
        self.settings = Settings(provider="demo", model="demo", auth_mode="local",
                                 db_path=self.db, routes_path=root / "no-routes.json")
        self._admin_client = None

    def admin_client(self, admin=True):
        client = TestClient(create_app(self.settings))
        client.__enter__()
        store = Store(self.db)
        store.create_user("admin", hash_password("admin-password"), "admin")
        store.create_user("member", hash_password("member-password"), "member")
        response = client.post("/login", data={"username": "admin",
                                               "password": "admin-password"},
                               follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def key_client(self, plaintext):
        client = TestClient(create_app(self.settings),
                            headers={"Authorization": f"Bearer {plaintext}"})
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def execute(self, sql, parameters=()):
        connection = sqlite3.connect(self.db)
        try:
            with connection:
                connection.execute(sql, parameters)
        finally:
            connection.close()

    def create_key(self, client, scopes, name="ci-key", days=None):
        body = {"name": name, "scopes": scopes}
        if days is not None:
            body["expires_in_days"] = days
        response = client.post("/api-keys", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_default_expiry_and_explicit_never(self):
        admin = self.admin_client()
        default = self.create_key(admin, ["sessions"], name="default")
        self.assertIsNotNone(default["expires_at"])
        remaining = (datetime.fromisoformat(default["expires_at"])
                     - datetime.now(timezone.utc)).total_seconds() / 86400
        self.assertTrue(179 < remaining <= 180)

        forever = self.create_key(admin, ["sessions"], name="forever", days=0)
        self.assertIsNone(forever["expires_at"])
        listed = {item["name"]: item for item in admin.get("/api-keys").json()}
        self.assertIsNotNone(listed["default"]["days_left"])
        self.assertIsNone(listed["forever"]["days_left"])

    def test_create_list_and_audit(self):
        client = self.admin_client()
        created = self.create_key(client, ["sessions", "agents"])
        self.assertTrue(created["key"].startswith("ydd_"))
        self.assertEqual(created["prefix"], created["key"].split("_", 2)[2][:8])

        listed = client.get("/api-keys").json()
        self.assertEqual(len(listed), 1)
        self.assertNotIn("key", listed[0])          # 明文只返回一次
        self.assertEqual(listed[0]["scopes"], ["agents", "sessions"])

        actions = [e["action"] for e in client.get("/audit").json()]
        self.assertIn("api_key.create", actions)

    def test_scope_limits_access(self):
        admin = self.admin_client()
        created = self.create_key(admin, ["sessions"])
        key = self.key_client(created["key"])

        session = key.post("/sessions", json={})
        self.assertEqual(session.status_code, 201)
        chat = key.post(f"/sessions/{session.json()['id']}/chat",
                        json={"content": "key 能用"})
        self.assertEqual(chat.status_code, 200, chat.text)

        denied = key.get("/agents")
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["error"]["code"], "forbidden")
        self.assertEqual(key.get("/audit").status_code, 403)
        self.assertEqual(key.post("/api-keys",
                                  json={"name": "x", "scopes": []}).status_code, 403)

    def test_revoked_and_expired_keys_are_rejected(self):
        admin = self.admin_client()
        first = self.create_key(admin, ["sessions"], name="revoked")
        second = self.create_key(admin, ["sessions"], name="expired")

        self.assertEqual(self.key_client(first["key"]).get("/sessions").status_code, 200)
        admin.delete(f"/api-keys/{first['id']}")
        self.assertEqual(self.key_client(first["key"]).get("/sessions").status_code, 401)

        self.execute("UPDATE api_keys SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                     (second["id"],))
        self.assertEqual(self.key_client(second["key"]).get("/sessions").status_code, 401)

    def test_member_cannot_manage_keys(self):
        client = TestClient(create_app(self.settings))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        Store(self.db).create_user("bob", hash_password("bob-password"), "member")
        login = client.post("/login", data={"username": "bob", "password": "bob-password"},
                            follow_redirects=False)
        self.assertEqual(login.status_code, 303)
        denied = client.post("/api-keys", json={"name": "x", "scopes": ["sessions"]})
        self.assertEqual(denied.status_code, 403)

    def test_websocket_accepts_api_key(self):
        admin = self.admin_client()
        created = self.create_key(admin, ["sessions"])
        key = self.key_client(created["key"])
        session = key.post("/sessions", json={}).json()["id"]

        with key.websocket_connect(f"/sessions/{session}/stream") as websocket:
            websocket.send_json({"content": "key ws"})
            kinds = []
            while True:
                event = websocket.receive_json()
                kinds.append(event["type"])
                if event["type"] in {"done", "error"}:
                    break
        self.assertEqual(kinds[-1], "done")


if __name__ == "__main__":
    unittest.main()
