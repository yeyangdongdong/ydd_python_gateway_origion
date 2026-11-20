"""限流、请求体上限与安全响应头。

这些都属于"网络边界"的横切逻辑，集中在一个模块里便于审查。
"""

import time
from collections import deque
from contextlib import asynccontextmanager
from typing import AsyncIterator

from starlette.datastructures import Headers

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
    """对内置接口限制请求体；代理前缀豁免（文件上传由代理自行控制）。"""

    def __init__(self, app, max_bytes: int, exempt_prefixes: tuple[str, ...] = ()) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.exempt_prefixes = exempt_prefixes

    def _exempt(self, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in self.exempt_prefixes)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope.get("method") not in BODY_METHODS \
                or self._exempt(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        try:
            length = int(Headers(scope=scope).get("content-length", "0"))
        except ValueError:
            length = 0
        if length <= self.max_bytes:
            await self.app(scope, receive, send)
            return
        body = ('{"error":{"code":"body_too_large","message":"请求体超过限制。"}}'
                ).encode("utf-8")
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


class SecurityHeadersMiddleware:
    """统一加低风险安全头；页面仍保持可用（暂不加 CSP，避免内联脚本失效）。"""

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
                if extra:
                    message["headers"] = [*message["headers"], *extra]
            await send(message)

        await self.app(scope, receive, send_wrapper)
