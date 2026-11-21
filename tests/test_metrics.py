"""指标端点与计数器的测试。"""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.auth import hash_password
from gateway.config import Settings
from gateway.metrics import Metrics
from gateway.storage import Store


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ydd-metrics-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def settings(self, **overrides):
        base = dict(provider="demo", model="demo",
                    db_path=self.root / "g.sqlite3",
                    routes_path=self.root / "no-routes.json")
        base.update(overrides)
        return Settings(**base)

    def test_http_counters_and_auth(self):
        with TestClient(create_app(self.settings(gateway_token="tok"))) as client:
            self.assertEqual(client.get("/agents").status_code, 401)
            response = client.get("/metrics",
                                  headers={"Authorization": "Bearer tok"})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers["content-type"].startswith("text/plain"))
            text = response.text
        self.assertIn('gateway_http_requests_total{method="GET",status="401"} 1', text)
        self.assertIn("gateway_uptime_seconds", text)
        self.assertIn('gateway_build_info{version="0.1.0"} 1', text)

    def test_metrics_requires_admin_or_metrics_scope(self):
        settings = self.settings(auth_mode="local")
        with TestClient(create_app(settings)) as client:
            Store(settings.db_path).create_user("root", hash_password("root-password-1"),
                                                "admin")
            client.post("/login", data={"username": "root",
                                        "password": "root-password-1"},
                        follow_redirects=False)
            self.assertEqual(client.get("/metrics").status_code, 200)
            created = client.post("/api-keys", json={"name": "prom",
                                                     "scopes": ["metrics"]})
            self.assertEqual(created.status_code, 201)
            key = created.json()["key"]
            no_scope = client.post("/api-keys", json={"name": "nope",
                                                      "scopes": ["sessions"]})
            self.assertEqual(no_scope.status_code, 201)
            no_scope_key = no_scope.json()["key"]

        with TestClient(create_app(settings),
                        headers={"Authorization": f"Bearer {key}"}) as reader:
            self.assertEqual(reader.get("/metrics").status_code, 200)

        with TestClient(create_app(settings),
                        headers={"Authorization": f"Bearer {no_scope_key}"}) as other:
            self.assertEqual(other.get("/metrics").status_code, 403)

    def test_ws_gauge(self):
        metrics = Metrics()
        metrics.ws_opened()
        self.assertIn('gateway_ws_connections{kind="chat"} 1', metrics.render())
        metrics.ws_closed()
        self.assertIn('gateway_ws_connections{kind="chat"} 0', metrics.render())


if __name__ == "__main__":
    unittest.main()
