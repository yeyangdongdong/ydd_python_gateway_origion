"""限流、请求体上限与安全响应头的测试。"""

import asyncio
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings
from gateway.errors import GatewayError
from gateway.limits import UserLimiter


class LimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ydd-limit-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def settings(self, **overrides):
        base = dict(provider="demo", model="demo", db_path=self.root / "g.sqlite3",
                    routes_path=self.root / "no-routes.json")
        base.update(overrides)
        return Settings(**base)

    def test_security_headers_are_added(self):
        with TestClient(create_app(self.settings())) as client:
            response = client.get("/health")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["referrer-policy"], "same-origin")

    def test_body_limit_returns_413(self):
        with TestClient(create_app(self.settings(max_body_bytes=200))) as client:
            response = client.post("/sessions", json={"title": "x" * 400})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "body_too_large")

    def test_chat_rate_limit_returns_429(self):
        with TestClient(create_app(self.settings(chat_per_minute=1))) as client:
            session = client.post("/sessions", json={}).json()["id"]
            first = client.post(f"/sessions/{session}/chat", json={"content": "第一次"})
            self.assertEqual(first.status_code, 200)
            second = client.post(f"/sessions/{session}/chat", json={"content": "第二次"})
            self.assertEqual(second.status_code, 429)
            self.assertEqual(second.json()["error"]["code"], "rate_limited")


class UserLimiterTests(unittest.TestCase):
    def test_concurrency_limit(self):
        limiter = UserLimiter(concurrency=2, per_minute=100)

        async def scenario():
            async with limiter.slot("alice"):
                async with limiter.slot("alice"):
                    with self.assertRaises(GatewayError) as caught:
                        async with limiter.slot("alice"):
                            pass
                    self.assertEqual(caught.exception.code, "too_many_concurrent")

        asyncio.run(scenario())
        self.assertEqual(limiter.running, {})

    def test_per_minute_limit(self):
        limiter = UserLimiter(concurrency=10, per_minute=2)

        async def scenario():
            for _ in range(2):
                async with limiter.slot("bob"):
                    pass
            with self.assertRaises(GatewayError) as caught:
                async with limiter.slot("bob"):
                    pass
            self.assertEqual(caught.exception.code, "rate_limited")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
