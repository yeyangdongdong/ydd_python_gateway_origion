"""管理员接口：API Key 管理与审计查询。"""

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from starlette.concurrency import run_in_threadpool

from ..auth import hash_api_key, new_api_key
from ..container import AppContext
from ..errors import GatewayError
from . import deps
from .models import ApiKeyCreate


def build_admin_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()
    auth = [Depends(deps.require_auth)]

    @router.get("/audit", dependencies=auth)
    async def list_audit(request: Request,
                         limit: int = Query(default=100, ge=1, le=1000),
                         offset: int = Query(default=0, ge=0),
                         action: str | None = Query(default=None, max_length=100),
                         result: str | None = Query(default=None, max_length=20)):
        """审计记录（新→旧），仅管理员。"""
        deps.require_admin(request)
        return await run_in_threadpool(ctx.audit.list, limit, offset,
                                       action=action, result=result)

    @router.get("/api-keys", dependencies=auth)
    async def list_api_keys(request: Request):
        deps.require_admin(request)
        rows = await run_in_threadpool(ctx.store.list_api_keys)
        return [{"id": row["id"], "name": row["name"], "prefix": row["key_prefix"],
                 "scopes": json.loads(row["scopes"] or "[]"),
                 "owner_id": row["owner_id"], "expires_at": row["expires_at"],
                 "created_at": row["created_at"], "last_used_at": row["last_used_at"],
                 "revoked": bool(row["revoked_at"])} for row in rows]

    @router.post("/api-keys", status_code=201, dependencies=auth)
    async def create_api_key(data: ApiKeyCreate, request: Request):
        deps.require_admin(request)
        # 允许自定义 scope（例如某个代理前缀的 reports），但只接受简单标识符。
        for scope in data.scopes:
            if not scope or len(scope) > 50 or not scope[0].isalpha() \
                    or not all(char.isalnum() or char in "_-:" for char in scope):
                raise GatewayError(422, "invalid_request",
                                   "scope 只能是以字母开头的字母/数字/-/_/: 组合。")
        key_id, secret, plaintext = new_api_key()
        now = datetime.now(timezone.utc)
        expires_at = ((now + timedelta(days=data.expires_in_days)).isoformat()
                      if data.expires_in_days else None)
        principal = deps.principal_of(request)
        await run_in_threadpool(ctx.store.create_api_key, {
            "id": key_id, "name": data.name, "key_hash": hash_api_key(secret),
            "key_prefix": secret[:8], "scopes": json.dumps(sorted(set(data.scopes))),
            "owner_id": principal["user_id"] or "",
            "expires_at": expires_at, "created_at": now.isoformat(),
        })
        await deps.record_audit(ctx, request, "api_key.create", target_type="api_key",
                                target_id=key_id,
                                details={"name": data.name,
                                         "scopes": sorted(set(data.scopes))})
        return {"id": key_id, "name": data.name, "key": plaintext,
                "prefix": secret[:8], "scopes": sorted(set(data.scopes)),
                "expires_at": expires_at}

    @router.delete("/api-keys/{key_id}", status_code=204, dependencies=auth)
    async def revoke_api_key(key_id: str, request: Request):
        deps.require_admin(request)
        removed = await run_in_threadpool(ctx.store.revoke_api_key, key_id)
        if not removed:
            raise GatewayError(404, "api_key_not_found", "API Key 不存在或已撤销。")
        await deps.record_audit(ctx, request, "api_key.revoke", target_type="api_key",
                                target_id=key_id)

    return router
