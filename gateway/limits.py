"""限流、请求体上限与安全响应头。

这些都属于"网络边界"的横切逻辑，集中在一个模块里便于审查。
"""

import time
from collections import deque
from contextlib import asynccontextmanager
from typing import AsyncIterator

from starlette.datastructures import Headers
from starlette.middleware.cors import CORSMiddleware

from .errors import GatewayError

BODY_METHODS = {"POST", "PUT", "PATCH"}


def limiter_key(principal: dict) -> str:
    """同一用户（或同一把 API Key）共享额度；legacy 管理员单独一档。"""
    if principal.get("user_id"):
        return principal["user_id"]
    return f"{principal.get('kind')}:{principal.get('username')}"


class UserLimiter:
    """按用户的并发与每分钟请求数限制；单进程内存即可。"""

    def __init__(self, *, concurrency: int = 3, per_minute: int = 60) -> None:
        self.concurrency = concurrency
        self.per_minute = per_minute
        self.running: dict[str, int] = {}
        self.windows: dict[str, deque] = {}

    def _check(self, key: str) -> None:
        now = time.monotonic()
        window = self.windows.setdefault(key, deque())
        while window and now - window[0] >= 60:
            window.popleft()
        if len(window) >= self.per_minute:
            raise GatewayError(429, "rate_limited", "请求过于频繁，请稍后再试。")
        if self.running.get(key, 0) >= self.concurrency:
            raise GatewayError(429, "too_many_concurrent",
                               "同时进行的对话过多，请等上一轮结束再试。")

    @asynccontextmanager
    async def slot(self, key: str) -> AsyncIterator[None]:
        self._check(key)
        self.windows.setdefault(key, deque()).append(time.monotonic())
        self.running[key] = self.running.get(key, 0) + 1
        try:
            yield
        finally:
            self.running[key] -= 1
            if self.running[key] <= 0:
                self.running.pop(key, None)


class BodyLimitMiddleware:
    """对内置接口限制请求体；代理前缀豁免（文件上传由代理自行控制）。

    先按 Content-Length 快速拒绝；没有长度（chunked）时边读边计数，
    超过上限立即返回 413，不再把请求交给业务。
    """

    BODY_TOO_LARGE = ('{"error":{"code":"body_too_large","message":"请求体超过限制。"}}'
                      ).encode("utf-8")

    def __init__(self, app, max_bytes: int, exempt_prefixes: tuple[str, ...] = ()) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.exempt_prefixes = exempt_prefixes

    def _exempt(self, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in self.exempt_prefixes)

    async def _reject(self, send) -> None:
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                (b"content-length",
                                 str(len(self.BODY_TOO_LARGE)).encode())]})
        await send({"type": "http.response.body", "body": self.BODY_TOO_LARGE})

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope.get("method") not in BODY_METHODS \
                or self._exempt(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        try:
            declared = int(Headers(scope=scope).get("content-length", "0"))
        except ValueError:
            declared = 0
        if declared > self.max_bytes:
            await self._reject(send)
            return

        messages = []
        received = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                messages.append(message)
                break
            chunk = message.get("body") or b""
            received += len(chunk)
            if received > self.max_bytes:
                await self._reject(send)
                return
            messages.append(message)
            if not message.get("more_body", False):
                break

        replay = list(messages)

        async def receive_replay():
            if replay:
                return replay.pop(0)
            return {"type": "http.disconnect"}

        await self.app(scope, receive_replay, send)


CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self' ws: wss:; font-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


class PathScopedCORS:
    """只给网关自己的接口加 CORS；代理前缀交给上游，避免响应头重复。"""

    def __init__(self, app, *, origins: tuple[str, ...],
                 exempt_prefixes: tuple[str, ...] = ()) -> None:
        self.app = app
        self.exempt_prefixes = exempt_prefixes
        self.cors = CORSMiddleware(
            app,
            allow_origins=list(origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID", "X-Proxy-Upstream"],
            max_age=600,
        )

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if scope["type"] == "http" and not any(path.startswith(prefix)
                                               for prefix in self.exempt_prefixes):
            await self.cors(scope, receive, send)
        else:
            await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    """统一加低风险安全头；HTML 页面额外加严格 CSP（脚本/样式都已外置）。"""

    HEADERS = (
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"same-origin"),
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    )

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = Headers(scope=message)
                extra = [(name, value) for name, value in self.HEADERS
                         if name.decode() not in headers]
                if "text/html" in headers.get("content-type", "") \
                        and "content-security-policy" not in headers:
                    extra.append((b"content-security-policy",
                                  CONTENT_SECURITY_POLICY.encode("utf-8")))
                if extra:
                    message["headers"] = [*message["headers"], *extra]
            await send(message)

        await self.app(scope, receive, send_wrapper)
