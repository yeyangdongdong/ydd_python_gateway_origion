"""会话、消息、运行查询与 WS 票据。"""

from fastapi import APIRouter, Depends, Query, Request
from starlette.concurrency import run_in_threadpool

from ..container import AppContext
from ..errors import GatewayError
from . import deps
from .models import MESSAGE_PAGE, SESSION_PAGE, SessionCreate, SessionUpdate


def build_sessions_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()
    auth = [Depends(deps.require_auth)]

    @router.get("/sessions", dependencies=auth)
    async def list_sessions(request: Request,
                            limit: int = Query(default=SESSION_PAGE, ge=1, le=1000),
                            offset: int = Query(default=0, ge=0)):
        principal = deps.principal_of(request)
        owner = None if principal["role"] == "admin" or principal["kind"] == "api_key" \
            else principal["user_id"]
        return await run_in_threadpool(ctx.store.list_sessions, limit, offset, owner)

    @router.post("/sessions", status_code=201, dependencies=auth)
    async def create_session(data: SessionCreate, request: Request):
        principal = deps.principal_of(request)
        agent = await run_in_threadpool(ctx.store.get_agent, data.agent_id)
        if agent.get("owner_id") and not deps.owns(principal, agent["owner_id"]):
            raise GatewayError(404, "agent_not_found", "Agent 不存在，请先创建 Agent。")
        return await run_in_threadpool(ctx.store.create_session, data.agent_id, data.title,
                                       principal["user_id"] or None)

    @router.get("/sessions/{session_id}", dependencies=auth)
    async def get_session(session_id: str, request: Request):
        return await deps.session_for(ctx, request, session_id)

    @router.patch("/sessions/{session_id}", dependencies=auth)
    async def update_session(session_id: str, data: SessionUpdate, request: Request):
        await deps.session_for(ctx, request, session_id)
        session = await run_in_threadpool(ctx.store.update_session, session_id,
                                          title=data.title)
        await deps.record_audit(ctx, request, "session.update", target_type="session",
                                target_id=session_id, details={"fields": ["title"]})
        return session

    @router.delete("/sessions/{session_id}", status_code=204, dependencies=auth)
    async def delete_session(session_id: str, request: Request):
        await deps.session_for(ctx, request, session_id)
        await run_in_threadpool(ctx.store.delete_session, session_id)
        await deps.record_audit(ctx, request, "session.delete", target_type="session",
                                target_id=session_id)

    @router.get("/sessions/{session_id}/messages", dependencies=auth)
    async def get_messages(session_id: str, request: Request,
                           limit: int = Query(default=MESSAGE_PAGE, ge=1, le=5000),
                           offset: int = Query(default=0, ge=0)):
        await deps.session_for(ctx, request, session_id)
        return await run_in_threadpool(ctx.store.messages, session_id,
                                       limit=limit, offset=offset)

    @router.delete("/sessions/{session_id}/messages", dependencies=auth)
    async def clear_messages(session_id: str, request: Request):
        await deps.session_for(ctx, request, session_id)
        deleted = await run_in_threadpool(ctx.store.clear_messages, session_id)
        await deps.record_audit(ctx, request, "session.clear", target_type="session",
                                target_id=session_id, details={"deleted": deleted})
        return {"deleted": deleted}

    @router.get("/sessions/{session_id}/requests/{request_id}", dependencies=auth)
    async def get_request(session_id: str, request_id: str, request: Request):
        await deps.session_for(ctx, request, session_id)
        run = await run_in_threadpool(ctx.store.find_request, session_id, request_id)
        if run is None:
            raise GatewayError(404, "request_not_found", "请求尚未开始或不存在。")
        return run

    @router.get("/runs/{run_id}", dependencies=auth)
    async def get_run(run_id: str, request: Request):
        run = await run_in_threadpool(ctx.store.get_run, run_id)
        session = await run_in_threadpool(ctx.store.get_session, run["session_id"])
        deps.ensure_session_access(deps.principal_of(request), session)
        return run

    @router.post("/sessions/{session_id}/ticket", dependencies=auth)
    async def create_ticket(session_id: str, request: Request):
        """给浏览器换一张连接 WebSocket 的一次性票据。"""
        await deps.session_for(ctx, request, session_id)
        value, seconds = ctx.tickets.issue(f"session:{session_id}")
        return {"ticket": value, "session_id": session_id, "expires_in": seconds}

    return router
