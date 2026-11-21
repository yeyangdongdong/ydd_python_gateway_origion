"""阶段 1：结构化日志与访问日志的测试。"""

import json
import logging
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from gateway.access_log import redact
from gateway.app import create_app
from gateway.config import Settings
from gateway.logging_setup import (
    DailyFileHandler,
    JsonFormatter,
    LogSettings,
    setup_logging,
)


def read_lines(directory: Path):
    entries = []
    for path in sorted(directory.glob("*.log")):
        for line in path.read_text(encoding="utf-8").splitlines():
            entries.append(json.loads(line))
    return entries


class AccessLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ydd-log-test-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.log_dir = root / "logs"
        self.log_settings = LogSettings(
            enabled=True, directory=self.log_dir, level="INFO",
            params=True, content_preview=10,
        )
        self.settings = Settings(
            provider="demo", model="demo",
            db_path=root / "gateway.sqlite3",
            routes_path=root / "no-routes.json",
        )
        self._root_handlers = logging.getLogger().handlers[:]
        self._root_level = logging.getLogger().level
        self.runtime = setup_logging(self.log_settings)

    def tearDown(self):
        self.flush()
        root = logging.getLogger()
        root.handlers[:] = self._root_handlers
        root.setLevel(self._root_level)

    def flush(self):
        if self.runtime is not None:
            self.runtime.stop()
            self.runtime = None

    def client(self):
        return TestClient(create_app(self.settings, self.log_settings))

    def access_lines(self):
        self.flush()
        return read_lines(self.log_dir / "access")

    def test_records_path_status_and_redacts_sensitive_fields(self):
        with self.client() as client:
            response = client.post("/sessions",
                                   json={"title": "测试会话", "password": "hunter2"})
            self.assertEqual(response.status_code, 422)
            self.assertTrue(response.headers.get("x-request-id"))

        entry = next(line for line in self.access_lines()
                     if line["http"]["path"] == "/sessions")
        self.assertEqual(entry["event"], "http.request")
        self.assertEqual(entry["http"]["method"], "POST")
        self.assertEqual(entry["http"]["status"], 422)
        self.assertEqual(entry["http"]["route"], "/sessions")
        self.assertEqual(entry["params"]["body"]["password"], "***")
        self.assertEqual(entry["params"]["body"]["title"], "测试会话")

    def test_long_content_is_previewed(self):
        content = "长" * 50
        with self.client() as client:
            session = client.post("/sessions", json={}).json()["id"]
            response = client.post(f"/sessions/{session}/chat", json={"content": content})
            self.assertEqual(response.status_code, 200, response.text)

        entry = next(line for line in self.access_lines()
                     if line["http"]["path"].endswith("/chat"))
        body = entry["params"]["body"]["content"]
        self.assertEqual(body["length"], 50)
        self.assertTrue(body["truncated"])
        self.assertEqual(body["preview"], "长" * 10)

    def test_query_ticket_is_redacted(self):
        with self.client() as client:
            client.get("/health?ticket=super-secret&page=1")
        entry = next(line for line in self.access_lines()
                     if line["http"]["path"] == "/health")
        self.assertEqual(entry["params"]["query"]["ticket"], "***")
        self.assertEqual(entry["params"]["query"]["page"], "1")

    def test_request_body_still_reaches_endpoint(self):
        with self.client() as client:
            session = client.post("/sessions", json={"title": "日志不吞请求体"}).json()["id"]
            reply = client.post(f"/sessions/{session}/chat", json={"content": "你好"})
            self.assertEqual(reply.json()["reply"], "[演示模式] 收到：你好")

    def test_inbound_request_id_is_preserved(self):
        with self.client() as client:
            response = client.get("/health", headers={"X-Request-ID": "trace-abc"})
        self.assertEqual(response.headers["x-request-id"], "trace-abc")
        entry = next(line for line in self.access_lines()
                     if line["http"]["path"] == "/health")
        self.assertEqual(entry["trace_id"], "trace-abc")

    def test_params_can_be_disabled(self):
        self.flush()
        self.log_settings = replace(self.log_settings, params=False)
        self.runtime = setup_logging(self.log_settings)
        with self.client() as client:
            client.post("/sessions", json={})
        entry = self.access_lines()[0]
        self.assertEqual(entry["params"], {"disabled": True})


class RedactionTests(unittest.TestCase):
    def test_nested_values_and_lists(self):
        cleaned = redact({
            "password": "x",
            "nested": {"api_key": "k"},
            "items": ["a" * 20, {"token": "t"}],
        }, 10)
        self.assertEqual(cleaned["password"], "***")
        self.assertEqual(cleaned["nested"]["api_key"], "***")
        self.assertEqual(cleaned["items"][0],
                         {"preview": "a" * 10, "length": 20, "truncated": True})
        self.assertEqual(cleaned["items"][1]["token"], "***")


class _TinyUpstream(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ServiceLogTests(unittest.TestCase):
    def test_proxied_requests_are_written_to_service_directory(self):
        with tempfile.TemporaryDirectory(prefix="ydd-service-log-") as tmp:
            root = Path(tmp)
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), _TinyUpstream)
            threading.Thread(target=upstream.serve_forever, daemon=True).start()
            try:
                routes = root / "routes.json"
                routes.write_text(json.dumps({"routes": [{
                    "prefix": "/demo/",
                    "target": f"http://127.0.0.1:{upstream.server_address[1]}",
                }]}), encoding="utf-8")
                settings = Settings(provider="demo", model="demo",
                                    db_path=root / "g.sqlite3", routes_path=routes)
                log_settings = LogSettings(enabled=True, directory=root / "logs")
                root_logger = logging.getLogger()
                saved = root_logger.handlers[:]
                saved_level = root_logger.level
                runtime = setup_logging(log_settings)
                try:
                    with TestClient(create_app(settings, log_settings)) as client:
                        response = client.get("/demo/hello")
                        self.assertEqual(response.status_code, 200, response.text)
                finally:
                    runtime.stop()
                    root_logger.handlers[:] = saved
                    root_logger.setLevel(saved_level)
            finally:
                upstream.shutdown()
                upstream.server_close()

            files = list((root / "logs" / "services" / "demo").glob("*.log"))
            self.assertEqual(len(files), 1)
            entry = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(entry["event"], "service.request")
            self.assertEqual(entry["service"], "demo")
            self.assertEqual(entry["http"]["path"], "/demo/hello")
            self.assertIn("upstream", entry)


class FormatterRedactionTests(unittest.TestCase):
    def test_message_query_secrets_are_redacted(self):
        message = ('WebSocket /sessions/x/stream?ticket=secret-ticket'
                   '&token=secret-token&api_key=secret-key&page=1')
        record = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1,
                                   message, None, None)
        payload = json.loads(JsonFormatter().format(record))
        rendered = payload["message"]
        for secret in ("secret-ticket", "secret-token", "secret-key"):
            self.assertNotIn(secret, rendered)
        self.assertIn("ticket=***", rendered)
        self.assertIn("token=***", rendered)
        self.assertIn("api_key=***", rendered)
        self.assertIn("page=1", rendered)


class DailyHandlerTests(unittest.TestCase):
    def test_size_rotation_and_retention_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            handler = DailyFileHandler(directory, max_bytes=200, backup_days=7)
            handler.setFormatter(JsonFormatter())
            logger = logging.getLogger("test.daily")
            logger.propagate = False
            logger.setLevel(logging.INFO)
            logger.addHandler(handler)
            try:
                for i in range(10):
                    logger.info("entry %d", i, extra={"fields": {"i": i}})
            finally:
                logger.removeHandler(handler)
                handler.close()

            files = sorted(path.name for path in directory.glob("*.log"))
            self.assertGreater(len(files), 1, "超过大小上限后应出现序号文件")

            old = directory / f"{(date.today() - timedelta(days=40)).isoformat()}.log"
            old.write_text("{}\n", encoding="utf-8")
            other = DailyFileHandler(directory, max_bytes=200, backup_days=7)
            try:
                other._cleanup(date.today())
            finally:
                other.close()
            self.assertFalse(old.exists(), "超过保留期的文件应被清理")
