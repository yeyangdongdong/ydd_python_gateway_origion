"""管理员接口：API Key 管理与审计查询。"""

import json
import math
from datetime import datetime, timedelta, timezone
from ipaddress import ip_network

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

from ..auth import hash_api_key, new_api_key
from ..container import AppContext
from ..errors import GatewayError
from . import deps
from .models import ApiKeyCreate, ApiKeyUpdate


def _days_left(expires_at: str | None) -> int | None:
    if not expires_at:
        return None
    try:
        remaining = (datetime.fromisoformat(expires_at)
                     - datetime.now(timezone.utc)).total_seconds()
    except ValueError:
        return None
    return max(0, math.ceil(remaining / 86400))


def _normalize_cidrs(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    normalized: list[str] = []
    for value in values:
        try:
            network = ip_network(value.strip(), strict=False)
        except ValueError as error:
            raise GatewayError(422, "invalid_request",
                               f"无效的 IP/CIDR：{value}") from error
        text = str(network)
        if text not in normalized:
            normalized.append(text)
    return normalized


def _key_public(row: dict) -> dict:
    return {"id": row["id"], "name": row["name"], "prefix": row["key_prefix"],
            "scopes": json.loads(row["scopes"] or "[]"),
            "allowed_cidrs": json.loads(row.get("allowed_cidrs") or "[]"),
            "owner_id": row["owner_id"], "expires_at": row["expires_at"],
            "days_left": _days_left(row["expires_at"]),
            "created_at": row["created_at"], "last_used_at": row["last_used_at"],
            "revoked": bool(row["revoked_at"])}


def build_admin_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()
    auth = [Depends(deps.require_auth)]

    @router.get("/metrics", dependencies=auth, response_class=PlainTextResponse)
    async def metrics(request: Request):
        """Prometheus 文本格式；管理员或带 metrics scope 的 API Key 可读。"""
        principal = deps.principal_of(request)
        if principal["kind"] == "api_key":
            if "metrics" not in principal["scopes"]:
                raise GatewayError(403, "forbidden", "该 API Key 缺少权限：metrics")
        elif principal["role"] != "admin":
            raise GatewayError(403, "forbidden", "需要管理员权限。")
        return PlainTextResponse(ctx.metrics.render())

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
        return [_key_public(row) for row in rows]

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
        days = data.expires_in_days if data.expires_in_days is not None \
            else ctx.settings.api_key_default_days
        expires_at = (now + timedelta(days=days)).isoformat() if days > 0 else None
        allowed_cidrs = _normalize_cidrs(data.allowed_cidrs) or []
        principal = deps.principal_of(request)
        await run_in_threadpool(ctx.store.create_api_key, {
            "id": key_id, "name": data.name, "key_hash": hash_api_key(secret),
            "key_prefix": secret[:8], "scopes": json.dumps(sorted(set(data.scopes))),
            "owner_id": principal["user_id"] or "",
            "expires_at": expires_at, "created_at": now.isoformat(),
            "allowed_cidrs": json.dumps(allowed_cidrs),
        })
        await deps.record_audit(ctx, request, "api_key.create", target_type="api_key",
                                target_id=key_id,
                                details={"name": data.name,
                                         "scopes": sorted(set(data.scopes)),
                                         "expires_at": expires_at,
                                         "allowed_cidrs": allowed_cidrs})
        return {"id": key_id, "name": data.name, "key": plaintext,
                "prefix": secret[:8], "scopes": sorted(set(data.scopes)),
                "expires_at": expires_at, "days_left": _days_left(expires_at),
                "allowed_cidrs": allowed_cidrs}

    @router.patch("/api-keys/{key_id}", dependencies=auth)
    async def update_api_key(key_id: str, data: ApiKeyUpdate, request: Request):
        deps.require_admin(request)
        if data.name is None and data.allowed_cidrs is None:
            raise GatewayError(422, "invalid_request", "请至少提供一个要修改的字段。")
        allowed = _normalize_cidrs(data.allowed_cidrs)
        row = await run_in_threadpool(
            ctx.store.update_api_key, key_id, name=data.name,
            allowed_cidrs=None if allowed is None else json.dumps(allowed))
        await deps.record_audit(ctx, request, "api_key.update", target_type="api_key",
                                target_id=key_id,
                                details={"fields": [key for key, value in
                                                    (("name", data.name),
                                                     ("allowed_cidrs", data.allowed_cidrs))
                                                    if value is not None]})
        return _key_public(row)

    @router.delete("/api-keys/{key_id}", status_code=204, dependencies=auth)
    async def revoke_api_key(key_id: str, request: Request):
        deps.require_admin(request)
        removed = await run_in_threadpool(ctx.store.revoke_api_key, key_id)
        if not removed:
            raise GatewayError(404, "api_key_not_found", "API Key 不存在或已撤销。")
        await deps.record_audit(ctx, request, "api_key.revoke", target_type="api_key",
                                target_id=key_id)

    return router
