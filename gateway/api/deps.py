"""鉴权、归属判断与审计包装；路由层只调用这里，不自己拼规则。

约定：`owner_id=None` 表示"不限制"（管理员 / 服务级 API Key）；
其他人的资源不匹配时统一按 404 处理，避免暴露资源是否存在。
"""

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import Header, Request, WebSocket
from starlette.concurrency import run_in_threadpool

from ..auth import (COOKIE_NAME, api_key_principal, hash_api_key, hash_token,
                    legacy_principal, parse_api_key, session_principal)
from ..container import AppContext
from ..errors import GatewayError
from ..logging_setup import TRACE_ID
from ..net import forwarded_ip

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
logger = logging.getLogger(__name__)


def get_context(connection: Request | WebSocket) -> AppContext:
    return connection.app.state.ctx


def token_matches(settings, authorization: str | None) -> bool:
    if not settings.gateway_token:
        return False
    expected = f"Bearer {settings.gateway_token}".encode("utf-8")
    return secrets.compare_digest((authorization or "").encode("utf-8"), expected)


def legacy_open(settings) -> bool:
    """legacy 模式且未设置 token：保持旧行为（本机演示免鉴权）。"""
    return settings.auth_mode == "legacy" and not settings.gateway_token


async def session_from_cookie(ctx: AppContext, token: str | None) -> dict | None:
    """校验会话 Cookie：库里只存 token 的 SHA-256；同时检查空闲与绝对过期。"""
    if not token:
        return None
    row = await run_in_threadpool(ctx.store.find_auth_session, hash_token(token))
    if row is None or row["revoked_at"] or row["user_status"] != "active":
        return None
    current = datetime.now(timezone.utc)
    if row["expires_at"] <= current.isoformat():
        await run_in_threadpool(ctx.store.revoke_auth_session, row["id"])
        return None
    last_seen = datetime.fromisoformat(row["last_seen_at"])
    if (current - last_seen).total_seconds() > ctx.settings.session_idle_seconds:
        await run_in_threadpool(ctx.store.revoke_auth_session, row["id"])
        return None
    # 每 60 秒才写一次 last_seen，避免每个请求都产生写事务。
    if (current - last_seen).total_seconds() > 60:
        await run_in_threadpool(ctx.store.touch_auth_session, row["id"], current.isoformat())
    return session_principal(row)


async def api_key_from_header(ctx: AppContext, authorization: str | None) -> dict | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    parsed = parse_api_key(authorization[7:].strip())
    if parsed is None:
        return None
    key_id, secret = parsed
    row = await run_in_threadpool(ctx.store.find_api_key, key_id)
    if row is None or row["revoked_at"]:
        return None
    if not secrets.compare_digest(hash_api_key(secret), row["key_hash"]):
        return None
    current = datetime.now(timezone.utc)
    if row["expires_at"] and row["expires_at"] <= current.isoformat():
        return None
    last_used = row["last_used_at"]
    if not last_used or (current - datetime.fromisoformat(last_used)).total_seconds() > 60:
        try:
            await run_in_threadpool(ctx.store.touch_api_key, key_id, current.isoformat())
        except Exception:
            pass
    return api_key_principal(row, frozenset(json.loads(row["scopes"] or "[]")))


async def authenticate(ctx: AppContext, request: Request,
                       authorization: str | None) -> dict | None:
    settings = ctx.settings
    if settings.auth_mode == "legacy":
        if token_matches(settings, authorization) or legacy_open(settings):
            return legacy_principal()
        return await api_key_from_header(ctx, authorization)
    principal = await session_from_cookie(ctx, request.cookies.get(COOKIE_NAME))
    if principal is not None:
        return principal
    # 本地模式下旧的共享 token 仍可用（映射为管理员），方便脚本平滑迁移。
    if token_matches(settings, authorization):
        return legacy_principal()
    return await api_key_from_header(ctx, authorization)


def enforce_csrf(ctx: AppContext, request: Request, principal: dict) -> None:
    """只有 Cookie 认证需要 CSRF；Bearer 头不会被浏览器自动带上。"""
    if principal["kind"] != "session" or request.method in SAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin:
        if origin.rstrip("/") in ctx.settings.cors_origins:
            # 已配置的 CORS 来源同时视为 CSRF 可信来源；浏览器侧仍受 CORS 限制。
            return
        from urllib.parse import urlsplit
        if urlsplit(origin).netloc != request.headers.get("host", ""):
            raise GatewayError(403, "csrf_rejected", "跨站请求被拒绝。")
        return
    site = request.headers.get("sec-fetch-site")
    if site and site not in {"same-origin", "none"}:
        raise GatewayError(403, "csrf_rejected", "跨站请求被拒绝。")


def enforce_scope(request: Request, principal: dict) -> None:
    """API Key 按资源级别授权；会话/Agent/审计各自需要对应 scope。"""
    path = request.url.path
    for prefix, scope in (("/sessions", "sessions"), ("/runs", "sessions"),
                          ("/agents", "agents"), ("/audit", "audit")):
        if path == prefix or path.startswith(prefix + "/"):
            if scope not in principal["scopes"]:
                raise GatewayError(403, "forbidden", f"该 API Key 缺少权限：{scope}")
            return
    if path.startswith("/api-keys"):
        raise GatewayError(403, "forbidden", "API Key 不能管理 API Key。")


async def require_auth(request: Request,
                       authorization: str | None = Header(default=None)) -> None:
    ctx = get_context(request)
    principal = await authenticate(ctx, request, authorization)
    if principal is None:
        raise GatewayError(401, "unauthorized",
                           "请先登录，或提供正确的 Authorization: Bearer <token>。")
    enforce_csrf(ctx, request, principal)
    request.state.principal = principal
    if principal["kind"] == "api_key":
        enforce_scope(request, principal)
    if principal["role"] == "viewer" and request.method not in SAFE_METHODS \
            and request.url.path != "/logout":
        raise GatewayError(403, "forbidden", "只读账号不能执行写操作。")


def principal_of(request: Request) -> dict:
    return request.state.principal


def require_admin(request: Request) -> None:
    principal = getattr(request.state, "principal", None)
    if principal is None or principal["role"] != "admin":
        raise GatewayError(403, "forbidden", "需要管理员权限。")


def user_public(user: dict) -> dict:
    return {key: user.get(key) for key in
            ("id", "username", "role", "status", "created_at", "last_seen_at")}


async def ensure_not_last_admin(ctx: AppContext, user: dict, *,
                                role: str | None = None,
                                status: str | None = None) -> None:
    if user["role"] != "admin" or user["status"] != "active":
        return
    final_role = role if role is not None else user["role"]
    final_status = status if status is not None else user["status"]
    if final_role == "admin" and final_status == "active":
        return
    if await run_in_threadpool(ctx.store.count_active_admins) <= 1:
        raise GatewayError(409, "last_admin", "不能停用或降级最后一个管理员。")


def owns(principal: dict, owner_id: str | None) -> bool:
    """admin 与管理级 API Key 可访问全部；其他人只能访问自己创建的资源。"""
    if principal["role"] == "admin" or principal["kind"] == "api_key":
        return True
    return bool(owner_id) and owner_id == principal["user_id"]


def owner_scope(principal: dict) -> str | None:
    """传给 storage/service 的归属过滤：None 表示不限。"""
    if principal["role"] == "admin" or principal["kind"] == "api_key":
        return None
    return principal["user_id"] or None


async def owned_session(ctx: AppContext, request: Request, session_id: str) -> dict:
    """读会话并校验归属；所有会话级入口都必须经过这里。"""
    principal = principal_of(request)
    return await run_in_threadpool(ctx.store.get_owned_session, session_id,
                                   owner_scope(principal))


async def websocket_principal(ctx: AppContext, websocket: WebSocket,
                              ticket_resource: str) -> dict | None:
    """WS 准入：Cookie 会话（浏览器）→ Bearer（脚本/旧 token/API Key）→ 一次性票据。"""
    settings = ctx.settings
    if settings.auth_mode == "local":
        principal = await session_from_cookie(ctx, websocket.cookies.get(COOKIE_NAME))
        if principal is not None:
            return principal
    if legacy_open(settings) or token_matches(settings,
                                              websocket.headers.get("authorization")):
        return legacy_principal()
    api = await api_key_from_header(ctx, websocket.headers.get("authorization"))
    if api is not None:
        return api
    ticket = websocket.query_params.get("ticket")
    if ticket and ctx.tickets.take(ticket) == ticket_resource:
        return legacy_principal()
    return None


def request_ip(request: Request) -> str:
    # Caddy 前置时对端是 127.0.0.1，真实 IP 在 X-Forwarded-For；只信任回环对端。
    peer = request.client.host if request.client else ""
    return forwarded_ip(peer, request.headers.get("x-forwarded-for"),
                        request.headers.get("x-real-ip"))


def audit_context(request: Request) -> dict:
    principal = getattr(request.state, "principal", None)
    ip = request_ip(request)
    if principal:
        return {"actor_type": principal["kind"],
                "actor_id": principal["user_id"] or principal["username"],
                "actor_name": principal["username"], "ip": ip,
                "trace_id": TRACE_ID.get()}
    return {"actor_type": "anonymous", "actor_id": "", "actor_name": "anonymous",
            "ip": ip, "trace_id": TRACE_ID.get()}


async def record_audit(ctx: AppContext, request: Request, action: str, *,
                       target_type: str = "", target_id: str = "",
                       details: dict | None = None, result: str = "ok",
                       reason: str = "") -> None:
    try:
        await run_in_threadpool(
            ctx.audit.append, action=action, **audit_context(request),
            target_type=target_type, target_id=target_id,
            details=details, result=result, reason=reason)
    except Exception:      # 审计是旁路：失败只记 ERROR，不改变业务结果
        ctx.metrics.audit_failure()
        logger.exception("Audit append failed: %s", action)


def ws_error(session_id: str, error: GatewayError, *, status: int | None = None,
             **identifiers: Any) -> dict:
    """WebSocket 统一的错误事件；不存在的标识符直接省略，而不是留 null。"""
    event = {"type": "error", "status": status or error.status_code,
             "session_id": session_id, "error": error.as_dict()}
    event.update({key: value for key, value in identifiers.items() if value is not None})
    return event
