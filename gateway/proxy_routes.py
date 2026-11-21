"""反向代理：状态、票据与按 routes.json 动态注册的转发路由。"""

from fastapi import APIRouter, Depends, Request, WebSocket
from fastapi.responses import Response

from .api import deps
from .container import AppContext
from .errors import GatewayError
from .proxy import match_route
from .api.models import ProxyTicketInput


def build_proxy_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()
    auth = [Depends(deps.require_auth)]

    @router.get("/proxy-status", dependencies=auth)
    async def proxy_status(request: Request):
        """反向代理的只读状态：每个上游是否健康、是否在冷却、最近一次错误。"""
        deps.require_admin(request)
        if ctx.proxy is None:
            return {"enabled": False, "routes": []}
        return {"enabled": True, **ctx.proxy.status()}

    @router.post("/proxy-ticket", dependencies=auth)
    async def create_proxy_ticket(data: ProxyTicketInput):
        """给浏览器换一张连接"被代理的 WebSocket"的一次性票据。

        浏览器设不了握手头，所以先用普通 HTTP 取票，再带 ?ticket= 连接；
        票据只对这条路径有效，且只能用一次。
        """
        if not data.path.startswith("/"):
            raise GatewayError(422, "invalid_request", "path 必须以 / 开头。")
        if match_route(ctx.routes, data.path) is None:
            raise GatewayError(404, "route_not_found", "该路径没有配置反向代理。")
        value, seconds = ctx.tickets.issue(f"ws:{data.path}")
        return {"ticket": value, "path": data.path, "expires_in": seconds}

    return router


def service_name(route) -> str:
    raw = route.prefix.strip("/").split("/")[0]
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in raw) or "proxy"


def register_forwarders(app, ctx: AppContext) -> None:
    """放在内置接口之后注册；每个方法单独注册，保证 operationId 唯一。"""
    auth = [Depends(deps.require_auth)]

    def make_forwarder(route):
        async def forward(request: Request, path: str) -> Response:
            principal = deps.principal_of(request)
            if principal["kind"] == "api_key":
                if "proxy" not in principal["scopes"] and \
                        route.required_scope not in principal["scopes"]:
                    raise GatewayError(403, "forbidden", "该 API Key 缺少代理权限。")
            # 访问日志中间件据此额外写一份 services/<name>/。
            request.scope["proxy_service"] = service_name(route)
            return await request.app.state.ctx.proxy.forward(request, route, path)
        return forward

    def make_ws_forwarder(route):
        async def forward(websocket: WebSocket, path: str) -> None:
            principal = await deps.websocket_principal(
                ctx, websocket, f"ws:{websocket.url.path}")
            if principal is None:
                await websocket.close(code=1008)
                return
            if principal["kind"] == "api_key" and "proxy" not in principal["scopes"] \
                    and route.required_scope not in principal["scopes"]:
                await websocket.close(code=1008)
                return
            await websocket.app.state.ctx.proxy.forward_websocket(
                websocket, route, path, principal)
        return forward

    for index, route in enumerate(ctx.routes):
        handler = make_forwarder(route)
        handler.__name__ = f"proxy_{index}"
        for method in route.methods:
            app.api_route(route.prefix + "{path:path}", methods=[method],
                          name=f"proxy_{index}_{method.lower()}",
                          dependencies=auth)(handler)

        if route.websocket:
            ws_handler = make_ws_forwarder(route)
            ws_handler.__name__ = f"proxy_ws_{index}"
            app.add_api_websocket_route(route.prefix + "{path:path}", ws_handler,
                                        name=f"proxy_ws_{index}")
