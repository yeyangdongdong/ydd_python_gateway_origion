"""HTTP/WebSocket 访问日志：记录路径、JSON 化参数、状态与耗时，并做脱敏。

作为纯 ASGI 中间件实现，不读取下游的 Request 对象，因此不会影响请求体解析。
"""

import json
import logging
import re
import time
from typing import Any
from urllib.parse import parse_qsl
from uuid import uuid4

from starlette.datastructures import MutableHeaders

from .logging_setup import TRACE_ID, LogSettings

SENSITIVE = re.compile(
    r"pass(word|wd)?|token|secret|api[_-]?key|authorization|cookie|ticket|credential",
    re.IGNORECASE,
)

BODY_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
MAX_LIST_ITEMS = 50


def _preview(text: str, preview: int) -> Any:
    """短文本原样返回；长文本只留预览与长度。"""
    if preview <= 0:
        return {"length": len(text)}
    if len(text) <= preview:
        return text
    return {"preview": text[:preview], "length": len(text), "truncated": True}


def redact(value: Any, preview: int) -> Any:
    """递归脱敏：敏感键一律替换，字符串按预览长度截断。"""
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            name = str(key)
            cleaned[name] = "***" if SENSITIVE.search(name) else redact(item, preview)
        return cleaned
    if isinstance(value, list):
        items = [redact(item, preview) for item in value[:MAX_LIST_ITEMS]]
        if len(value) > MAX_LIST_ITEMS:
            items.append({"truncated": True, "length": len(value)})
        return items
    if isinstance(value, str):
        return _preview(value, preview)
    return value


def summarize_query(query_string: str, preview: int) -> dict:
    if not query_string:
        return {}
    grouped: dict[str, Any] = {}
    for key, value in parse_qsl(query_string, keep_blank_values=True):
        if key in grouped:
            existing = grouped[key]
            grouped[key] = existing + [value] if isinstance(existing, list) else [existing, value]
        else:
            grouped[key] = value
    return redact(grouped, preview)


def summarize_body(raw: bytes, content_type: str, settings: LogSettings) -> Any:
    if not raw:
        return None
    if len(raw) > settings.param_max_bytes:
        return {"skipped": True, "length": len(raw)}
    media_type = content_type.split(";")[0].strip().lower()
    if "json" not in media_type:
        return {"length": len(raw), "content_type": media_type or "unknown"}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"length": len(raw), "unparsable": True}
    return redact(parsed, settings.content_preview)


def _header(scope, name: str) -> str:
    target = name.lower().encode()
    for key, value in scope.get("headers") or []:
        if key.lower() == target:
            return value.decode("latin-1")
    return ""


def _route_path(scope) -> str:
    route = scope.get("route")
    return getattr(route, "path", None) or scope.get("path", "")


class AccessLogMiddleware:
    """记录每个 HTTP 请求；在响应头阶段写日志，不缓冲响应体。"""

    def __init__(self, app, settings: LogSettings) -> None:
        self.app = app
        self.settings = settings
        self.logger = logging.getLogger("gateway.access")

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        trace_id = _header(scope, "x-request-id") or uuid4().hex[:16]
        token = TRACE_ID.set(trace_id)
        chunks: list[bytes] = []
        received = 0
        truncated = False
        status = 500
        logged = False

        async def receive_wrapper():
            nonlocal received, truncated
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body") or b""
                room = self.settings.param_max_bytes - received
                if room > 0:
                    chunks.append(chunk[:room])
                if len(chunk) > room:
                    truncated = True
                received += len(chunk)
            return message

        async def send_wrapper(message):
            nonlocal status, logged
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = MutableHeaders(scope=message)
                headers["x-request-id"] = trace_id
                self._emit(scope, status, started, trace_id,
                           b"".join(chunks), truncated, received)
                logged = True
            await send(message)

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except Exception:
            if not logged:
                self._emit(scope, 500, started, trace_id,
                           b"".join(chunks), truncated, received)
            raise
        finally:
            TRACE_ID.reset(token)

    def _emit(self, scope, status: int, started: float, trace_id: str,
              raw_body: bytes, truncated: bool, received: int) -> None:
        method = scope.get("method", "")
        event = "http.request"
        fields = {
            "event": event,
            "service": "gateway",
            "http": {
                "method": method,
                "path": scope.get("path", ""),
                "route": _route_path(scope),
                "status": status,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "client_ip": (scope.get("client") or ("", 0))[0],
                "user_agent": _preview(_header(scope, "user-agent"), 200),
                "bytes_in": received,
            },
            "params": self._params(scope, method, raw_body, truncated),
        }
        level = logging.WARNING if status >= 500 else logging.INFO
        self.logger.log(level, "http request", extra={"fields": fields})

    def _params(self, scope, method: str, raw_body: bytes, truncated: bool) -> Any:
        if not self.settings.params:
            return {"disabled": True}
        query = summarize_query(scope.get("query_string", b"").decode("latin-1"),
                                self.settings.content_preview)
        body = None
        if method in BODY_METHODS:
            body = summarize_body(raw_body, _header(scope, "content-type"), self.settings)
            if truncated:
                body = {"skipped": True, "length": ">%d" % self.settings.param_max_bytes,
                        "partial": body}
        return {"query": query, "body": body}


def log_websocket(event: str, *, session_id: str, trace_id: str, **fields: Any) -> None:
    """WebSocket 生命周期日志（连接/关闭/每轮），参数同样只放摘要。"""
    payload = {
        "event": event,
        "service": "gateway",
        "ws": {"session_id": session_id, **fields},
    }
    token = TRACE_ID.set(trace_id)
    try:
        logging.getLogger("gateway.access").info("websocket", extra={"fields": payload})
    finally:
        TRACE_ID.reset(token)
