"""阶段 5：资源归属、角色权限与代理 scope 的测试。"""

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.auth import hash_password
from gateway.config import Settings
from gateway.storage import Store


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ydd-own-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "gateway.sqlite3"
        self.settings = Settings(provider="demo", model="demo", auth_mode="local",
                                 db_path=self.db, routes_path=self.root / "no-routes.json")
        self.seeded = False

    def client(self, username=None, settings=None):
        client = TestClient(create_app(settings or self.settings))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        if not self.seeded:
            store = Store(self.db)
            for name, role in (("admin", "admin"), ("alice", "member"),
                               ("bob", "member"), ("viewer", "viewer")):
                store.create_user(name, hash_password(f"{name}-pass"), role)
            self.seeded = True
        if username:
            response = client.post("/login",
                                   data={"username": username,
                                         "password": f"{username}-pass"},
                                   follow_redirects=False)
            self.assertEqual(response.status_code, 303)
        return client

    def test_members_only_see_their_own_sessions(self):
        alice = self.client("alice")
        session = alice.post("/sessions", json={}).json()["id"]

        bob = self.client("bob")
        self.assertEqual(bob.get(f"/sessions/{session}").status_code, 404)
        self.assertEqual(bob.get(f"/sessions/{session}/messages").status_code, 404)
        self.assertEqual(bob.post(f"/sessions/{session}/chat",
                                  json={"content": "偷看"}).status_code, 404)
        self.assertEqual(bob.delete(f"/sessions/{session}").status_code, 404)
        self.assertEqual(bob.get("/sessions").json(), [])

        admin = self.client("admin")
        self.assertEqual(admin.get(f"/sessions/{session}").status_code, 200)
        self.assertEqual(len(admin.get("/sessions").json()), 1)

    def test_agent_ownership_and_default_agent(self):
        alice = self.client("alice")
        agent = alice.post("/agents", json={"name": "alice 的助手"}).json()
        bob = self.client("bob")
        self.assertEqual(bob.patch(f"/agents/{agent['id']}",
                                   json={"name": "改掉"}).status_code, 404)
        self.assertEqual(bob.delete(f"/agents/{agent['id']}").status_code, 404)
        bob_names = [item["name"] for item in bob.get("/agents").json()]
        self.assertNotIn("alice 的助手", bob_names)
        self.assertIn("默认助手", bob_names)

        # 默认助手（owner 为空）对成员可见，也能用它建会话。
        self.assertEqual(bob.post("/sessions", json={}).status_code, 201)
        admin_names = [item["name"] for item in self.client("admin").get("/agents").json()]
        self.assertIn("alice 的助手", admin_names)

    def test_viewer_is_read_only(self):
        viewer = self.client("viewer")
        self.assertEqual(viewer.get("/sessions").status_code, 200)
        denied = viewer.post("/sessions", json={})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["error"]["code"], "forbidden")
        self.assertEqual(viewer.post("/agents", json={"name": "x"}).status_code, 403)
        self.assertEqual(viewer.get("/audit").status_code, 403)

    def test_websocket_rejects_other_members_session(self):
        alice = self.client("alice")
        session = alice.post("/sessions", json={}).json()["id"]
        bob = self.client("bob")
        with bob.websocket_connect(f"/sessions/{session}/stream") as websocket:
            event = websocket.receive_json()
        self.assertEqual(event["type"], "error")
        self.assertEqual(event["error"]["code"], "session_not_found")

    def test_proxy_required_scope(self):
        routes = self.root / "routes.json"
        routes.write_text(json.dumps({"routes": [{
            "prefix": "/reports/",
            "target": "http://127.0.0.1:1",
            "required_scope": "reports",
        }]}), encoding="utf-8")
        settings = Settings(provider="demo", model="demo", auth_mode="local",
                            db_path=self.db, routes_path=routes)
        admin = self.client("admin", settings=settings)
        key = admin.post("/api-keys",
                         json={"name": "报表", "scopes": ["sessions"]})
        self.assertEqual(key.status_code, 201, key.text)
        key = key.json()["key"]
        client = TestClient(create_app(settings),
                            headers={"Authorization": f"Bearer {key}"})
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        denied = client.get("/reports/x")
        self.assertEqual(denied.status_code, 403)

        # 补上 scope 后能通过闸门；上游不可用，因此得到 502 而不是 403。
        denied = admin.delete(f"/api-keys/{admin.get('/api-keys').json()[0]['id']}")
        self.assertEqual(denied.status_code, 204)
        key2 = admin.post("/api-keys",
                          json={"name": "报表2", "scopes": ["reports"]})
        self.assertEqual(key2.status_code, 201, key2.text)
        key2 = key2.json()["key"]
        allowed = TestClient(create_app(settings),
                             headers={"Authorization": f"Bearer {key2}"})
        allowed.__enter__()
        self.addCleanup(allowed.__exit__, None, None, None)
        self.assertEqual(allowed.get("/reports/x").status_code, 502)


if __name__ == "__main__":
    unittest.main()
