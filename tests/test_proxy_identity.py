"""代理向上游注入 HMAC 身份头，并剥除客户端伪造头。"""

import hashlib
import hmac
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.config import Settings

SECRET = "0123456789abcdef-shared"
CAPTURED = []


class _Capture(BaseHTTPRequestHandler):
    def do_GET(self):
        CAPTURED.append({key.lower(): value for key, value in self.headers.items()})
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ProxyIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ydd-identity-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Capture)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        CAPTURED.clear()
        target = f"http://127.0.0.1:{self.upstream.server_address[1]}"
        routes = self.root / "routes.json"
        routes.write_text(json.dumps({"routes": [
            {"prefix": "/signed/", "target": target, "inject_identity": True,
             "identity_secret": SECRET},
            {"prefix": "/plain/", "target": target},
        ]}), encoding="utf-8")
        self.settings = Settings(provider="demo", model="demo", gateway_token="tok",
                                 db_path=self.root / "g.sqlite3", routes_path=routes)

    def test_signed_identity_and_spoof_stripping(self):
        headers = {"Authorization": "Bearer tok", "X-Auth-User": "evil", "X-Auth-Roles": "admin"}
        with TestClient(create_app(self.settings)) as client:
            signed = client.get("/signed/hello", headers=headers)
            self.assertEqual(signed.status_code, 200, signed.text)
            plain = client.get("/plain/hello", headers=headers)
            self.assertEqual(plain.status_code, 200, plain.text)

        signed_headers, plain_headers = CAPTURED
        self.assertEqual(signed_headers["x-auth-user"], "gateway-token")
        self.assertEqual(signed_headers["x-auth-roles"], "admin")
        timestamp = signed_headers["x-auth-timestamp"]
        expected = hmac.new(SECRET.encode(),
                            f"gateway-token\nadmin\n{timestamp}".encode(),
                            hashlib.sha256).hexdigest()
        self.assertEqual(signed_headers["x-auth-signature"], expected)
        self.assertNotIn("x-auth-user", plain_headers)


if __name__ == "__main__":
    unittest.main()
