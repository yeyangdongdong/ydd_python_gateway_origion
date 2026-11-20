"""可选的反向代理：把其他本地服务挂到网关的同一个入口下。

网关只负责"路由到上游并原样转发"，不解析上游的响应内容。路由表来自
项目根目录的 `routes.json`（可用 GATEWAY_ROUTES_PATH 改位置），形如：

    {
      "routes": [
        {"prefix": "/finance/", "target": "http://127.0.0.1:9001"},
        {"prefix": "/report/",
         "targets": [{"url": "http://127.0.0.1:9002", "weight": 2},
                     {"url": "http://127.0.0.1:9003", "weight": 1}]}
      ]
    }

设计上的几条边界：

- 同时转发 HTTP 与 WebSocket；其余协议（如 gRPC）不处理；
- 目标地址只来自配置，不接受客户端指定，因此天然不会变成 SSRF 跳板；
- 网关自己的 token 不会转发给上游；需要时用路由里的 headers 注入上游凭据；
- 多上游用平滑加权轮询；连接失败的上游会短暂冷却，HTTP 只在幂等方法上切换；
- 浏览器原生 WebSocket 设不了请求头，所以代理 WS 用 `?ticket=`（由 /proxy-ticket 签发）。
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .errors import GatewayError
from .proxy_health import HealthMonitor, HealthSettings

logger = logging.getLogger(__name__)

DEFAULT_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")

# 只有这些方法的连接失败可以安全地换一个上游：请求还没被处理，重放没有副作用。
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

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
IDENTITY_HEADERS = frozenset({"x-auth-user", "x-auth-roles",
                              "x-auth-timestamp", "x-auth-signature"})

# WS 握手时值得转发的头：Cookie 决定登录态，Origin 可能被上游用于来源校验。
WS_FORWARD_HEADERS = {"cookie", "origin", "user-agent"}

# 内置路由占用的第一段路径，配到这里会导致内置接口被遮蔽，直接拒绝。
RESERVED = {"sessions", "agents", "runs", "health", "docs", "redoc", "openapi.json",
            "proxy-status", "audit", "login", "logout", "auth", "api-keys", "users"}

# Starlette 允许应用主动发送的关闭码；上游的其他码（如 1006）必须映射。
ALLOWED_CLOSE_CODES = {1000, 1001, 1002, 1003, *range(1007, 1015)}


@dataclass
class Upstream:
    """一个上游地址及其选路状态（单进程内使用，不需要加锁）。"""

    url: str
    weight: int = 1
    current_weight: int = 0
    cooldown_until: float = 0.0
    # 主动探活的结果；healthy=False 的上游只在没有别的选择时才会被使用。
    healthy: bool = True
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_error: str = ""


@dataclass(frozen=True)
class ProxyRoute:
    prefix: str                      # 必须以 / 开头和结尾，例如 "/finance/"
    targets: tuple[Upstream, ...]    # 至少一个；多于一个时按权重轮询
    strip_prefix: bool = True        # 转发时是否去掉 prefix
    add_prefix: str = ""             # 转发前额外加的前缀，例如 "/api"
    methods: tuple[str, ...] = DEFAULT_METHODS
    websocket: bool = True           # 该前缀是否接受 WebSocket
    cooldown_seconds: float = 5.0    # 连接失败后的冷却时间
    headers: dict[str, str] | None = None  # 额外注入给上游的请求头
    health: HealthSettings = HealthSettings()  # 主动探活参数（已合并全局默认）
    required_scope: str | None = None    # 配置后，API Key 必须带这个 scope
    inject_identity: bool = False        # 是否向上游注入带签名的用户身份头
    identity_secret: str | None = None   # 与上游共享的 HMAC 密钥

    def rewritten_path(self, remainder: str) -> str:
        """算出发给上游的路径：prefix 的取舍 + add_prefix。"""
        path = remainder if self.strip_prefix else self.prefix.lstrip("/") + remainder
        path = path.lstrip("/")
        if self.add_prefix:
            return f"{self.add_prefix}/{path}" if path else self.add_prefix
        return f"/{path}" if path else "/"

    def upstream_url(self, upstream: Upstream, remainder: str, query: str) -> str:
        url = upstream.url + self.rewritten_path(remainder)
        return f"{url}?{query}" if query else url

    def upstream_ws_url(self, upstream: Upstream, remainder: str, query: str) -> str:
        """HTTP 地址换成 WebSocket 地址：http→ws，https→wss。"""
        url = self.upstream_url(upstream, remainder, query)
        scheme = "wss://" if url.startswith("https://") else "ws://"
        return scheme + url.split("://", 1)[1]


def load_routes(path: Path) -> list[ProxyRoute]:
    """读取并校验路由表；文件不存在表示不启用反向代理。

    返回的列表按前缀长度从长到短排序，注册路由时"最长前缀优先"，
    这样 `/a/b/` 不会被先声明的 `/a/` 遮蔽。
    """
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

    # 顶层的 health 是所有路由的默认值；每条路由可以再覆盖自己需要的字段。
    default_health = HealthSettings.from_dict(data.get("health"), f"{path} 的 health")
    routes = [_parse_route(item, path, default_health) for item in data["routes"]]
    seen: set[str] = set()
    for route in routes:
        if route.prefix in seen:
            raise ValueError(f"路由配置 {path} 里 prefix 重复：{route.prefix}")
        seen.add(route.prefix)
    routes.sort(key=lambda route: len(route.prefix), reverse=True)
    return routes


def match_route(routes: list[ProxyRoute], path: str) -> ProxyRoute | None:
    """按最长前缀匹配；列表已排序，所以第一个命中的就是最长的。"""
    for route in routes:
        if path.startswith(route.prefix):
            return route
    return None


def _parse_route(item: object, path: Path, default_health: HealthSettings) -> ProxyRoute:
    if not isinstance(item, dict):
        raise ValueError(f"路由配置 {path} 里的每一项都必须是对象。")

    prefix = item.get("prefix")
    if not isinstance(prefix, str) or prefix == "/" or not prefix.startswith("/") \
            or not prefix.endswith("/"):
        raise ValueError(f"路由配置 {path} 里的 prefix 必须形如 \"/name/\"。")
    if prefix.strip("/").split("/")[0] in RESERVED:
        raise ValueError(f"prefix {prefix} 与内置接口冲突，请换一个前缀。")

    targets = _parse_targets(item, path)
    strip_prefix = item.get("strip_prefix", True)
    if not isinstance(strip_prefix, bool):
        raise ValueError(f"路由配置 {path} 里的 strip_prefix 只能是 true 或 false。")

    add_prefix = item.get("add_prefix", "")
    if not isinstance(add_prefix, str) or (add_prefix and not add_prefix.startswith("/")):
        raise ValueError(f"路由配置 {path} 里的 add_prefix 必须是空串或以 / 开头的路径。")
    add_prefix = add_prefix.rstrip("/")

    methods = item.get("methods", list(DEFAULT_METHODS))
    if not isinstance(methods, list) or not methods:
        raise ValueError(f"路由配置 {path} 里的 methods 必须是非空数组。")
    normalized = []
    for method in methods:
        if not isinstance(method, str) or method.upper() not in DEFAULT_METHODS:
            raise ValueError(f"路由配置 {path} 里的 method {method!r} 不是支持的 HTTP 方法。")
        if method.upper() not in normalized:
            normalized.append(method.upper())

    websocket = item.get("websocket", True)
    if not isinstance(websocket, bool):
        raise ValueError(f"路由配置 {path} 里的 websocket 只能是 true 或 false。")

    cooldown = item.get("cooldown_seconds", 5.0)
    if not isinstance(cooldown, (int, float)) or isinstance(cooldown, bool) or cooldown <= 0:
        raise ValueError(f"路由配置 {path} 里的 cooldown_seconds 必须是大于 0 的数字。")

    headers = item.get("headers")
    if headers is not None and not (
        isinstance(headers, dict)
        and all(isinstance(key, str) and isinstance(value, str) for key, value in headers.items())
    ):
        raise ValueError(f"路由配置 {path} 里的 headers 必须是字符串键值对。")

    required_scope = item.get("required_scope")
    if required_scope is not None:
        if not isinstance(required_scope, str) or not required_scope.strip() \
                or len(required_scope) > 50:
            raise ValueError(f"路由配置 {path} 里的 required_scope 必须是 1~50 字符的字符串。")
        required_scope = required_scope.strip()

    inject_identity = item.get("inject_identity", False)
    if not isinstance(inject_identity, bool):
        raise ValueError(f"路由配置 {path} 里的 inject_identity 只能是 true 或 false。")
    identity_secret = item.get("identity_secret")
    if inject_identity and (not isinstance(identity_secret, str) or len(identity_secret) < 16):
        raise ValueError(f"路由配置 {path} 启用 inject_identity 时，"
                         "identity_secret 必须是至少 16 字符的共享密钥。")
    if not inject_identity:
        identity_secret = None

    # 路由级 health 覆盖全局：未写的字段沿用全局值，然后整体重新校验。
    raw_health = item.get("health")
    if raw_health is None:
        health = default_health
    elif isinstance(raw_health, dict):
        health = default_health.override(raw_health, f"{path} 里 {prefix} 的 health")
    else:
        raise ValueError(f"路由配置 {path} 里的 health 必须是对象。")

    return ProxyRoute(prefix=prefix, targets=tuple(targets), strip_prefix=strip_prefix,
                      add_prefix=add_prefix, methods=tuple(normalized), websocket=websocket,
                      cooldown_seconds=float(cooldown), headers=headers, health=health,
                      required_scope=required_scope, inject_identity=inject_identity,
                      identity_secret=identity_secret)


def _parse_targets(item: dict, path: Path) -> list[Upstream]:
    """兼容两种写法：单个 target 字符串，或带权重的 targets 数组。"""
    if "target" in item and "targets" in item:
        raise ValueError(f"路由配置 {path} 里不能同时写 target 和 targets。")
    raw = item.get("targets")
    if raw is None:
        target = item.get("target")
        if not isinstance(target, str):
            raise ValueError(f"路由配置 {path} 里的每条路由都需要 target 或 targets。")
        raw = [{"url": target}]
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"路由配置 {path} 里的 targets 必须是非空数组。")

    targets: list[Upstream] = []
    seen: set[str] = set()
    for entry in raw:
        if isinstance(entry, str):
            url, weight = entry, 1
        elif isinstance(entry, dict):
            url, weight = entry.get("url"), entry.get("weight", 1)
        else:
            raise ValueError(f"路由配置 {path} 里的 targets 项必须是字符串或对象。")
        if not isinstance(url, str):
            raise ValueError(f"路由配置 {path} 里的 target url 必须是字符串。")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"target {url} 必须是完整的 http 或 https 地址。")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(f"target {url} 不应包含账号、密码、查询参数或片段。")
        if not isinstance(weight, int) or isinstance(weight, bool) or weight <= 0:
            raise ValueError(f"target {url} 的 weight 必须是正整数。")
        url = url.rstrip("/")
        if url in seen:
            raise ValueError(f"路由配置 {path} 里的 targets 出现了重复地址：{url}")
        seen.add(url)
        targets.append(Upstream(url=url, weight=weight))
    return targets


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
        self.health = HealthMonitor(routes, self.client)

    @property
    def health_task(self) -> asyncio.Task | None:
        return self.health.task

    def start(self) -> None:
        """有任意一条路由开启探活时，启动后台探活协程。"""
        self.health.start()

    async def close(self) -> None:
        await self.health.close()
        await self.client.aclose()

    # ---------------------------------------------------------------- HTTP

    async def forward(self, request: Request, route: ProxyRoute, remainder: str) -> StreamingResponse:
        if request.headers.get(LOOP_HEADER):
            raise GatewayError(508, "proxy_loop", "检测到代理回环：上游又指回了网关自己。")

        principal = getattr(request.state, "principal", None)
        headers = self._request_headers(request.headers, route, principal)
        body = await request.body()
        # POST 之类的请求可能已经有副作用，连接失败也不换上游重放。
        retry_allowed = request.method in IDEMPOTENT_METHODS
        attempts = self._attempts(route)
        last_error = "连接失败"

        for upstream in attempts:
            url = route.upstream_url(upstream, remainder, request.url.query)
            outgoing = self.client.build_request(request.method, url, headers=headers,
                                                 content=body or None)
            try:
                response = await self.client.send(outgoing, stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # 连接阶段就失败，说明请求没有被处理，可以安全地换下一个。
                last_error = type(exc).__name__
                self._cool(route, upstream, last_error)
                if not retry_allowed:
                    break
                continue
            except httpx.TimeoutException as exc:
                # 已经连上但迟迟没有响应：可能已在别处生效，不重试、也不冷却。
                raise GatewayError(504, "upstream_timeout",
                                   f"上游 {upstream.url} 响应超时，请检查该服务。") from exc
            except httpx.RequestError as exc:
                last_error = type(exc).__name__
                self._cool(route, upstream, last_error)
                if not retry_allowed:
                    break
                continue

            # 流式转发：不要把上游整个响应读进内存，否则 SSE 和大文件都会出问题。
            return StreamingResponse(
                response.aiter_raw(),
                status_code=response.status_code,
                headers={**self._response_headers(response.headers),
                         "X-Proxy-Upstream": upstream.url},
                background=BackgroundTask(response.aclose),
            )

        raise GatewayError(502, "upstream_unreachable",
                           f"无法连接上游（{len(attempts)} 个候选，最后一次：{last_error}）。")

    # ----------------------------------------------------------- WebSocket

    async def forward_websocket(self, websocket: WebSocket, route: ProxyRoute,
                                remainder: str, principal: dict | None = None) -> None:
        if websocket.headers.get(LOOP_HEADER):
            await websocket.send_json(_error_event(508, "proxy_loop",
                                                   "检测到代理回环：上游又指回了网关自己。"))
            await websocket.close(code=1008)
            return

        headers = {key: value for key, value in websocket.headers.items()
                   if key.lower() in WS_FORWARD_HEADERS
                   and key.lower() not in IDENTITY_HEADERS}
        if route.headers:
            headers.update(route.headers)
        headers.update(self._identity_headers(route, principal))
        subprotocols = list(websocket.scope.get("subprotocols") or [])

        connection = None
        last_status = 502
        # 网关票据只用于网关自己的准入，不能转发给上游（那是别人的凭证）。
        query = urlencode([(key, value) for key, value in
                           parse_qsl(websocket.url.query, keep_blank_values=True)
                           if key != "ticket"])
        for upstream in self._attempts(route):
            url = route.upstream_ws_url(upstream, remainder, query)
            try:
                # 先连上游再 accept：失败还能换下一个，accept 之后就只能将错就错。
                connection = await connect(url, additional_headers=headers or None,
                                           subprotocols=subprotocols or None,
                                           open_timeout=self.timeout, max_size=None)
                break
            except (TimeoutError, asyncio.TimeoutError):
                last_status = 504
                self._cool(route, upstream, "connect timeout")
            except (OSError, WebSocketException):
                last_status = 502
                self._cool(route, upstream, "connect error")

        if connection is None:
            await self._close_with_error(
                websocket, last_status,
                "upstream_timeout" if last_status == 504 else "upstream_unreachable",
                f"无法连接上游的 WebSocket（{len(route.targets)} 个候选）。")
            return

        close_code = None
        try:
            await websocket.accept(subprotocol=connection.subprotocol)
            await self._bridge(websocket, connection)
            close_code = connection.close_code
        except RuntimeError:
            # 客户端在 accept 之前就断开了。
            return
        finally:
            await connection.close()

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

    # ------------------------------------------------------------- 状态

    def status(self) -> dict:
        """给 /proxy-status 用的只读快照（探活状态由 HealthMonitor 提供）。"""
        return self.health.status()

    # ------------------------------------------------------------- 选路

    def _attempts(self, route: ProxyRoute) -> list[Upstream]:
        """返回本次的尝试顺序：健康且未冷却 → 其余未冷却 → 冷却中兜底。

        每组内部用平滑加权轮询；全部不健康时退化为"仍然从这些上游里试"，
        因为把整个前缀直接判死比偶尔打到坏上游更糟。
        """
        now = time.monotonic()
        pool = [upstream for upstream in route.targets
                if upstream.healthy and upstream.cooldown_until <= now]
        fallback = [upstream for upstream in route.targets
                    if not upstream.healthy and upstream.cooldown_until <= now]
        cooling = sorted((upstream for upstream in route.targets
                          if upstream.cooldown_until > now),
                         key=lambda upstream: upstream.cooldown_until)
        if not pool:
            pool, fallback = fallback, []
        if not pool:
            return cooling

        total = sum(upstream.weight for upstream in pool)
        chosen = None
        for upstream in pool:
            upstream.current_weight += upstream.weight
            if chosen is None or upstream.current_weight > chosen.current_weight:
                chosen = upstream
        chosen.current_weight -= total
        return [chosen] + [upstream for upstream in pool if upstream is not chosen] \
            + fallback + cooling

    def _cool(self, route: ProxyRoute, upstream: Upstream, reason: str) -> None:
        upstream.cooldown_until = time.monotonic() + route.cooldown_seconds
        logger.warning("Proxy %s -> %s cooling %.0fs (%s)",
                       route.prefix, upstream.url, route.cooldown_seconds, reason)

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _request_headers(incoming, route: ProxyRoute, principal: dict | None = None) -> dict[str, str]:
        # 先剥掉客户端自带的身份头，避免伪造；再按路由配置注入。
        headers = {key: value for key, value in incoming.items()
                   if key.lower() not in REQUEST_DROP
                   and key.lower() not in IDENTITY_HEADERS}
        if route.headers:
            headers.update(route.headers)
        headers[LOOP_HEADER] = "1"
        headers.update(Proxy._identity_headers(route, principal))
        return headers

    @staticmethod
    def _identity_headers(route: ProxyRoute, principal: dict | None) -> dict[str, str]:
        if not route.inject_identity or principal is None or not route.identity_secret:
            return {}
        user = principal.get("username") or ""
        roles = principal.get("role") or ""
        timestamp = str(int(time.time()))
        signature = hmac.new(
            route.identity_secret.encode("utf-8"),
            f"{user}\n{roles}\n{timestamp}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {"X-Auth-User": user, "X-Auth-Roles": roles,
                "X-Auth-Timestamp": timestamp, "X-Auth-Signature": signature}

    @staticmethod
    def _response_headers(incoming) -> dict[str, str]:
        return {key: value for key, value in incoming.items()
                if key.lower() not in HOP_BY_HOP}
