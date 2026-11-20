"""可选的反向代理：把其他本地服务挂到网关的同一个入口下。

网关只负责"路由到上游并原样转发"，不解析上游的响应内容。路由表来自
项目根目录的 `routes.json`（可用 GATEWAY_ROUTES_PATH 改位置），形如：

    {
      "routes": [
        {"prefix": "/finance/", "target": "http://127.0.0.1:9001"}
      ]
    }

设计上的几条边界：

- 同时转发 HTTP 与 WebSocket；其余协议（如 gRPC）不处理；
- 目标地址只来自配置，不接受客户端指定，因此天然不会变成 SSRF 跳板；
- 网关自己的 token 不会转发给上游；需要时用路由里的 headers 注入上游凭据；
- 浏览器原生 WebSocket 无法携带请求头，所以代理的 WS 需要 `Authorization` 头，
  不适合直接从浏览器页面连接（内置聊天页走的是会话票据，不经过这里）。
"""

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .errors import GatewayError

PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

# 逐跳头只对单段连接有意义，不能跨代理转发。
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
})

# 网关用来识别"自己的请求又被自己代理回来"的头。
LOOP_HEADER = "x-gateway-proxy"

# 这些请求头不转发给上游：逐跳头、由 httpx 重算的 host/content-length、
# 网关自己的 Bearer 凭据，以及回环标记（由本代理重新设置）。
REQUEST_DROP = HOP_BY_HOP | {"host", "content-length", "authorization", LOOP_HEADER}

# WS 握手时值得转发的头：Cookie 决定登录态，Origin 可能被上游用于来源校验。
WS_FORWARD_HEADERS = {"cookie", "origin", "user-agent"}

# 内置路由占用的第一段路径，配到这里会导致内置接口被遮蔽，直接拒绝。
RESERVED = {"sessions", "agents", "runs", "health", "docs", "redoc", "openapi.json"}

# Starlette 允许应用主动发送的关闭码；上游的其他码（如 1006）必须映射。
ALLOWED_CLOSE_CODES = {1000, 1001, 1002, 1003, *range(1007, 1015)}


@dataclass(frozen=True)
class ProxyRoute:
    prefix: str                      # 必须以 / 开头和结尾，例如 "/finance/"
    target: str                      # 上游前缀，例如 "http://127.0.0.1:9001"
    strip_prefix: bool = True        # 转发时是否去掉 prefix
    headers: dict[str, str] | None = None  # 额外注入给上游的请求头

    def upstream_url(self, remainder: str, query: str) -> str:
        """把"去掉 prefix 之后的部分"拼回上游地址。"""
        path = remainder if self.strip_prefix else self.prefix.lstrip("/") + remainder
        url = f"{self.target}/{path.lstrip('/')}"
        return f"{url}?{query}" if query else url

    def upstream_ws_url(self, remainder: str, query: str) -> str:
        """HTTP 地址换成 WebSocket 地址：http→ws，https→wss。"""
        url = self.upstream_url(remainder, query)
        if url.startswith("https://"):
            return "wss://" + url[len("https://"):]
        return "ws://" + url[len("http://"):]


def load_routes(path: Path) -> list[ProxyRoute]:
    """读取并校验路由表；文件不存在表示不启用反向代理。"""
    if not path.exists():
        return []
    try:
        # 用 utf-8-sig 读：Windows 记事本、PowerShell 的 Set-Content 都会写 BOM，
        # 而 json 模块不接受 BOM，否则路由表会"明明看起来对却读不出来"。
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取路由配置 {path}：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("routes"), list):
        raise ValueError(f"路由配置 {path} 需要形如 {{\"routes\": [ ... ]}} 的结构。")

    routes = [_parse_route(item, path) for item in data["routes"]]
    seen: set[str] = set()
    for route in routes:
        if route.prefix in seen:
            raise ValueError(f"路由配置 {path} 里 prefix 重复：{route.prefix}")
        seen.add(route.prefix)
    return routes


def _parse_route(item: object, path: Path) -> ProxyRoute:
    if not isinstance(item, dict):
        raise ValueError(f"路由配置 {path} 里的每一项都必须是对象。")

    prefix = item.get("prefix")
    if not isinstance(prefix, str) or prefix == "/" or not prefix.startswith("/") \
            or not prefix.endswith("/"):
        raise ValueError(f"路由配置 {path} 里的 prefix 必须形如 \"/name/\"。")
    if prefix.strip("/").split("/")[0] in RESERVED:
        raise ValueError(f"prefix {prefix} 与内置接口冲突，请换一个前缀。")

    target = item.get("target")
    if not isinstance(target, str):
        raise ValueError(f"路由配置 {path} 里的 target 必须是字符串。")
    url = urlsplit(target)
    if url.scheme not in {"http", "https"} or not url.hostname:
        raise ValueError(f"target {target} 必须是完整的 http 或 https 地址。")
    if url.username or url.password or url.query or url.fragment:
        raise ValueError(f"target {target} 不应包含账号、密码、查询参数或片段。")

    strip_prefix = item.get("strip_prefix", True)
    if not isinstance(strip_prefix, bool):
        raise ValueError(f"路由配置 {path} 里的 strip_prefix 只能是 true 或 false。")

    headers = item.get("headers")
    if headers is not None and not (
        isinstance(headers, dict)
        and all(isinstance(key, str) and isinstance(value, str) for key, value in headers.items())
    ):
        raise ValueError(f"路由配置 {path} 里的 headers 必须是字符串键值对。")

    return ProxyRoute(prefix=prefix, target=target.rstrip("/"),
                      strip_prefix=strip_prefix, headers=headers)


def _error_event(status: int, code: str, message: str) -> dict:
    """代理 WS 的错误事件；形状与会话流一致，只是没有 session_id。"""
    return {"type": "error", "status": status, "error": {"code": code, "message": message}}


class Proxy:
    """持有上游客户端，负责单次转发的全部细节（HTTP 与 WebSocket）。"""

    def __init__(self, routes: list[ProxyRoute], timeout: float):
        self.routes = routes
        self.timeout = timeout
        # 与模型调用同理：默认不读取系统代理，否则本机上游会被代理拦截。
        self.client = httpx.AsyncClient(timeout=timeout, trust_env=False)

    async def close(self) -> None:
        await self.client.aclose()

    # ---------------------------------------------------------------- HTTP

    async def forward(self, request: Request, route: ProxyRoute, remainder: str) -> StreamingResponse:
        if request.headers.get(LOOP_HEADER):
            raise GatewayError(508, "proxy_loop", "检测到代理回环：上游又指回了网关自己。")

        url = route.upstream_url(remainder, request.url.query)
        headers = self._request_headers(request.headers, route)
        body = await request.body()
        upstream_request = self.client.build_request(
            request.method, url, headers=headers, content=body if body else None,
        )
        try:
            upstream = await self.client.send(upstream_request, stream=True)
        except httpx.TimeoutException as exc:
            raise GatewayError(504, "upstream_timeout",
                               f"上游 {route.target} 响应超时，请检查该服务。") from exc
        except httpx.RequestError as exc:
            raise GatewayError(502, "upstream_unreachable",
                               f"无法连接上游 {route.target}，请确认服务是否在运行。") from exc

        # 流式转发：不要把上游整个响应读进内存，否则 SSE 和大文件都会出问题。
        return StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=self._response_headers(upstream.headers),
            background=BackgroundTask(upstream.aclose),
        )

    # ----------------------------------------------------------- WebSocket

    async def forward_websocket(self, websocket: WebSocket, route: ProxyRoute,
                                remainder: str) -> None:
        if websocket.headers.get(LOOP_HEADER):
            await websocket.send_json(_error_event(508, "proxy_loop",
                                                   "检测到代理回环：上游又指回了网关自己。"))
            await websocket.close(code=1008)
            return

        url = route.upstream_ws_url(remainder, websocket.url.query)
        headers = {key: value for key, value in websocket.headers.items()
                   if key.lower() in WS_FORWARD_HEADERS}
        if route.headers:
            headers.update(route.headers)
        subprotocols = list(websocket.scope.get("subprotocols") or [])

        try:
            async with connect(url, additional_headers=headers or None,
                               subprotocols=subprotocols or None,
                               open_timeout=self.timeout, max_size=None) as upstream:
                # 先连上游再 accept：这样可以把上游选中的子协议原样回给客户端。
                await websocket.accept(subprotocol=upstream.subprotocol)
                await self._bridge(websocket, upstream)
                close_code = upstream.close_code
        except (TimeoutError, asyncio.TimeoutError):
            await self._close_with_error(websocket, 504, "upstream_timeout",
                                         f"连接上游 {route.target} 的 WebSocket 超时。")
            return
        except (OSError, WebSocketException):
            await self._close_with_error(websocket, 502, "upstream_unreachable",
                                         f"无法连接上游 {route.target} 的 WebSocket。")
            return

        code = close_code if close_code in ALLOWED_CLOSE_CODES else 1011
        with suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close(code=code)

    async def _bridge(self, websocket: WebSocket, upstream) -> None:
        """双向搬运消息；任意一侧结束就返回，由调用方负责收尾。"""

        async def client_to_upstream() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("text") is not None:
                    await upstream.send(message["text"])
                elif message.get("bytes") is not None:
                    await upstream.send(message["bytes"])

        async def upstream_to_client() -> None:
            async for message in upstream:
                if isinstance(message, str):
                    await websocket.send_text(message)
                else:
                    await websocket.send_bytes(message)

        tasks = [asyncio.create_task(client_to_upstream()),
                 asyncio.create_task(upstream_to_client())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError, Exception):
                    await task

    async def _close_with_error(self, websocket: WebSocket, status: int,
                               code: str, message: str) -> None:
        with suppress(RuntimeError):
            await websocket.accept()
        with suppress(RuntimeError, WebSocketDisconnect):
            await websocket.send_json(_error_event(status, code, message))
            await websocket.close(code=1011)

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _request_headers(incoming, route: ProxyRoute) -> dict[str, str]:
        headers = {key: value for key, value in incoming.items()
                   if key.lower() not in REQUEST_DROP}
        if route.headers:
            headers.update(route.headers)
        headers[LOOP_HEADER] = "1"
        return headers

    @staticmethod
    def _response_headers(incoming) -> dict[str, str]:
        return {key: value for key, value in incoming.items()
                if key.lower() not in HOP_BY_HOP}
