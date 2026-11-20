"""阶段 2：审计留痕的测试。"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.audit import AuditTrail
from gateway.config import Settings


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ydd-audit-test-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.settings = Settings(
            provider="demo", model="demo",
            gateway_token="synthetic-gateway-key",
            db_path=root / "gateway.sqlite3",
            routes_path=root / "no-routes.json",
        )
        self.auth = {"Authorization": "Bearer synthetic-gateway-key"}

    def client(self, headers=None):
        return TestClient(create_app(self.settings),
                          headers=self.auth if headers is None else headers)

    def actions(self, client):
        return [event["action"] for event in client.get("/audit").json()]

    def test_agent_mutations_are_audited(self):
        with self.client() as client:
            agent = client.post("/agents", json={"name": "审计助手"})
            self.assertEqual(agent.status_code, 201)
            agent_id = agent.json()["id"]
            client.patch(f"/agents/{agent_id}", json={"name": "改名"})
            client.delete(f"/agents/{agent_id}")

            events = client.get("/audit", params={"action": "agent.create"}).json()
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event["target"]["type"], "agent")
            self.assertEqual(event["target"]["id"], agent_id)
            self.assertEqual(event["result"], "ok")
            self.assertEqual(event["actor"]["type"], "legacy")
            self.assertEqual(event["actor"]["name"], "gateway-token")
            self.assertEqual(event["ip"], "testclient")
            self.assertEqual(event["details"], {"name": "审计助手", "model": "demo"})

            update = client.get("/audit", params={"action": "agent.update"}).json()[0]
            self.assertEqual(update["details"], {"fields": ["name"]})
            self.assertIn("agent.delete", self.actions(client))

    def test_session_clear_and_delete_are_audited(self):
        with self.client() as client:
            session = client.post("/sessions", json={}).json()["id"]
            client.post(f"/sessions/{session}/chat", json={"content": "你好"})
            cleared = client.delete(f"/sessions/{session}/messages")
            self.assertEqual(cleared.json()["deleted"], 2)
            client.delete(f"/sessions/{session}")

            events = {event["action"]: event for event in client.get("/audit").json()}
            self.assertEqual(events["session.clear"]["details"], {"deleted": 2})
            self.assertEqual(events["session.clear"]["target"]["id"], session)
            self.assertEqual(events["session.delete"]["target"]["id"], session)

    def test_startup_event_is_recorded(self):
        with self.client() as client:
            events = client.get("/audit", params={"action": "gateway.start"}).json()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor"]["type"], "system")
        self.assertEqual(events[0]["details"]["version"], "0.1.0")

    def test_audit_query_requires_token_and_pages(self):
        with self.client() as client:
            for index in range(3):
                client.post("/agents", json={"name": f"助手{index}"})
            first = client.get("/audit", params={"limit": 2}).json()
            second = client.get("/audit", params={"limit": 2, "offset": 2}).json()
            self.assertEqual(len(first), 2)
            self.assertEqual(len(second), 2)
            self.assertTrue({item["id"] for item in first}.isdisjoint(
                {item["id"] for item in second}))
            denied = client.get("/audit", headers={"Authorization": "wrong"})
            self.assertEqual(denied.status_code, 401)

    def test_audit_failure_does_not_break_business(self):
        with mock.patch.object(AuditTrail, "append", side_effect=RuntimeError("audit down")):
            with self.client() as client:
                response = client.post("/agents", json={"name": "审计挂了也要成功"})
                self.assertEqual(response.status_code, 201, response.text)


if __name__ == "__main__":
    unittest.main()
