"""网络入口：校验输入、调用业务逻辑、把结果返回给客户端。"""

import asyncio
import logging
import secrets
from contextlib import aclosing, asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import anyio
from fastapi import Depends, FastAPI, Header, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

from .config import Settings
from .errors import GatewayError
from .provider import ModelProvider
from .proxy import Proxy, load_routes, match_route
from .service import ChatService
from .storage import Store
from .tickets import TicketStore

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# 列表分页的默认值集中放在这里，接口定义读起来更直观。
AGENT_PAGE = 100
SESSION_PAGE = 100
MESSAGE_PAGE = 1000

CHAT_PAGE = Path(__file__).resolve().parent.parent / "web" / "chat.html"


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentCreate(InputModel):
    name: Text = Field(max_length=100)
    system_prompt: Text = Field(default="你是一个友好、准确的中文助手。", max_length=16000)
    model: Text | None = Field(default=None, max_length=200)


class AgentUpdate(InputModel):
    # None 表示这一项不修改；全部省略会被视为无效请求。
    name: Text | None = Field(default=None, max_length=100)
    system_prompt: Text | None = Field(default=None, max_length=16000)
    model: Text | None = Field(default=None, max_length=200)


class SessionCreate(InputModel):
    agent_id: Text = Field(default="default", max_length=100)
    title: Text = Field(default="新会话", max_length=200)


class SessionUpdate(InputModel):
    title: Text = Field(max_length=200)


class ChatInput(InputModel):
    content: Text = Field(max_length=32000)
    request_id: Text = Field(default_factory=lambda: uuid4().hex, max_length=100)


class ProxyTicketInput(InputModel):
    # 要连接的代理 WS 路径，必须落在已配置的前缀下。
    path: Text = Field(max_length=2000)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.db_path)
    tickets = TicketStore()
    # 启动时就把路由表读进内存：配置写错立刻报错，而不是等第一个请求。
    routes = load_routes(settings.routes_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await run_in_threadpool(store.initialize, settings.model)
        provider = ModelProvider(settings)
        app.state.chat = ChatService(store, provider, settings)
        app.state.proxy = Proxy(routes, settings.proxy_timeout) if routes else None
        if app.state.proxy is not None:
            app.state.proxy.start()
        try:
            yield
        finally:
            await provider.close()
            if app.state.proxy is not None:
                await app.state.proxy.close()

    app = FastAPI(title="ydd_python_gateway_origion", version="0.1.0",
                  description="易于理解的 Python Agent Gateway 基础版。", lifespan=lifespan)

    def authorized(authorization: str | None) -> bool:
        if not settings.gateway_token:
            return True
        expected = f"Bearer {settings.gateway_token}".encode("utf-8")
        return secrets.compare_digest((authorization or "").encode("utf-8"), expected)

    def accepts_ticket(session_id: str, ticket: str | None) -> bool:
        # 票据一次性使用，且只对签发时绑定的资源有效。
        return ticket is not None and tickets.take(ticket) == f"session:{session_id}"

    def ws_error(session_id: str, error: GatewayError, *, status: int | None = None,
                 **identifiers: str | None) -> dict:
        """WebSocket 统一的错误事件。

        形状固定为 {type, status, session_id, error:{code,message}}，已知的 request_id /
        run_id 追加在外层；不存在的标识符直接省略，而不是留 null。
        """
        event = {"type": "error", "status": status or error.status_code,
                 "session_id": session_id, "error": error.as_dict()}
        event.update({key: value for key, value in identifiers.items() if value is not None})
        return event

    async def require_auth(authorization: str | None = Header(default=None)) -> None:
        if not authorized(authorization):
            raise GatewayError(401, "unauthorized", "请提供正确的 Authorization: Bearer <token>。")

    auth = [Depends(require_auth)]

    @app.exception_handler(GatewayError)
    async def handle_gateway_error(request: Request, error: GatewayError):
        return JSONResponse(error.as_body(), status_code=error.status_code)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, error: RequestValidationError):
        # 默认校验错误会回显输入。这里只说明规则，避免把用户文本复制到错误中。
        return JSONResponse(
            GatewayError(422, "invalid_request",
                         "参数无效，请按 /docs 中的字段类型和长度提交请求。").as_body(),
            status_code=422)

    @app.get("/")
    async def chat_page():
        # 页面本身不含任何密钥，只是让浏览器能打开聊天界面；数据仍需 token。
        return FileResponse(CHAT_PAGE, media_type="text/html",
                            headers={"Cache-Control": "no-store"})

    @app.get("/health")
    async def health():
        # auth_required 供页面提示“是否需要填写 token”，不是一个安全边界。
        return {"status": "ok", "provider": settings.provider,
                "auth_required": bool(settings.gateway_token)}

    @app.get("/proxy-status", dependencies=auth)
    async def proxy_status():
        """反向代理的只读状态：每个上游是否健康、是否在冷却、最近一次错误。"""
        proxy = app.state.proxy
        if proxy is None:
            return {"enabled": False, "routes": []}
        return {"enabled": True, **proxy.status()}

    @app.get("/agents", dependencies=auth)
    async def list_agents(limit: int = Query(default=AGENT_PAGE, ge=1, le=1000),
                          offset: int = Query(default=0, ge=0)):
        return await run_in_threadpool(store.list_agents, limit, offset)

    @app.post("/agents", status_code=201, dependencies=auth)
    async def create_agent(data: AgentCreate):
        return await run_in_threadpool(store.create_agent, data.name, data.system_prompt,
                                      data.model or settings.model)

    @app.patch("/agents/{agent_id}", dependencies=auth)
    async def update_agent(agent_id: str, data: AgentUpdate):
        if data.name is None and data.system_prompt is None and data.model is None:
            raise GatewayError(422, "invalid_request", "请至少提供一个要修改的字段。")
        return await run_in_threadpool(store.update_agent, agent_id, name=data.name,
                                       system_prompt=data.system_prompt, model=data.model)

    @app.delete("/agents/{agent_id}", status_code=204, dependencies=auth)
    async def delete_agent(agent_id: str):
        await run_in_threadpool(store.delete_agent, agent_id)

    @app.get("/sessions", dependencies=auth)
    async def list_sessions(limit: int = Query(default=SESSION_PAGE, ge=1, le=1000),
                            offset: int = Query(default=0, ge=0)):
        return await run_in_threadpool(store.list_sessions, limit, offset)

    @app.post("/sessions", status_code=201, dependencies=auth)
    async def create_session(data: SessionCreate):
        return await run_in_threadpool(store.create_session, data.agent_id, data.title)

    @app.get("/sessions/{session_id}", dependencies=auth)
    async def get_session(session_id: str):
        return await run_in_threadpool(store.get_session, session_id)

    @app.patch("/sessions/{session_id}", dependencies=auth)
    async def update_session(session_id: str, data: SessionUpdate):
        return await run_in_threadpool(store.update_session, session_id, title=data.title)

    @app.delete("/sessions/{session_id}", status_code=204, dependencies=auth)
    async def delete_session(session_id: str):
        await run_in_threadpool(store.delete_session, session_id)

    @app.get("/sessions/{session_id}/messages", dependencies=auth)
    async def get_messages(session_id: str,
                           limit: int = Query(default=MESSAGE_PAGE, ge=1, le=5000),
                           offset: int = Query(default=0, ge=0)):
        await run_in_threadpool(store.get_session, session_id)
        return await run_in_threadpool(store.messages, session_id, limit=limit, offset=offset)

    @app.delete("/sessions/{session_id}/messages", dependencies=auth)
    async def clear_messages(session_id: str):
        deleted = await run_in_threadpool(store.clear_messages, session_id)
        return {"deleted": deleted}

    @app.get("/sessions/{session_id}/requests/{request_id}", dependencies=auth)
    async def get_request(session_id: str, request_id: str):
        await run_in_threadpool(store.get_session, session_id)
        run = await run_in_threadpool(store.find_request, session_id, request_id)
        if run is None:
            raise GatewayError(404, "request_not_found", "请求尚未开始或不存在。")
        return run

    @app.get("/runs/{run_id}", dependencies=auth)
    async def get_run(run_id: str):
        return await run_in_threadpool(store.get_run, run_id)

    @app.post("/sessions/{session_id}/ticket", dependencies=auth)
    async def create_ticket(session_id: str):
        """给浏览器换一张连接 WebSocket 的一次性票据。

        这一步用普通 HTTP 完成，因此可以携带 Authorization 头；
        拿到票据后，页面就能连 `.../stream?ticket=...`。
        """
        await run_in_threadpool(store.get_session, session_id)
        value, seconds = tickets.issue(f"session:{session_id}")
        return {"ticket": value, "session_id": session_id, "expires_in": seconds}

    @app.post("/proxy-ticket", dependencies=auth)
    async def create_proxy_ticket(data: ProxyTicketInput):
        """给浏览器换一张连接"被代理的 WebSocket"的一次性票据。

        浏览器设不了握手头，所以先用普通 HTTP 取票，再带 ?ticket= 连接；
        票据只对这条路径有效，且只能用一次。
        """
        if not data.path.startswith("/"):
            raise GatewayError(422, "invalid_request", "path 必须以 / 开头。")
        if match_route(routes, data.path) is None:
            raise GatewayError(404, "route_not_found", "该路径没有配置反向代理。")
        value, seconds = tickets.issue(f"ws:{data.path}")
        return {"ticket": value, "path": data.path, "expires_in": seconds}

    @app.post("/sessions/{session_id}/chat", dependencies=auth)
    async def chat(session_id: str, data: ChatInput):
        async with aclosing(app.state.chat.stream(session_id, data.content, data.request_id)) as events:
            async for event in events:
                if event["type"] == "error":
                    # 与其它 HTTP 错误保持同一形状：{"error": {code, message}}，
                    # 会话/请求/运行编号放在外层，HTTP 状态码本身不用写进响应体。
                    body = {key: value for key, value in event.items()
                            if key not in {"type", "status"}}
                    return JSONResponse(body, status_code=event["status"])
                if event["type"] == "done":
                    return event

    @app.websocket("/sessions/{session_id}/stream")
    async def stream(websocket: WebSocket, session_id: str):
        if not authorized(websocket.headers.get("authorization")):
            # 浏览器原生 WebSocket 设不了请求头，所以改用一次性票据准入。
            if not accepts_ticket(session_id, websocket.query_params.get("ticket")):
                await websocket.close(code=1008)
                return
        await websocket.accept()
        try:
            await run_in_threadpool(store.get_session, session_id)
        except GatewayError as error:
            await websocket.send_json(ws_error(session_id, error))
            await websocket.close(code=1008)
            return

        # 接收和执行分开：即使模型很慢，也能及时发现客户端断开。
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=8)

        async def respond():
            while True:
                raw = await queue.get()
                try:
                    data = ChatInput.model_validate_json(raw)
                except ValidationError:
                    await websocket.send_json(ws_error(session_id, GatewayError(
                        422, "invalid_request", "请输入有效的 JSON，包含非空 content。")))
                    continue
                try:
                    async with aclosing(app.state.chat.stream(
                        session_id, data.content, data.request_id
                    )) as events:
                        async for event in events:
                            await websocket.send_json(event)
                except GatewayError as error:
                    await websocket.send_json(ws_error(session_id, error,
                                                       request_id=data.request_id))
                except WebSocketDisconnect:
                    return
                except Exception as error:
                    logging.getLogger(__name__).error("Stream failed: %s", type(error).__name__)
                    # 先尽力说明为什么断开，再关闭；连接已坏时忽略发送失败。
                    with suppress(Exception):
                        await websocket.send_json(ws_error(session_id, GatewayError(
                            500, "internal_error", "本轮处理失败，请检查服务日志。")))
                    await websocket.close(code=1011)
                    return

        worker = asyncio.create_task(respond())
        try:
            while True:
                frame = await websocket.receive()
                if frame["type"] == "websocket.disconnect":
                    break
                raw = frame.get("text")
                if raw is None:
                    await websocket.close(code=1003, reason="Only JSON text is supported")
                    break
                if len(raw) > 65536:
                    await websocket.close(code=1009)
                    break
                try:
                    queue.put_nowait(raw)
                except asyncio.QueueFull:
                    await websocket.close(code=1008, reason="Too many pending messages")
                    break
        finally:
            worker.cancel()
            # 等待业务协程写完中断记录，避免连接的外层取消再次打断清理。
            with anyio.CancelScope(shield=True):
                with suppress(asyncio.CancelledError, WebSocketDisconnect):
                    await worker

    # 可选的反向代理：按 routes.json 为每个前缀注册 HTTP 与 WebSocket 转发路由。
    # 放在最后注册，内置接口优先，不会被前缀遮蔽（加载时也已拒绝冲突前缀）。
    def make_forwarder(route):
        async def forward(request: Request, path: str) -> Response:
            return await request.app.state.proxy.forward(request, route, path)
        return forward

    def make_ws_forwarder(route):
        async def forward(websocket: WebSocket, path: str) -> None:
            # 优先用握手头；浏览器带不了头，就用 /proxy-ticket 换来的票据（绑定到这条路径）。
            if not authorized(websocket.headers.get("authorization")):
                ticket = websocket.query_params.get("ticket")
                if not (ticket and tickets.take(ticket) == f"ws:{websocket.url.path}"):
                    await websocket.close(code=1008)
                    return
            await websocket.app.state.proxy.forward_websocket(websocket, route, path)
        return forward

    for index, route in enumerate(routes):
        handler = make_forwarder(route)
        handler.__name__ = f"proxy_{index}"
        # 每个方法单独注册：FastAPI 对多方法路由只生成一个 operationId，
        # 拆开之后 /docs 与代码生成工具里的每个入口都是唯一的。
        for method in route.methods:
            app.api_route(route.prefix + "{path:path}", methods=[method],
                          name=f"proxy_{index}_{method.lower()}",
                          dependencies=auth)(handler)

        if route.websocket:
            ws_handler = make_ws_forwarder(route)
            ws_handler.__name__ = f"proxy_ws_{index}"
            app.add_api_websocket_route(route.prefix + "{path:path}", ws_handler,
                                        name=f"proxy_ws_{index}")

    return app
