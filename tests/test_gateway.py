"""从真实 HTTP/WS 入口验证行为；模型替身是独立的本地 HTTP SSE 服务。"""

import asyncio
import json
import os
import queue
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.server import serve

from gateway.app import create_app
from gateway.config import Settings
from gateway.context import build_context, estimate_tokens
from gateway.storage import MIGRATIONS, Store
from gateway.tickets import TicketStore


class ModelStub(BaseHTTPRequestHandler):
    requests = queue.Queue()
    slow_started = threading.Event()
    release_slow = threading.Event()

    def log_message(self, format, *args):
        pass  # 测试日志不打印请求正文或凭证。

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.put((self.path, self.headers.get("Authorization"), body))
        text = body["messages"][-1]["content"]
        self.send_response(500 if text == "[http-error]" else 200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        if text == "[http-error]":
            return
        try:
            # 注释与一个多行 data 事件，检验真实 SSE 边界。
            self.wfile.write(b': heartbeat\n\ndata: {"choices":\n')
            self.wfile.write(('data: [{"delta":{"content":"答"}}]}\n\n').encode())
            self.wfile.flush()
            if text == "[slow]":
                self.slow_started.set()
                if not self.release_slow.wait(timeout=5):
                    return
            if text == "[truncated]":
                return
            self.wfile.write(('data: {"choices":[{"delta":{"content":"案"}}]}\n\n').encode())
            self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
            self.wfile.write(b'data: {"choices":[],"usage":{"total_tokens":7}}\n\n')
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # 客户端中断本来就是测试的输入。


class EchoStub(BaseHTTPRequestHandler):
    """反向代理测试用的最小上游：回显路径，并记录收到的请求头。"""

    requests = queue.Queue()

    def log_message(self, format, *args):
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        EchoStub.requests.put({
            "method": self.command,
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "loop": self.headers.get("X-Gateway-Proxy"),
            "body": body.decode("utf-8", "replace"),
        })
        if self.path.startswith("/slow"):
            time.sleep(0.5)          # 用来触发代理超时
        payload = json.dumps({"path": self.path, "method": self.command},
                             ensure_ascii=False).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # 代理超时后会主动断开，这属于测试预期。

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle


class WsEchoUpstream:
    """独立的 WebSocket 回显服务，跑在自己的线程和事件循环里。"""

    def __init__(self):
        self.port = None
        self.ready = threading.Event()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        if not self.ready.wait(timeout=5):
            raise RuntimeError("WS 上游未能启动")

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._start())
        self.loop.run_forever()

    async def _start(self):
        self.server = await serve(self._echo, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.ready.set()

    async def _echo(self, websocket):
        await websocket.send(f"path:{websocket.request.path}")
        async for message in websocket:
            await websocket.send(f"echo:{message}")

    def close(self):
        async def stop():
            self.server.close()
            await self.server.wait_closed()

        asyncio.run_coroutine_threadsafe(stop(), self.loop).result(timeout=5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model_server = ThreadingHTTPServer(("127.0.0.1", 0), ModelStub)
        cls.model_thread = threading.Thread(target=cls.model_server.serve_forever, daemon=True)
        cls.model_thread.start()
        cls.echo_server = ThreadingHTTPServer(("127.0.0.1", 0), EchoStub)
        cls.echo_thread = threading.Thread(target=cls.echo_server.serve_forever, daemon=True)
        cls.echo_thread.start()
        cls.ws_upstream = WsEchoUpstream()

    @classmethod
    def tearDownClass(cls):
        ModelStub.release_slow.set()
        cls.model_server.shutdown()
        cls.model_server.server_close()
        cls.model_thread.join(timeout=5)
        cls.echo_server.shutdown()
        cls.echo_server.server_close()
        cls.echo_thread.join(timeout=5)
        cls.ws_upstream.close()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="ydd-gateway-test-")
        self.addCleanup(self.directory.cleanup)
        ModelStub.requests = queue.Queue()
        ModelStub.slow_started.clear()
        ModelStub.release_slow.clear()
        EchoStub.requests = queue.Queue()
        self.addCleanup(ModelStub.release_slow.set)
        self.settings = Settings(
            provider="openai", model="fixture-model", api_key="synthetic-model-key",
            gateway_token="synthetic-gateway-key",
            base_url=f"http://127.0.0.1:{self.model_server.server_port}/v1",
            db_path=Path(self.directory.name) / "gateway.sqlite3", request_timeout=5,
            # 默认指向不存在的文件，保证每个用例都不受本地 routes.json 影响。
            routes_path=Path(self.directory.name) / "routes.json",
        )
        self.auth = {"Authorization": "Bearer synthetic-gateway-key"}

    @property
    def echo_target(self):
        return f"http://127.0.0.1:{self.echo_server.server_port}"

    def routed_client(self, routes, headers=None, **overrides):
        """写一份临时路由表并返回已启用代理的客户端。"""
        path = Path(self.directory.name) / "routes.json"
        path.write_text(json.dumps({"routes": routes}, ensure_ascii=False), encoding="utf-8")
        settings = replace(self.settings, routes_path=path, **overrides)
        return TestClient(create_app(settings), headers=self.auth if headers is None else headers)

    def client(self):
        return TestClient(create_app(self.settings), headers=self.auth)

    def session(self, client, **data):
        response = client.post("/sessions", json=data)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def events(self, websocket):
        events = []
        while True:
            event = websocket.receive_json()
            events.append(event)
            if event["type"] in {"done", "error"}:
                return events

    def test_http_context_idempotency_and_restart(self):
        with self.client() as client:
            agent = client.post("/agents", json={
                "name": "学习助手", "system_prompt": "只用中文回答。", "model": "custom-model",
            })
            self.assertEqual(agent.status_code, 201)
            session = self.session(client, agent_id=agent.json()["id"], title="测试")
            path = f"/sessions/{session}/chat"
            first = client.post(path, json={"content": "第一问", "request_id": "first"})
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()["reply"], "答案")
            client.post(path, json={"content": "第二问", "request_id": "second"}).raise_for_status()
            endpoint, authorization, first_request = ModelStub.requests.get_nowait()
            second_request = ModelStub.requests.get_nowait()[2]
            self.assertEqual(endpoint, "/v1/chat/completions")
            self.assertEqual(authorization, "Bearer synthetic-model-key")
            self.assertEqual(first_request["model"], "custom-model")
            self.assertEqual(first_request["messages"], [
                {"role": "system", "content": "只用中文回答。"},
                {"role": "user", "content": "第一问"},
            ])
            self.assertEqual(second_request["messages"][1:], [
                {"role": "user", "content": "第一问"}, {"role": "assistant", "content": "答案"},
                {"role": "user", "content": "第二问"},
            ])
            replay = client.post(path, json={"content": "第一问", "request_id": "first"}).json()
            self.assertTrue(replay["replayed"])
            self.assertEqual(replay["run_id"], first.json()["run_id"])
            conflict = client.post(path, json={"content": "其他内容", "request_id": "first"})
            self.assertEqual(conflict.status_code, 409)
        with self.client() as restarted:
            history = restarted.get(f"/sessions/{session}/messages").json()
            self.assertEqual([row["content"] for row in history], ["第一问", "答案", "第二问", "答案"])
            replay = restarted.post(path, json={"content": "第一问", "request_id": "first"}).json()
            self.assertTrue(replay["replayed"])
            self.assertTrue(ModelStub.requests.empty(), "复用已完成请求不应再次调用模型")

    def test_websocket_stream_and_http_replay(self):
        with self.client() as client:
            session = self.session(client)
            with client.websocket_connect(f"/sessions/{session}/stream") as websocket:
                websocket.send_json({"content": "流式问题", "request_id": "stream"})
                events = self.events(websocket)
                self.assertEqual([event["type"] for event in events], ["start", "delta", "delta", "done"])
                self.assertEqual(events[0]["context"]["trimmed_messages"], 0)
                self.assertEqual("".join(event["content"] for event in events if event["type"] == "delta"), "答案")
                self.assertEqual(events[-1]["reply"], "答案")
            rows = client.get(f"/sessions/{session}/messages").json()
            self.assertEqual([(row["role"], row["content"], row["run_status"]) for row in rows], [
                ("user", "流式问题", "completed"), ("assistant", "答案", "completed"),
            ])
            replay = client.post(f"/sessions/{session}/chat", json={"content": "流式问题", "request_id": "stream"}).json()
            self.assertTrue(replay["replayed"])
            with client.websocket_connect(f"/sessions/{session}/stream") as websocket:
                websocket.send_json({"content": "流式问题", "request_id": "stream"})
                self.assertTrue(websocket.receive_json()["replayed"])
            self.assertEqual(ModelStub.requests.qsize(), 1)

    def test_ticket_grants_websocket_access_once(self):
        # 浏览器拿不到自定义请求头，所以走“HTTP 取票 + WS 带 ticket”这条路。
        # 空 Authorization 覆盖客户端默认头，才能真的模拟浏览器。
        browser = {"Authorization": ""}
        with self.client() as client:
            session = self.session(client)
            response = client.post(f"/sessions/{session}/ticket")
            self.assertEqual(response.status_code, 200, response.text)
            ticket = response.json()
            self.assertEqual(ticket["session_id"], session)
            self.assertGreater(ticket["expires_in"], 0)

            with client.websocket_connect(
                f"/sessions/{session}/stream?ticket={ticket['ticket']}", headers=browser
            ) as websocket:
                websocket.send_json({"content": "票据问题", "request_id": "ticket"})
                events = self.events(websocket)
            self.assertEqual([event["type"] for event in events], ["start", "delta", "delta", "done"])
            self.assertEqual(events[-1]["reply"], "答案")

            # 一次性：同一张票据再次连接必须被拒绝。
            with self.assertRaises(WebSocketDisconnect) as denied:
                with client.websocket_connect(
                    f"/sessions/{session}/stream?ticket={ticket['ticket']}", headers=browser
                ):
                    pass
            self.assertEqual(denied.exception.code, 1008)

    def test_ticket_is_bound_to_one_session(self):
        browser = {"Authorization": ""}
        with self.client() as client:
            first = self.session(client)
            second = self.session(client)
            self.assertEqual(client.post("/sessions/missing/ticket").status_code, 404)
            self.assertEqual(
                client.post(f"/sessions/{first}/ticket",
                            headers={"Authorization": "wrong"}).status_code, 401)

            ticket = client.post(f"/sessions/{first}/ticket").json()["ticket"]
            # 拿 A 的票据去连 B 必须失败，而且这张票据同时被作废。
            with self.assertRaises(WebSocketDisconnect) as denied:
                with client.websocket_connect(
                    f"/sessions/{second}/stream?ticket={ticket}", headers=browser
                ):
                    pass
            self.assertEqual(denied.exception.code, 1008)
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect(
                    f"/sessions/{first}/stream?ticket={ticket}", headers=browser
                ):
                    pass
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect(
                    f"/sessions/{first}/stream?ticket=not-a-ticket", headers=browser
                ):
                    pass

    def test_ticket_store_expires_and_is_single_use(self):
        expired = TicketStore(seconds=-1)
        value, seconds = expired.issue("session")
        self.assertEqual(seconds, -1)
        self.assertIsNone(expired.take(value))

        fresh = TicketStore(seconds=30)
        value, seconds = fresh.issue("session")
        self.assertEqual(seconds, 30)
        self.assertEqual(fresh.take(value), "session")
        self.assertIsNone(fresh.take(value))

    def test_chat_page_and_health_need_no_token(self):
        with self.client() as client:
            page = client.get("/", headers={"Authorization": "wrong"})
            self.assertEqual(page.status_code, 200)
            self.assertIn("text/html", page.headers["content-type"])
            # 页面本身不含密钥：拿错 token 也能打开，只是用不了数据接口。
            self.assertIn('id="composer"', page.text)
            self.assertIn("ticket", page.text)
            self.assertTrue(client.get("/health").json()["auth_required"])

    def test_authentication_and_invalid_inputs(self):
        with self.client() as client:
            self.assertEqual(client.get("/health", headers={"Authorization": "wrong"}).status_code, 200)
            self.assertEqual(client.get("/agents", headers={"Authorization": "wrong"}).status_code, 401)
            session = self.session(client)
            with self.assertRaises(WebSocketDisconnect) as denied:
                with client.websocket_connect(f"/sessions/{session}/stream", headers={"Authorization": "wrong"}):
                    pass
            self.assertEqual(denied.exception.code, 1008)
            for body in [{"content": " "}, {"content": 12}, {"content": "hello", "unknown": True}]:
                with self.subTest(body=body):
                    self.assertEqual(client.post(f"/sessions/{session}/chat", json=body).status_code, 422)
            self.assertEqual(client.post("/sessions", json={"agent_id": "missing"}).status_code, 404)
            self.assertEqual(client.post("/sessions/missing/chat", json={"content": "hello"}).status_code, 404)
            with client.websocket_connect(f"/sessions/{session}/stream") as websocket:
                websocket.send_text("not json")
                self.assertEqual(websocket.receive_json()["error"]["code"], "invalid_request")
            self.assertEqual(client.get(f"/sessions/{session}/messages").json(), [])
            self.assertTrue(ModelStub.requests.empty())

    def test_failed_provider_requests_do_not_pollute_next_context(self):
        with self.client() as client:
            for text, code in [("[http-error]", "model_http_error"), ("[truncated]", "model_incomplete")]:
                with self.subTest(failure=text):
                    session = self.session(client)
                    failed = client.post(f"/sessions/{session}/chat", json={"content": text, "request_id": "bad"})
                    self.assertEqual(failed.status_code, 502)
                    self.assertEqual(failed.json()["error"]["code"], code)
                    rows = client.get(f"/sessions/{session}/messages").json()
                    self.assertEqual([(row["role"], row["run_status"]) for row in rows], [("user", "failed")])
                    retry = client.post(f"/sessions/{session}/chat", json={"content": text, "request_id": "bad"})
                    self.assertEqual(retry.status_code, 409)
                    good = client.post(f"/sessions/{session}/chat", json={"content": "新的问题", "request_id": "good"})
                    self.assertEqual(good.status_code, 200, good.text)
                    ModelStub.requests.get_nowait()
                    context = ModelStub.requests.get_nowait()[2]["messages"]
                    self.assertEqual(context[1:], [{"role": "user", "content": "新的问题"}])
                    self.assertTrue(ModelStub.requests.empty())

    def test_websocket_error_events_share_one_shape(self):
        # 所有 WS 错误都用同一个形状：type + status + session_id + error{code,message}。
        with self.client() as client:
            session = self.session(client)
            with client.websocket_connect(f"/sessions/{session}/stream") as websocket:
                websocket.send_text("not json")
                invalid = websocket.receive_json()
                websocket.send_json({"content": "[http-error]", "request_id": "boom"})
                failed = self.events(websocket)[-1]

            for event in (invalid, failed):
                self.assertEqual(event["type"], "error")
                self.assertEqual(event["session_id"], session)
                self.assertIsInstance(event["status"], int)
                self.assertNotIn("status_code", event)
                self.assertTrue(event["error"]["code"])
                self.assertTrue(event["error"]["message"])

            self.assertEqual(invalid["status"], 422)
            self.assertNotIn("request_id", invalid)      # 帧都解析不了，谈不上编号
            self.assertEqual(failed["status"], 502)
            self.assertEqual(failed["request_id"], "boom")
            self.assertTrue(failed["run_id"])

            # 握手阶段发现会话不存在时，也是同一种事件（随后关闭 1008）。
            with client.websocket_connect("/sessions/missing/stream") as websocket:
                missing = websocket.receive_json()
            self.assertEqual(missing["type"], "error")
            self.assertEqual(missing["session_id"], "missing")
            self.assertEqual(missing["status"], 404)
            self.assertEqual(missing["error"]["code"], "session_not_found")

    def test_websocket_gateway_error_has_status_and_ids(self):
        settings = replace(self.settings, context_tokens=400, max_tokens=100)
        with TestClient(create_app(settings), headers=self.auth) as client:
            session = self.session(client)
            with client.websocket_connect(f"/sessions/{session}/stream") as websocket:
                websocket.send_json({"content": "长" * 300, "request_id": "big"})
                event = websocket.receive_json()
            self.assertEqual(event["type"], "error")
            self.assertEqual(event["status"], 413)
            self.assertEqual(event["error"]["code"], "context_too_large")
            self.assertEqual(event["session_id"], session)
            self.assertEqual(event["request_id"], "big")
            # 413 发生在写运行记录之前，所以没有 run_id。
            self.assertNotIn("run_id", event)

    def test_http_stream_error_uses_error_envelope(self):
        # 模型阶段失败在 HTTP 上也返回 {"error": ...}，标识符放外层，不再混用事件形状。
        with self.client() as client:
            session = self.session(client)
            response = client.post(f"/sessions/{session}/chat",
                                   json={"content": "[http-error]", "request_id": "fail"})
            self.assertEqual(response.status_code, 502)
            body = response.json()
            self.assertEqual(set(body), {"error", "session_id", "request_id", "run_id"})
            self.assertEqual(body["error"]["code"], "model_http_error")
            self.assertEqual(body["session_id"], session)
            self.assertEqual(body["request_id"], "fail")

    def test_schema_version_upgrades_legacy_database(self):
        # 造一个“还没有 schema_version 表”的旧库，模拟升级前的部署。
        path = Path(self.directory.name) / "legacy.sqlite3"
        connection = sqlite3.connect(path)
        try:
            for statement in MIGRATIONS[0][1]:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO agents VALUES "
                "('legacy', '旧助手', '提示', 'old-model', '2026-01-01T00:00:00+00:00')"
            )
            connection.commit()
        finally:
            connection.close()

        store = Store(path)
        store.initialize("new-model")
        store.initialize("new-model")       # 重复启动不应重复迁移或报错

        connection = sqlite3.connect(path)
        try:
            version = connection.execute("SELECT version FROM schema_version").fetchone()[0]
            ids = [row[0] for row in connection.execute("SELECT id FROM agents ORDER BY id")]
            model = connection.execute(
                "SELECT model FROM agents WHERE id = 'legacy'").fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(version, MIGRATIONS[-1][0])
        self.assertIn("legacy", ids)          # 旧数据没有被清掉
        self.assertIn("default", ids)         # 默认助手按需补上
        self.assertEqual(model, "old-model")  # 用户自建 Agent 的模型不受影响

    def test_session_serialization_across_transports_and_parallel_sessions(self):
        with self.client() as client, ThreadPoolExecutor(max_workers=1) as pool:
            session = self.session(client)
            other = self.session(client)
            first = pool.submit(client.post, f"/sessions/{session}/chat",
                                json={"content": "[slow]", "request_id": "first"})
            try:
                self.assertTrue(ModelStub.slow_started.wait(timeout=3))
                with client.websocket_connect(f"/sessions/{session}/stream") as websocket:
                    websocket.send_json({"content": "排队问题", "request_id": "second"})
                    # 首个会话仍在等模型时，另一个会话必须能够完成。
                    parallel = client.post(f"/sessions/{other}/chat", json={"content": "独立问题"})
                    self.assertEqual(parallel.status_code, 200, parallel.text)
                    ModelStub.release_slow.set()
                    self.assertEqual(first.result(timeout=3).status_code, 200)
                    self.assertEqual(self.events(websocket)[-1]["reply"], "答案")
                requests = [ModelStub.requests.get_nowait()[2] for _ in range(3)]
                queued = next(item for item in requests if item["messages"][-1]["content"] == "排队问题")
                self.assertEqual(queued["messages"][1:], [
                    {"role": "user", "content": "[slow]"}, {"role": "assistant", "content": "答案"},
                    {"role": "user", "content": "排队问题"},
                ])
            finally:
                ModelStub.release_slow.set()

    def test_websocket_disconnect_records_interrupted_run(self):
        with self.client() as client:
            session = self.session(client)
            try:
                with client.websocket_connect(f"/sessions/{session}/stream") as websocket:
                    websocket.send_json({"content": "[slow]", "request_id": "disconnect"})
                    self.assertEqual(websocket.receive_json()["type"], "start")
                    self.assertEqual(websocket.receive_json()["type"], "delta")
                    self.assertTrue(ModelStub.slow_started.wait(timeout=3))
                deadline = time.monotonic() + 3
                while True:
                    run = client.get(f"/sessions/{session}/requests/disconnect").json()
                    if run["status"] != "running" or time.monotonic() >= deadline:
                        break
                self.assertEqual(run["status"], "interrupted")
                rows = client.get(f"/sessions/{session}/messages").json()
                self.assertEqual([row["role"] for row in rows], ["user"])
            finally:
                ModelStub.release_slow.set()

    def test_restart_marks_unfinished_durable_run_interrupted(self):
        # 用生产存储接口留下相当于进程崩溃时的已提交运行记录。
        store = Store(self.settings.db_path)
        store.initialize(self.settings.model)
        session = store.create_session("default", "崩溃恢复")["id"]
        run_id = store.start_run(session, "unfinished", "未完成问题")
        with self.client() as client:
            run = client.get(f"/runs/{run_id}").json()
            self.assertEqual(run["status"], "interrupted")
            self.assertEqual(run["error_code"], "interrupted")
            response = client.post(f"/sessions/{session}/chat", json={"content": "恢复后问题"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(ModelStub.requests.get_nowait()[2]["messages"][1:], [
                {"role": "user", "content": "恢复后问题"},
            ])

    def test_default_demo_requires_no_model_service(self):
        settings = replace(self.settings, provider="demo", model="demo", api_key="", gateway_token="")
        with TestClient(create_app(settings)) as client:
            self.assertFalse(client.get("/health").json()["auth_required"])
            session = self.session(client)
            response = client.post(f"/sessions/{session}/chat", json={"content": "你好"})
            self.assertEqual(response.json()["reply"], "[演示模式] 收到：你好")
            self.assertTrue(ModelStub.requests.empty())

    def test_agent_management(self):
        with self.client() as client:
            created = client.post("/agents", json={"name": "临时助手", "system_prompt": "简短回答。"})
            self.assertEqual(created.status_code, 201, created.text)
            agent_id = created.json()["id"]

            updated = client.patch(f"/agents/{agent_id}",
                                   json={"name": "改名助手", "model": "another-model"})
            self.assertEqual(updated.status_code, 200, updated.text)
            self.assertEqual(updated.json()["name"], "改名助手")
            self.assertEqual(updated.json()["model"], "another-model")
            # 未提交的字段保持原值，空请求体和未知字段都被拒绝。
            self.assertEqual(updated.json()["system_prompt"], "简短回答。")
            self.assertEqual(client.patch(f"/agents/{agent_id}", json={}).status_code, 422)
            self.assertEqual(client.patch(f"/agents/{agent_id}", json={"unknown": 1}).status_code, 422)

            session = self.session(client, agent_id=agent_id)
            self.assertEqual(client.delete(f"/agents/{agent_id}").status_code, 409)
            self.assertEqual(client.delete(f"/sessions/{session}").status_code, 204)
            self.assertEqual(client.delete(f"/agents/{agent_id}").status_code, 204)
            self.assertEqual(client.patch(f"/agents/{agent_id}", json={"name": "x"}).status_code, 404)
            # 默认助手由服务自动创建，删除会破坏默认会话入口。
            self.assertEqual(client.delete("/agents/default").status_code, 409)

    def test_session_management_and_listing(self):
        with self.client() as client:
            first = self.session(client, title="会话一")
            second = self.session(client, title="会话二")

            renamed = client.patch(f"/sessions/{first}", json={"title": "改名会话"})
            self.assertEqual(renamed.status_code, 200, renamed.text)
            self.assertEqual(client.get(f"/sessions/{first}").json()["title"], "改名会话")
            self.assertEqual(client.patch("/sessions/missing", json={"title": "x"}).status_code, 404)

            page = client.get("/sessions", params={"limit": 1, "offset": 0}).json()
            rest = client.get("/sessions", params={"limit": 1, "offset": 1}).json()
            self.assertEqual([len(page), len(rest)], [1, 1])
            self.assertEqual({first, second}, {row["id"] for row in page + rest})
            self.assertEqual(client.get("/sessions", params={"limit": 0}).status_code, 422)

            self.assertEqual(client.delete(f"/sessions/{second}").status_code, 204)
            self.assertEqual(client.get(f"/sessions/{second}").status_code, 404)
            self.assertEqual(client.delete(f"/sessions/{second}").status_code, 404)

    def test_clear_history_allows_reusing_request_id(self):
        with self.client() as client:
            session = self.session(client)
            path = f"/sessions/{session}/chat"
            self.assertEqual(client.post(path, json={"content": "第一问", "request_id": "reuse"}).status_code, 200)
            self.assertEqual(len(client.get(f"/sessions/{session}/messages").json()), 2)

            cleared = client.delete(f"/sessions/{session}/messages")
            self.assertEqual(cleared.status_code, 200)
            self.assertEqual(cleared.json()["deleted"], 2)
            self.assertEqual(client.get(f"/sessions/{session}/messages").json(), [])

            # 清空会同时删除运行记录，所以旧 request_id 可以重新提问，且不再重放。
            again = client.post(path, json={"content": "第一问", "request_id": "reuse"})
            self.assertEqual(again.status_code, 200, again.text)
            self.assertFalse(again.json()["replayed"])
            requests = [ModelStub.requests.get_nowait()[2] for _ in range(2)]
            self.assertEqual(requests[-1]["messages"][1:], [{"role": "user", "content": "第一问"}])

    def test_message_listing_pagination(self):
        with self.client() as client:
            session = self.session(client)
            client.post(f"/sessions/{session}/chat", json={"content": "一"}).raise_for_status()
            client.post(f"/sessions/{session}/chat", json={"content": "二"}).raise_for_status()
            head = client.get(f"/sessions/{session}/messages", params={"limit": 3}).json()
            self.assertEqual([row["content"] for row in head], ["一", "答案", "二"])
            tail = client.get(f"/sessions/{session}/messages",
                              params={"limit": 3, "offset": 3}).json()
            self.assertEqual([row["content"] for row in tail], ["答案"])

    def test_estimate_tokens_is_conservative(self):
        self.assertEqual(estimate_tokens(""), 0)
        # 中文按 1 字 1 token；其他字符按 3 个 1 token 向上取整。
        self.assertEqual(estimate_tokens("你好世界"), 4)
        self.assertEqual(estimate_tokens("abc"), 1)
        self.assertEqual(estimate_tokens("abcd"), 2)
        self.assertLess(estimate_tokens("短"), estimate_tokens("很长的文本" * 10))

    def test_context_keeps_newest_turn_without_splitting_pairs(self):
        history = [
            {"role": "user", "content": "旧问题" * 40},
            {"role": "assistant", "content": "旧回答" * 40},
            {"role": "user", "content": "新问题"},
            {"role": "assistant", "content": "新回答"},
        ]
        prepared = build_context(history, system_prompt="系统", user_content="当前问题",
                                 context_tokens=600, max_tokens=200)
        # 旧的整轮被丢弃，新的一整轮完整保留，不会只剩半个问答。
        self.assertEqual(prepared.trimmed_messages, 2)
        self.assertEqual([message["role"] for message in prepared.messages],
                         ["system", "system", "user", "assistant", "user"])
        self.assertIn("已省略", prepared.messages[1]["content"])
        self.assertEqual(prepared.messages[2]["content"], "新问题")
        self.assertEqual(prepared.messages[-1]["content"], "当前问题")
        self.assertLessEqual(prepared.estimated_tokens, prepared.budget_tokens)

    def test_long_history_is_trimmed_to_budget(self):
        settings = replace(self.settings, context_tokens=600, max_tokens=200)
        long_question = "问" * 100
        with TestClient(create_app(settings), headers=self.auth) as client:
            session = self.session(client)
            for turn in range(3):
                response = client.post(f"/sessions/{session}/chat",
                                       json={"content": long_question, "request_id": f"turn-{turn}"})
                self.assertEqual(response.status_code, 200, response.text)
            final = client.post(f"/sessions/{session}/chat",
                                json={"content": "小结", "request_id": "final"})
            self.assertEqual(final.status_code, 200, final.text)

            context = final.json()["context"]
            self.assertGreater(context["trimmed_messages"], 0)
            self.assertLessEqual(context["estimated_tokens"], context["budget_tokens"])

            sent = [ModelStub.requests.get_nowait()[2] for _ in range(4)][-1]["messages"]
            self.assertTrue(any("已省略" in message["content"]
                                for message in sent if message["role"] == "system"))
            body = [message for message in sent if message["role"] != "system"]
            self.assertEqual(body[-1]["content"], "小结")
            self.assertEqual(context["trimmed_messages"], 6 - (len(body) - 1))
            # 保留下来的历史仍然严格交替，从用户消息开始。
            for index, message in enumerate(body):
                self.assertEqual(message["role"], "user" if index % 2 == 0 else "assistant")
            # 存储的历史不受影响，消息接口仍然返回全部记录。
            self.assertEqual(len(client.get(f"/sessions/{session}/messages").json()), 8)

    def test_oversized_input_returns_413_and_can_retry(self):
        settings = replace(self.settings, context_tokens=400, max_tokens=100)
        with TestClient(create_app(settings), headers=self.auth) as client:
            session = self.session(client)
            response = client.post(f"/sessions/{session}/chat",
                                   json={"content": "长" * 300, "request_id": "big"})
            self.assertEqual(response.status_code, 413, response.text)
            self.assertEqual(response.json()["error"]["code"], "context_too_large")
            # 超限时不调用模型，也不留下半条记录。
            self.assertTrue(ModelStub.requests.empty())
            self.assertEqual(client.get(f"/sessions/{session}/messages").json(), [])
            # 没有运行记录，所以改短输入后可以用同一个 request_id 重试。
            retry = client.post(f"/sessions/{session}/chat",
                                json={"content": "短问题", "request_id": "big"})
            self.assertEqual(retry.status_code, 200, retry.text)

    def test_short_history_reports_no_trimming(self):
        with self.client() as client:
            session = self.session(client)
            response = client.post(f"/sessions/{session}/chat", json={"content": "你好"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["context"]["trimmed_messages"], 0)
            sent = ModelStub.requests.get_nowait()[2]["messages"]
            self.assertEqual([message["role"] for message in sent], ["system", "user"])

    def test_context_window_must_leave_room(self):
        with self.assertRaises(ValueError):
            replace(self.settings, context_tokens=1024, max_tokens=1024)
        valid = replace(self.settings, context_tokens=1024, max_tokens=768)
        self.assertEqual(valid.context_tokens, 1024)

    def test_management_refuses_while_generating(self):
        with self.client() as client, ThreadPoolExecutor(max_workers=1) as pool:
            session = self.session(client)
            chat = pool.submit(client.post, f"/sessions/{session}/chat", json={"content": "[slow]"})
            try:
                self.assertTrue(ModelStub.slow_started.wait(timeout=3))
                self.assertEqual(client.delete(f"/sessions/{session}/messages").status_code, 409)
                self.assertEqual(client.delete(f"/sessions/{session}").status_code, 409)
            finally:
                ModelStub.release_slow.set()
                self.assertEqual(chat.result(timeout=3).status_code, 200)
            self.assertEqual(client.delete(f"/sessions/{session}").status_code, 204)

    def test_model_client_ignores_system_proxy_by_default(self):
        # Windows 上开着代理软件时，httpx 默认会把访问 127.0.0.1 的请求也交给代理，
        # 表现是模型调用莫名返回 502。这里把代理指向不可用端口验证默认直连。
        poison = {"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
                  "ALL_PROXY": "http://127.0.0.1:9", "NO_PROXY": "", "no_proxy": ""}
        with mock.patch.dict(os.environ, poison):
            with self.client() as client:
                session = self.session(client)
                response = client.post(f"/sessions/{session}/chat", json={"content": "代理问题"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["reply"], "答案")

    def test_system_proxy_can_be_enabled_explicitly(self):
        settings = replace(self.settings, use_system_proxy=True)
        poison = {"HTTP_PROXY": "http://127.0.0.1:9", "NO_PROXY": "", "no_proxy": ""}
        with mock.patch.dict(os.environ, poison):
            with TestClient(create_app(settings), headers=self.auth) as client:
                session = self.session(client)
                response = client.post(f"/sessions/{session}/chat", json={"content": "代理问题"})
                # 显式开启后请求会经过系统代理；代理不可用，因此本轮失败。
                self.assertIn(response.status_code, {502, 504}, response.text)

    def test_system_proxy_setting_is_validated(self):
        with mock.patch.dict(os.environ, {"MODEL_USE_SYSTEM_PROXY": "true"}):
            self.assertTrue(Settings.from_env().use_system_proxy)
        with mock.patch.dict(os.environ, {"MODEL_USE_SYSTEM_PROXY": ""}):
            self.assertFalse(Settings.from_env().use_system_proxy)
        with mock.patch.dict(os.environ, {"MODEL_USE_SYSTEM_PROXY": "maybe"}):
            with self.assertRaises(ValueError):
                Settings.from_env()

    def test_proxy_forwards_http_and_strips_prefix(self):
        with self.routed_client([{"prefix": "/echo/", "target": self.echo_target}]) as client:
            response = client.post("/echo/items?x=1&y=2", json={"hello": "world"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["path"], "/items?x=1&y=2")

        seen = EchoStub.requests.get_nowait()
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(json.loads(seen["body"]), {"hello": "world"})
        self.assertIsNone(seen["authorization"])   # 网关自己的 token 不能泄露给上游
        self.assertEqual(seen["loop"], "1")

    def test_proxy_can_keep_prefix(self):
        with self.routed_client([{"prefix": "/echo/", "target": self.echo_target,
                                  "strip_prefix": False}]) as client:
            self.assertEqual(client.get("/echo/a/b").status_code, 200)
        self.assertEqual(EchoStub.requests.get_nowait()["path"], "/echo/a/b")

    def test_proxy_requires_gateway_token(self):
        with self.routed_client([{"prefix": "/echo/", "target": self.echo_target}],
                                headers={}) as client:
            self.assertEqual(client.get("/echo/x").status_code, 401)
            with self.assertRaises(WebSocketDisconnect) as denied:
                with client.websocket_connect("/echo/ws"):
                    pass
            self.assertEqual(denied.exception.code, 1008)

    def test_proxy_stays_disabled_without_routes_file(self):
        with self.client() as client:
            self.assertEqual(client.get("/finance/anything").status_code, 404)

    def test_proxy_websocket_passthrough(self):
        target = f"http://127.0.0.1:{self.ws_upstream.port}"
        with self.routed_client([{"prefix": "/echo/", "target": target}]) as client:
            with client.websocket_connect("/echo/ws") as websocket:
                self.assertEqual(websocket.receive_text(), "path:/ws")
                websocket.send_text("hi")
                self.assertEqual(websocket.receive_text(), "echo:hi")

    def test_proxy_upstream_failures(self):
        dead = "http://127.0.0.1:1"
        with self.routed_client([{"prefix": "/dead/", "target": dead}]) as client:
            response = client.get("/dead/x")
            self.assertEqual(response.status_code, 502)
            self.assertEqual(response.json()["error"]["code"], "upstream_unreachable")

            with client.websocket_connect("/dead/ws") as websocket:
                event = websocket.receive_json()
                self.assertEqual(event["type"], "error")
                self.assertEqual(event["status"], 502)
                self.assertEqual(event["error"]["code"], "upstream_unreachable")
                self.assertNotIn("session_id", event)   # 代理 WS 不是会话级连接

    def test_proxy_timeout_maps_to_504(self):
        with self.routed_client([{"prefix": "/echo/", "target": self.echo_target}],
                                proxy_timeout=0.1) as client:
            response = client.get("/echo/slow")
            self.assertEqual(response.status_code, 504, response.text)
            self.assertEqual(response.json()["error"]["code"], "upstream_timeout")

    def test_proxy_loop_guard(self):
        with self.routed_client([{"prefix": "/echo/", "target": self.echo_target}]) as client:
            response = client.get("/echo/x", headers={**self.auth, "X-Gateway-Proxy": "1"})
            self.assertEqual(response.status_code, 508)
            self.assertEqual(response.json()["error"]["code"], "proxy_loop")

    def test_proxy_route_file_tolerates_bom(self):
        # Windows 记事本 / PowerShell 的 Set-Content 会写 UTF-8 BOM。
        path = Path(self.directory.name) / "bom-routes.json"
        payload = json.dumps({"routes": [{"prefix": "/echo/", "target": self.echo_target}]})
        path.write_text(payload, encoding="utf-8-sig")
        with TestClient(create_app(replace(self.settings, routes_path=path)),
                        headers=self.auth) as client:
            self.assertEqual(client.get("/echo/x").status_code, 200)

    def test_proxy_route_config_is_validated(self):
        cases = {
            "非法 prefix": [{"prefix": "finance/", "target": "http://127.0.0.1:1"}],
            "与内置接口冲突": [{"prefix": "/sessions/", "target": "http://127.0.0.1:1"}],
            "非法 target": [{"prefix": "/a/", "target": "ftp://127.0.0.1:1"}],
            "prefix 重复": [{"prefix": "/a/", "target": "http://127.0.0.1:1"},
                            {"prefix": "/a/", "target": "http://127.0.0.1:2"}],
        }
        for label, routes in cases.items():
            with self.subTest(case=label):
                path = Path(self.directory.name) / "bad-routes.json"
                path.write_text(json.dumps({"routes": routes}, ensure_ascii=False), encoding="utf-8")
                with self.assertRaises(ValueError):
                    create_app(replace(self.settings, routes_path=path))


if __name__ == "__main__":
    unittest.main()
