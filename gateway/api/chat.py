"""聊天（HTTP 流式返回 + WebSocket 会话）。"""

import asyncio
import logging
import time
from contextlib import aclosing, suppress
from uuid import uuid4

import anyio
from fastapi import APIRouter, Depends, Request, WebSocket
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

from ..access_log import log_websocket
from ..container import AppContext
from ..errors import GatewayError
from ..limits import limiter_key
from . import deps
from .models import ChatInput

logger = logging.getLogger(__name__)


def build_chat_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()
    auth = [Depends(deps.require_auth)]

    @router.post("/sessions/{session_id}/chat", dependencies=auth)
    async def chat(session_id: str, data: ChatInput, request: Request):
        await deps.owned_session(ctx, request, session_id)
        principal = deps.principal_of(request)
        async with ctx.chat_limiter.slot(limiter_key(principal)):
            async with aclosing(ctx.chat.stream(
                    session_id, data.content, data.request_id,
                    owner_id=deps.owner_scope(principal))) as events:
                async for event in events:
                    if event["type"] == "error":
                        # 与其它 HTTP 错误保持同一形状：{"error": {code, message}}，
                        # 会话/请求/运行编号放在外层，HTTP 状态码本身不用写进响应体。
                        body = {key: value for key, value in event.items()
                                if key not in {"type", "status"}}
                        return JSONResponse(body, status_code=event["status"])
                    if event["type"] == "done":
                        return event

    @router.websocket("/sessions/{session_id}/stream")
    async def stream(websocket: WebSocket, session_id: str):
        trace_id = uuid4().hex[:16]
        started = time.perf_counter()
        close_code = None
        principal = await deps.websocket_principal(ctx, websocket, f"session:{session_id}")
        if principal is None:
            await websocket.close(code=1008)
            return
        if principal["kind"] == "api_key" and "sessions" not in principal["scopes"]:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        ctx.metrics.ws_opened()
        log_websocket("ws.connect", session_id=session_id, trace_id=trace_id,
                      path=websocket.url.path)
        try:
            await run_in_threadpool(ctx.store.get_owned_session, session_id,
                                    deps.owner_scope(principal))
        except GatewayError as error:
            await websocket.send_json(deps.ws_error(session_id, error))
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
                    await websocket.send_json(deps.ws_error(session_id, GatewayError(
                        422, "invalid_request", "请输入有效的 JSON，包含非空 content。")))
                    continue
                try:
                    async with ctx.chat_limiter.slot(limiter_key(principal)):
                        async with aclosing(ctx.chat.stream(
                            session_id, data.content, data.request_id,
                            owner_id=deps.owner_scope(principal)
                        )) as events:
                            async for event in events:
                                await websocket.send_json(event)
                except GatewayError as error:
                    await websocket.send_json(deps.ws_error(
                        session_id, error, request_id=data.request_id))
                except WebSocketDisconnect:
                    return
                except Exception as error:
                    logger.error("Stream failed: %s", type(error).__name__)
                    # 先尽力说明为什么断开，再关闭；连接已坏时忽略发送失败。
                    with suppress(Exception):
                        await websocket.send_json(deps.ws_error(session_id, GatewayError(
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
                    close_code = 1003
                    await websocket.close(code=1003, reason="Only JSON text is supported")
                    break
                if len(raw) > 65536:
                    close_code = 1009
                    await websocket.close(code=1009)
                    break
                try:
                    queue.put_nowait(raw)
                except asyncio.QueueFull:
                    close_code = 1008
                    await websocket.close(code=1008, reason="Too many pending messages")
                    break
        finally:
            ctx.metrics.ws_closed()
            worker.cancel()
            # 等待业务协程写完中断记录，避免连接的外层取消再次打断清理。
            with anyio.CancelScope(shield=True):
                with suppress(asyncio.CancelledError, WebSocketDisconnect):
                    await worker
            log_websocket("ws.close", session_id=session_id, trace_id=trace_id,
                          duration_ms=round((time.perf_counter() - started) * 1000, 1),
                          close_code=close_code)

    return router
