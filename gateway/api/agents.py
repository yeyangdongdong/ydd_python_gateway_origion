"""Agent 管理：admin 看全部，member 只看自己的 + 默认助手。"""

from fastapi import APIRouter, Depends, Query, Request
from starlette.concurrency import run_in_threadpool

from ..container import AppContext
from ..errors import GatewayError
from . import deps
from .models import AGENT_PAGE, AgentCreate, AgentUpdate


def build_agents_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()
    auth = [Depends(deps.require_auth)]

    @router.get("/agents", dependencies=auth)
    async def list_agents(request: Request,
                          limit: int = Query(default=AGENT_PAGE, ge=1, le=1000),
                          offset: int = Query(default=0, ge=0)):
        principal = deps.principal_of(request)
        owner = None if principal["role"] == "admin" or principal["kind"] == "api_key" \
            else principal["user_id"]
        return await run_in_threadpool(ctx.store.list_agents, limit, offset, owner)

    @router.post("/agents", status_code=201, dependencies=auth)
    async def create_agent(data: AgentCreate, request: Request):
        principal = deps.principal_of(request)
        agent = await run_in_threadpool(ctx.store.create_agent, data.name,
                                        data.system_prompt,
                                        data.model or ctx.settings.model,
                                        principal["user_id"] or None)
        await deps.record_audit(ctx, request, "agent.create", target_type="agent",
                                target_id=agent["id"],
                                details={"name": agent["name"], "model": agent["model"]})
        return agent

    @router.patch("/agents/{agent_id}", dependencies=auth)
    async def update_agent(agent_id: str, data: AgentUpdate, request: Request):
        if data.name is None and data.system_prompt is None and data.model is None:
            raise GatewayError(422, "invalid_request", "请至少提供一个要修改的字段。")
        principal = deps.principal_of(request)
        existing = await run_in_threadpool(ctx.store.get_agent, agent_id)
        if not deps.owns(principal, existing.get("owner_id")):
            raise GatewayError(404, "agent_not_found", "Agent 不存在。")
        agent = await run_in_threadpool(ctx.store.update_agent, agent_id,
                                        name=data.name, system_prompt=data.system_prompt,
                                        model=data.model)
        changed = [field for field in ("name", "system_prompt", "model")
                   if getattr(data, field) is not None]
        await deps.record_audit(ctx, request, "agent.update", target_type="agent",
                                target_id=agent_id, details={"fields": changed})
        return agent

    @router.delete("/agents/{agent_id}", status_code=204, dependencies=auth)
    async def delete_agent(agent_id: str, request: Request):
        principal = deps.principal_of(request)
        existing = await run_in_threadpool(ctx.store.get_agent, agent_id)
        if not deps.owns(principal, existing.get("owner_id")):
            raise GatewayError(404, "agent_not_found", "Agent 不存在。")
        await run_in_threadpool(ctx.store.delete_agent, agent_id)
        await deps.record_audit(ctx, request, "agent.delete", target_type="agent",
                                target_id=agent_id)

    return router
