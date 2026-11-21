"""登录、登出、认证状态与自助改密。"""

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from ..auth import (COOKIE_NAME, DUMMY_HASH, hash_password, hash_token,
                    new_session_token, validate_password, verify_password)
from ..container import AppContext
from ..errors import GatewayError
from ..logging_setup import TRACE_ID
from . import deps
from .models import LOGIN_PAGE, PasswordChange

logger = logging.getLogger(__name__)


def build_auth_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()
    settings = ctx.settings

    @router.get("/login")
    async def login_page():
        if settings.auth_mode != "local":
            raise GatewayError(404, "not_found", "当前为 legacy 认证模式，不使用登录页。")
        return FileResponse(LOGIN_PAGE, media_type="text/html",
                            headers={"Cache-Control": "no-store"})

    def check_login_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
            raise GatewayError(403, "csrf_rejected", "跨站登录请求被拒绝。")

    @router.post("/login")
    async def login(request: Request):
        if settings.auth_mode != "local":
            raise GatewayError(404, "not_found", "当前为 legacy 认证模式，不使用登录。")
        check_login_origin(request)
        form = parse_qs((await request.body()).decode("utf-8", "replace"))
        username = (form.get("username") or [""])[0].strip()
        password = (form.get("password") or [""])[0]
        next_path = (form.get("next") or ["/"])[0]
        if not next_path.startswith("/") or next_path.startswith("//"):
            next_path = "/"
        ip = deps.request_ip(request)

        retry_after = ctx.login_limiter.retry_after(ip, username)
        if retry_after:
            await deps.record_audit(ctx, request, "auth.login", result="failed",
                                    reason="locked",
                                    details={"username": username, "retry_after": retry_after})
            return RedirectResponse("/login?error=locked", status_code=303)

        user = await run_in_threadpool(ctx.store.find_user, username=username)
        password_ok = await run_in_threadpool(verify_password, password,
                                              user["password_hash"] if user else DUMMY_HASH)
        if user is None or user["status"] != "active" or not password_ok:
            ctx.login_limiter.record_failure(ip, username)
            await deps.record_audit(ctx, request, "auth.login", result="failed",
                                    reason="invalid", details={"username": username})
            return RedirectResponse("/login?error=invalid", status_code=303)

        ctx.login_limiter.record_success(ip, username)
        token = new_session_token()
        now = datetime.now(timezone.utc)
        session = {
            "id": uuid4().hex, "user_id": user["id"], "token_hash": hash_token(token),
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=settings.session_absolute_seconds)).isoformat(),
            "last_seen_at": now.isoformat(), "ip": ip,
            "user_agent": (request.headers.get("user-agent") or "")[:200],
        }
        await run_in_threadpool(ctx.store.create_auth_session, session)
        try:
            await run_in_threadpool(ctx.audit.append, action="auth.login",
                                    actor_type="user", actor_id=user["id"],
                                    actor_name=user["username"], ip=ip,
                                    trace_id=TRACE_ID.get(), details={"username": username})
        except Exception:
            logger.exception("Login audit failed")

        response = RedirectResponse(next_path, status_code=303)
        response.set_cookie(COOKIE_NAME, token,
                            max_age=settings.session_absolute_seconds,
                            httponly=True, samesite="lax",
                            secure=settings.cookie_secure, path="/")
        return response

    @router.post("/logout", dependencies=[Depends(deps.require_auth)])
    async def logout(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        if token:
            row = await run_in_threadpool(ctx.store.find_auth_session, hash_token(token))
            if row is not None:
                await run_in_threadpool(ctx.store.revoke_auth_session, row["id"])
        await deps.record_audit(ctx, request, "auth.logout")
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @router.get("/auth/status")
    async def auth_status(request: Request):
        principal = None
        if settings.auth_mode == "local":
            principal = await deps.session_from_cookie(ctx, request.cookies.get(COOKIE_NAME))
        return {"auth_mode": settings.auth_mode,
                "authenticated": principal is not None,
                "user": None if principal is None else {
                    "id": principal["user_id"], "username": principal["username"],
                    "role": principal["role"]}}

    @router.post("/auth/password", dependencies=[Depends(deps.require_auth)])
    async def change_password(data: PasswordChange, request: Request):
        principal = deps.principal_of(request)
        if principal["kind"] != "session":
            raise GatewayError(403, "forbidden", "只有登录用户能修改自己的密码。")
        user = await run_in_threadpool(ctx.store.find_user, user_id=principal["user_id"])
        if user is None:
            raise GatewayError(404, "user_not_found", "用户不存在。")
        if not await run_in_threadpool(verify_password, data.current_password,
                                       user["password_hash"]):
            raise GatewayError(403, "invalid_password", "当前密码不正确。")
        try:
            validate_password(data.new_password, user["username"])
        except ValueError as error:
            raise GatewayError(422, "invalid_password", str(error)) from error
        await run_in_threadpool(ctx.store.update_user, user["id"],
                                password_hash=hash_password(data.new_password))
        current = None
        token = request.cookies.get(COOKIE_NAME)
        if token:
            row = await run_in_threadpool(ctx.store.find_auth_session, hash_token(token))
            current = row["id"] if row else None
        await run_in_threadpool(ctx.store.revoke_user_sessions, user["id"], except_id=current)
        await deps.record_audit(ctx, request, "auth.password_change",
                                target_type="user", target_id=user["id"])
        return {"changed": True}

    return router
