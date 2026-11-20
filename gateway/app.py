"""网络入口：校验输入、调用业务逻辑、把结果返回给客户端。"""

import asyncio
import json
import logging
import secrets
import time
from contextlib import aclosing, asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import anyio
from fastapi import Depends, FastAPI, Header, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect

from .access_log import AccessLogMiddleware, log_websocket
from .audit import AuditTrail
from .auth import (COOKIE_NAME, DUMMY_HASH, LoginLimiter, api_key_principal,
                   hash_api_key, hash_password, hash_token, legacy_principal,
                   new_api_key, new_session_token, parse_api_key, session_principal,
                   validate_password, verify_password)
from .config import Settings
from .errors import GatewayError
from .logging_setup import TRACE_ID, LogSettings
from .limits import BodyLimitMiddleware, SecurityHeadersMiddleware, UserLimiter, limiter_key
from .net import forwarded_ip
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
LOGIN_PAGE = Path(__file__).resolve().parent.parent / "web" / "login.html"


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


API_SCOPES = {"sessions", "agents", "audit", "proxy"}


class ApiKeyCreate(InputModel):
    name: Text = Field(max_length=100)
    scopes: list[str] = Field(default_factory=list, max_length=10)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class UserCreate(InputModel):
    username: Text = Field(max_length=50)
    password: Text = Field(max_length=200)
    role: str = Field(default="member", pattern="^(admin|member|viewer)$")


class UserUpdate(InputModel):
    role: str | None = Field(default=None, pattern="^(admin|member|viewer)$")
    status: str | None = Field(default=None, pattern="^(active|disabled)$")


class PasswordReset(InputModel):
    password: Text = Field(max_length=200)


class PasswordChange(InputModel):
    current_password: Text = Field(max_length=200)
    new_password: Text = Field(max_length=200)


def create_app(settings: Settings | None = None,
               log_settings: LogSettings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.db_path)
    tickets = TicketStore()
    audit = AuditTrail(store)
    login_limiter = LoginLimiter(max_failures=settings.login_max_failures,
                                 window_seconds=settings.login_window_seconds,
                                 lock_seconds=settings.login_lock_seconds)
    chat_limiter = UserLimiter(concurrency=settings.chat_concurrency,
                               per_minute=settings.chat_per_minute)
    # 启动时就把路由表读进内存：配置写错立刻报错，而不是等第一个请求。
    routes = load_routes(settings.routes_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await run_in_threadpool(store.initialize, settings.model)
        try:
            await run_in_threadpool(audit.append, action="gateway.start", actor_type="system",
                                    actor_name="gateway",
                                    details={"version": "0.1.0", "routes": len(routes)})
        except Exception:      # 审计不可用不应阻止服务启动
            logging.getLogger(__name__).exception("Startup audit failed")
        if settings.auth_mode == "local":
            users = await run_in_threadpool(store.count_users)
            if users == 0:
                logging.getLogger(__name__).warning(
                    "本地认证已启用但还没有用户：python -m gateway.admin create-user --username <名字> --admin")
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

    # 中间件从内到外：BodyLimit → AccessLog（可选）→ SecurityHeaders。
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes,
                       exempt_prefixes=tuple(route.prefix for route in routes))
    if log_settings is not None and log_settings.enabled:
        app.state.log_settings = log_settings
        app.add_middleware(AccessLogMiddleware, settings=log_settings)
    app.add_middleware(SecurityHeadersMiddleware)

    def token_matches(authorization: str | None) -> bool:
        if not settings.gateway_token:
            return False
        expected = f"Bearer {settings.gateway_token}".encode("utf-8")
        return secrets.compare_digest((authorization or "").encode("utf-8"), expected)

    def legacy_open() -> bool:
        """legacy 模式且未设置 token：保持旧行为（本机演示免鉴权）。"""
        return settings.auth_mode == "legacy" and not settings.gateway_token

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

    SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

    async def session_from_cookie(token: str | None) -> dict | None:
        """校验会话 Cookie：签名即哈希，库里只存 hash；同时检查空闲与绝对过期。"""
        if not token:
            return None
        row = await run_in_threadpool(store.find_auth_session, hash_token(token))
        if row is None or row["revoked_at"] or row["user_status"] != "active":
            return None
        now = datetime.now(timezone.utc)
        if row["expires_at"] <= now.isoformat():
            await run_in_threadpool(store.revoke_auth_session, row["id"])
            return None
        last_seen = datetime.fromisoformat(row["last_seen_at"])
        if (now - last_seen).total_seconds() > settings.session_idle_seconds:
            await run_in_threadpool(store.revoke_auth_session, row["id"])
            return None
        # 每 60 秒才写一次 last_seen，避免每个请求都产生写事务。
        if (now - last_seen).total_seconds() > 60:
            await run_in_threadpool(store.touch_auth_session, row["id"], now.isoformat())
        return session_principal(row)

    async def api_key_from_header(authorization: str | None) -> dict | None:
        if not authorization or not authorization.startswith("Bearer "):
            return None
        parsed = parse_api_key(authorization[7:].strip())
        if parsed is None:
            return None
        key_id, secret = parsed
        row = await run_in_threadpool(store.find_api_key, key_id)
        if row is None or row["revoked_at"]:
            return None
        if not secrets.compare_digest(hash_api_key(secret), row["key_hash"]):
            return None
        now = datetime.now(timezone.utc)
        if row["expires_at"] and row["expires_at"] <= now.isoformat():
            return None
        last_used = row["last_used_at"]
        if not last_used or (now - datetime.fromisoformat(last_used)).total_seconds() > 60:
            try:
                await run_in_threadpool(store.touch_api_key, key_id, now.isoformat())
            except Exception:
                pass
        return api_key_principal(row, frozenset(json.loads(row["scopes"] or "[]")))

    async def authenticate(request: Request, authorization: str | None) -> dict | None:
        if settings.auth_mode == "legacy":
            if token_matches(authorization) or legacy_open():
                return legacy_principal()
            return await api_key_from_header(authorization)
        principal = await session_from_cookie(request.cookies.get(COOKIE_NAME))
        if principal is not None:
            return principal
        # 本地模式下旧的共享 token 仍可用（映射为管理员），方便脚本平滑迁移。
        if token_matches(authorization):
            return legacy_principal()
        return await api_key_from_header(authorization)

    def enforce_csrf(request: Request, principal: dict) -> None:
        """只有 Cookie 认证需要 CSRF；Bearer 头不会被浏览器自动带上。"""
        if principal["kind"] != "session" or request.method in SAFE_METHODS:
            return
        origin = request.headers.get("origin")
        if origin:
            if urlsplit(origin).netloc != request.headers.get("host", ""):
                raise GatewayError(403, "csrf_rejected", "跨站请求被拒绝。")
            return
        site = request.headers.get("sec-fetch-site")
        if site and site not in {"same-origin", "none"}:
            raise GatewayError(403, "csrf_rejected", "跨站请求被拒绝。")

    async def require_auth(request: Request,
                           authorization: str | None = Header(default=None)) -> None:
        principal = await authenticate(request, authorization)
        if principal is None:
            raise GatewayError(401, "unauthorized",
                               "请先登录，或提供正确的 Authorization: Bearer <token>。")
        enforce_csrf(request, principal)
        request.state.principal = principal
        if principal["kind"] == "api_key":
            enforce_scope(request, principal)
        if principal["role"] == "viewer" and request.method not in SAFE_METHODS \
                and request.url.path != "/logout":
            raise GatewayError(403, "forbidden", "只读账号不能执行写操作。")

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

    def require_admin(request: Request) -> None:
        principal = getattr(request.state, "principal", None)
        if principal is None or principal["role"] != "admin":
            raise GatewayError(403, "forbidden", "需要管理员权限。")

    def user_public(user: dict) -> dict:
        return {key: user.get(key) for key in
                ("id", "username", "role", "status", "created_at", "last_seen_at")}

    async def ensure_not_last_admin(user: dict, *, role: str | None = None,
                                     status: str | None = None) -> None:
        if user["role"] != "admin" or user["status"] != "active":
            return
        final_role = role if role is not None else user["role"]
        final_status = status if status is not None else user["status"]
        if final_role == "admin" and final_status == "active":
            return
        if await run_in_threadpool(store.count_active_admins) <= 1:
            raise GatewayError(409, "last_admin", "不能停用或降级最后一个管理员。")

    def owns(principal: dict, owner_id: str | None) -> bool:
        """admin 与管理级 API Key 可访问全部；其他人只能访问自己创建的资源。"""
        if principal["role"] == "admin" or principal["kind"] == "api_key":
            return True
        return bool(owner_id) and owner_id == principal["user_id"]

    def ensure_session_access(principal: dict, session: dict) -> None:
        # 用 404 而不是 403，避免暴露"别人的会话是否存在"。
        if not owns(principal, session.get("owner_id")):
            raise GatewayError(404, "session_not_found", "会话不存在。")

    async def session_for(request: Request, session_id: str) -> dict:
        session = await run_in_threadpool(store.get_session, session_id)
        ensure_session_access(request.state.principal, session)
        return session

    auth = [Depends(require_auth)]

    async def websocket_principal(websocket: WebSocket, ticket_resource: str) -> dict | None:
        """WS 准入：Cookie 会话（浏览器）→ Bearer（脚本/旧 token）→ 一次性票据。"""
        if settings.auth_mode == "local":
            principal = await session_from_cookie(websocket.cookies.get(COOKIE_NAME))
            if principal is not None:
                return principal
        if legacy_open() or token_matches(websocket.headers.get("authorization")):
            return legacy_principal()
        api = await api_key_from_header(websocket.headers.get("authorization"))
        if api is not None:
            return api
        ticket = websocket.query_params.get("ticket")
        if ticket and tickets.take(ticket) == ticket_resource:
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

    async def record_audit(request: Request, action: str, *, target_type: str = "",
                           target_id: str = "", details: dict | None = None,
                           result: str = "ok", reason: str = "") -> None:
        try:
            await run_in_threadpool(
                audit.append, action=action, **audit_context(request),
                target_type=target_type, target_id=target_id,
                details=details, result=result, reason=reason)
        except Exception:      # 审计是旁路：失败只记 ERROR，不改变业务结果
            logging.getLogger(__name__).exception("Audit append failed: %s", action)

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
        # auth_required 供页面提示“是否需要登录”，不是一个安全边界。
        return {"status": "ok", "provider": settings.provider,
                "auth_required": settings.auth_mode == "local" or bool(settings.gateway_token)}

    @app.get("/login")
    async def login_page():
        if settings.auth_mode != "local":
            raise GatewayError(404, "not_found", "当前为 legacy 认证模式，不使用登录页。")
        return FileResponse(LOGIN_PAGE, media_type="text/html",
                            headers={"Cache-Control": "no-store"})

    def check_login_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
            raise GatewayError(403, "csrf_rejected", "跨站登录请求被拒绝。")

    @app.post("/login")
    async def login(request: Request):
        if settings.auth_mode != "local":
            raise GatewayError(404, "not_found", "当前为 legacy 认证模式，不使用登录。")
        check_login_origin(request)
        body = await request.body()
        form = parse_qs(body.decode("utf-8", "replace"))
        username = (form.get("username") or [""])[0].strip()
        password = (form.get("password") or [""])[0]
        next_path = (form.get("next") or ["/"])[0]
        if not next_path.startswith("/") or next_path.startswith("//"):
            next_path = "/"
        ip = request_ip(request)

        retry_after = login_limiter.retry_after(ip, username)
        if retry_after:
            await record_audit(request, "auth.login", result="failed", reason="locked",
                               details={"username": username, "retry_after": retry_after})
            return RedirectResponse("/login?error=locked", status_code=303)

        user = await run_in_threadpool(store.find_user, username=username)
        password_ok = await run_in_threadpool(verify_password, password,
                                              user["password_hash"] if user else DUMMY_HASH)
        if user is None or user["status"] != "active" or not password_ok:
            login_limiter.record_failure(ip, username)
            await record_audit(request, "auth.login", result="failed", reason="invalid",
                               details={"username": username})
            return RedirectResponse("/login?error=invalid", status_code=303)

        login_limiter.record_success(ip, username)
        token = new_session_token()
        now = datetime.now(timezone.utc)
        session = {
            "id": uuid4().hex, "user_id": user["id"], "token_hash": hash_token(token),
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=settings.session_absolute_seconds)).isoformat(),
            "last_seen_at": now.isoformat(), "ip": ip,
            "user_agent": (request.headers.get("user-agent") or "")[:200],
        }
        await run_in_threadpool(store.create_auth_session, session)
        try:
            await run_in_threadpool(audit.append, action="auth.login", actor_type="user",
                                    actor_id=user["id"], actor_name=user["username"],
                                    ip=ip, trace_id=TRACE_ID.get(),
                                    details={"username": username})
        except Exception:
            logging.getLogger(__name__).exception("Login audit failed")

        response = RedirectResponse(next_path, status_code=303)
        response.set_cookie(COOKIE_NAME, token,
                            max_age=settings.session_absolute_seconds,
                            httponly=True, samesite="lax",
                            secure=settings.cookie_secure, path="/")
        return response

    @app.post("/logout", dependencies=auth)
    async def logout(request: Request):
        token = request.cookies.get(COOKIE_NAME)
        if token:
            row = await run_in_threadpool(store.find_auth_session, hash_token(token))
            if row is not None:
                await run_in_threadpool(store.revoke_auth_session, row["id"])
        await record_audit(request, "auth.logout")
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.get("/auth/status")
    async def auth_status(request: Request):
        principal = None
        if settings.auth_mode == "local":
            principal = await session_from_cookie(request.cookies.get(COOKIE_NAME))
        return {"auth_mode": settings.auth_mode,
                "authenticated": principal is not None,
                "user": None if principal is None else {
                    "id": principal["user_id"], "username": principal["username"],
                    "role": principal["role"]}}

    @app.get("/proxy-status", dependencies=auth)
    async def proxy_status(request: Request):
        """反向代理的只读状态：每个上游是否健康、是否在冷却、最近一次错误。"""
        require_admin(request)
        proxy = app.state.proxy
        if proxy is None:
            return {"enabled": False, "routes": []}
        return {"enabled": True, **proxy.status()}

    @app.get("/agents", dependencies=auth)
    async def list_agents(request: Request,
                          limit: int = Query(default=AGENT_PAGE, ge=1, le=1000),
                          offset: int = Query(default=0, ge=0)):
        principal = request.state.principal
        owner = None if principal["role"] == "admin" or principal["kind"] == "api_key" \
            else principal["user_id"]
        return await run_in_threadpool(store.list_agents, limit, offset, owner)

    @app.post("/agents", status_code=201, dependencies=auth)
    async def create_agent(data: AgentCreate, request: Request):
        principal = request.state.principal
        agent = await run_in_threadpool(store.create_agent, data.name, data.system_prompt,
                                        data.model or settings.model,
                                        principal["user_id"] or None)
        await record_audit(request, "agent.create", target_type="agent",
                           target_id=agent["id"],
                           details={"name": agent["name"], "model": agent["model"]})
        return agent

    @app.patch("/agents/{agent_id}", dependencies=auth)
    async def update_agent(agent_id: str, data: AgentUpdate, request: Request):
        if data.name is None and data.system_prompt is None and data.model is None:
            raise GatewayError(422, "invalid_request", "请至少提供一个要修改的字段。")
        principal = request.state.principal
        existing = await run_in_threadpool(store.get_agent, agent_id)
        if not owns(principal, existing.get("owner_id")):
            raise GatewayError(404, "agent_not_found", "Agent 不存在。")
        agent = await run_in_threadpool(store.update_agent, agent_id, name=data.name,
                                        system_prompt=data.system_prompt, model=data.model)
        changed = [field for field in ("name", "system_prompt", "model")
                   if getattr(data, field) is not None]
        await record_audit(request, "agent.update", target_type="agent", target_id=agent_id,
                           details={"fields": changed})
        return agent

    @app.delete("/agents/{agent_id}", status_code=204, dependencies=auth)
    async def delete_agent(agent_id: str, request: Request):
        principal = request.state.principal
        existing = await run_in_threadpool(store.get_agent, agent_id)
        if not owns(principal, existing.get("owner_id")):
            raise GatewayError(404, "agent_not_found", "Agent 不存在。")
        await run_in_threadpool(store.delete_agent, agent_id)
        await record_audit(request, "agent.delete", target_type="agent", target_id=agent_id)

    @app.get("/sessions", dependencies=auth)
    async def list_sessions(request: Request,
                            limit: int = Query(default=SESSION_PAGE, ge=1, le=1000),
                            offset: int = Query(default=0, ge=0)):
        principal = request.state.principal
        owner = None if principal["role"] == "admin" or principal["kind"] == "api_key" \
            else principal["user_id"]
        return await run_in_threadpool(store.list_sessions, limit, offset, owner)

    @app.post("/sessions", status_code=201, dependencies=auth)
    async def create_session(data: SessionCreate, request: Request):
        principal = request.state.principal
        agent = await run_in_threadpool(store.get_agent, data.agent_id)
        if agent.get("owner_id") and not owns(principal, agent["owner_id"]):
            raise GatewayError(404, "agent_not_found", "Agent 不存在，请先创建 Agent。")
        return await run_in_threadpool(store.create_session, data.agent_id, data.title,
                                       principal["user_id"] or None)

    @app.get("/sessions/{session_id}", dependencies=auth)
    async def get_session(session_id: str, request: Request):
        return await session_for(request, session_id)

    @app.patch("/sessions/{session_id}", dependencies=auth)
    async def update_session(session_id: str, data: SessionUpdate, request: Request):
        await session_for(request, session_id)
        session = await run_in_threadpool(store.update_session, session_id, title=data.title)
        await record_audit(request, "session.update", target_type="session",
                           target_id=session_id, details={"fields": ["title"]})
        return session

    @app.delete("/sessions/{session_id}", status_code=204, dependencies=auth)
    async def delete_session(session_id: str, request: Request):
        await session_for(request, session_id)
        await run_in_threadpool(store.delete_session, session_id)
        await record_audit(request, "session.delete", target_type="session",
                           target_id=session_id)

    @app.get("/sessions/{session_id}/messages", dependencies=auth)
    async def get_messages(session_id: str, request: Request,
                           limit: int = Query(default=MESSAGE_PAGE, ge=1, le=5000),
                           offset: int = Query(default=0, ge=0)):
        await session_for(request, session_id)
        return await run_in_threadpool(store.messages, session_id, limit=limit, offset=offset)

    @app.delete("/sessions/{session_id}/messages", dependencies=auth)
    async def clear_messages(session_id: str, request: Request):
        await session_for(request, session_id)
        deleted = await run_in_threadpool(store.clear_messages, session_id)
        await record_audit(request, "session.clear", target_type="session",
                           target_id=session_id, details={"deleted": deleted})
        return {"deleted": deleted}

    @app.get("/sessions/{session_id}/requests/{request_id}", dependencies=auth)
    async def get_request(session_id: str, request_id: str, request: Request):
        await session_for(request, session_id)
        run = await run_in_threadpool(store.find_request, session_id, request_id)
        if run is None:
            raise GatewayError(404, "request_not_found", "请求尚未开始或不存在。")
        return run

    @app.get("/runs/{run_id}", dependencies=auth)
    async def get_run(run_id: str, request: Request):
        run = await run_in_threadpool(store.get_run, run_id)
        session = await run_in_threadpool(store.get_session, run["session_id"])
        ensure_session_access(request.state.principal, session)
        return run

    @app.get("/audit", dependencies=auth)
    async def list_audit(request: Request,
                         limit: int = Query(default=100, ge=1, le=1000),
                         offset: int = Query(default=0, ge=0),
                         action: str | None = Query(default=None, max_length=100),
                         result: str | None = Query(default=None, max_length=20)):
        """审计记录（新→旧），仅管理员。"""
        require_admin(request)
        return await run_in_threadpool(audit.list, limit, offset,
                                       action=action, result=result)

    @app.get("/api-keys", dependencies=auth)
    async def list_api_keys(request: Request):
        require_admin(request)
        rows = await run_in_threadpool(store.list_api_keys)
        return [{"id": row["id"], "name": row["name"], "prefix": row["key_prefix"],
                 "scopes": json.loads(row["scopes"] or "[]"),
                 "owner_id": row["owner_id"], "expires_at": row["expires_at"],
                 "created_at": row["created_at"], "last_used_at": row["last_used_at"],
                 "revoked": bool(row["revoked_at"])} for row in rows]

    @app.post("/api-keys", status_code=201, dependencies=auth)
    async def create_api_key(data: ApiKeyCreate, request: Request):
        require_admin(request)
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
        principal = request.state.principal
        await run_in_threadpool(store.create_api_key, {
            "id": key_id, "name": data.name, "key_hash": hash_api_key(secret),
            "key_prefix": secret[:8], "scopes": json.dumps(sorted(set(data.scopes))),
            "owner_id": principal["user_id"] or "",
            "expires_at": expires_at, "created_at": now.isoformat(),
        })
        await record_audit(request, "api_key.create", target_type="api_key",
                           target_id=key_id,
                           details={"name": data.name, "scopes": sorted(set(data.scopes))})
        return {"id": key_id, "name": data.name, "key": plaintext,
                "prefix": secret[:8], "scopes": sorted(set(data.scopes)),
                "expires_at": expires_at}

    @app.delete("/api-keys/{key_id}", status_code=204, dependencies=auth)
    async def revoke_api_key(key_id: str, request: Request):
        require_admin(request)
        removed = await run_in_threadpool(store.revoke_api_key, key_id)
        if not removed:
            raise GatewayError(404, "api_key_not_found", "API Key 不存在或已撤销。")
        await record_audit(request, "api_key.revoke", target_type="api_key",
                           target_id=key_id)

    @app.get("/users", dependencies=auth)
    async def list_users(request: Request):
        require_admin(request)
        users = await run_in_threadpool(store.list_users)
        return [user_public(user) for user in users]

    @app.post("/users", status_code=201, dependencies=auth)
    async def create_user(data: UserCreate, request: Request):
        require_admin(request)
        if await run_in_threadpool(store.find_user, username=data.username):
            raise GatewayError(409, "username_taken", "用户名已存在。")
        try:
            validate_password(data.password, data.username)
        except ValueError as error:
            raise GatewayError(422, "invalid_password", str(error)) from error
        user = await run_in_threadpool(store.create_user, data.username,
                                       hash_password(data.password), data.role)
        await record_audit(request, "user.create", target_type="user", target_id=user["id"],
                           details={"username": user["username"], "role": user["role"]})
        return user_public(user)

    @app.patch("/users/{user_id}", dependencies=auth)
    async def update_user(user_id: str, data: UserUpdate, request: Request):
        require_admin(request)
        if data.role is None and data.status is None:
            raise GatewayError(422, "invalid_request", "请至少提供一个要修改的字段。")
        user = await run_in_threadpool(store.find_user, user_id=user_id)
        if user is None:
            raise GatewayError(404, "user_not_found", "用户不存在。")
        await ensure_not_last_admin(user, role=data.role, status=data.status)
        updated = await run_in_threadpool(store.update_user, user_id,
                                          role=data.role, status=data.status)
        if data.status == "disabled":
            await run_in_threadpool(store.revoke_user_sessions, user_id)
        await record_audit(request, "user.update", target_type="user", target_id=user_id,
                           details={"fields": [key for key, value in
                                               (("role", data.role), ("status", data.status))
                                               if value is not None]})
        return user_public(updated)

    @app.post("/users/{user_id}/reset-password", dependencies=auth)
    async def reset_password(user_id: str, data: PasswordReset, request: Request):
        require_admin(request)
        user = await run_in_threadpool(store.find_user, user_id=user_id)
        if user is None:
            raise GatewayError(404, "user_not_found", "用户不存在。")
        try:
            validate_password(data.password, user["username"])
        except ValueError as error:
            raise GatewayError(422, "invalid_password", str(error)) from error
        await run_in_threadpool(store.update_user, user_id,
                                password_hash=hash_password(data.password))
        await run_in_threadpool(store.revoke_user_sessions, user_id)
        await record_audit(request, "user.reset_password", target_type="user",
                           target_id=user_id, details={"username": user["username"]})
        return {"reset": True, "user_id": user_id}

    @app.delete("/users/{user_id}/sessions", status_code=204, dependencies=auth)
    async def revoke_user_sessions(user_id: str, request: Request):
        require_admin(request)
        user = await run_in_threadpool(store.find_user, user_id=user_id)
        if user is None:
            raise GatewayError(404, "user_not_found", "用户不存在。")
        await run_in_threadpool(store.revoke_user_sessions, user_id)
        await record_audit(request, "user.revoke_sessions", target_type="user",
                           target_id=user_id)

    @app.post("/auth/password", dependencies=auth)
    async def change_password(data: PasswordChange, request: Request):
        principal = request.state.principal
        if principal["kind"] != "session":
            raise GatewayError(403, "forbidden", "只有登录用户能修改自己的密码。")
        user = await run_in_threadpool(store.find_user, user_id=principal["user_id"])
        if user is None:
            raise GatewayError(404, "user_not_found", "用户不存在。")
        if not await run_in_threadpool(verify_password, data.current_password,
                                       user["password_hash"]):
            raise GatewayError(403, "invalid_password", "当前密码不正确。")
        try:
            validate_password(data.new_password, user["username"])
        except ValueError as error:
            raise GatewayError(422, "invalid_password", str(error)) from error
        await run_in_threadpool(store.update_user, user["id"],
                                password_hash=hash_password(data.new_password))
        current = None
        token = request.cookies.get(COOKIE_NAME)
        if token:
            row = await run_in_threadpool(store.find_auth_session, hash_token(token))
            current = row["id"] if row else None
        await run_in_threadpool(store.revoke_user_sessions, user["id"], except_id=current)
        await record_audit(request, "auth.password_change", target_type="user",
                           target_id=user["id"])
        return {"changed": True}

    @app.post("/sessions/{session_id}/ticket", dependencies=auth)
    async def create_ticket(session_id: str, request: Request):
        """给浏览器换一张连接 WebSocket 的一次性票据。

        这一步用普通 HTTP 完成，因此可以携带 Authorization 头；
        拿到票据后，页面就能连 `.../stream?ticket=...`。
        """
        await session_for(request, session_id)
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
    async def chat(session_id: str, data: ChatInput, request: Request):
        await session_for(request, session_id)
        async with chat_limiter.slot(limiter_key(request.state.principal)):
            async with aclosing(app.state.chat.stream(
                    session_id, data.content, data.request_id)) as events:
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
        trace_id = uuid4().hex[:16]
        started = time.perf_counter()
        close_code = None
        principal = await websocket_principal(websocket, f"session:{session_id}")
        if principal is None:
            await websocket.close(code=1008)
            return
        if principal["kind"] == "api_key" and "sessions" not in principal["scopes"]:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        log_websocket("ws.connect", session_id=session_id, trace_id=trace_id,
                      path=websocket.url.path)
        try:
            session = await run_in_threadpool(store.get_session, session_id)
            ensure_session_access(principal, session)
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
                    async with chat_limiter.slot(limiter_key(principal)):
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
            worker.cancel()
            # 等待业务协程写完中断记录，避免连接的外层取消再次打断清理。
            with anyio.CancelScope(shield=True):
                with suppress(asyncio.CancelledError, WebSocketDisconnect):
                    await worker
            log_websocket("ws.close", session_id=session_id, trace_id=trace_id,
                          duration_ms=round((time.perf_counter() - started) * 1000, 1),
                          close_code=close_code)

    # 可选的反向代理：按 routes.json 为每个前缀注册 HTTP 与 WebSocket 转发路由。
    # 放在最后注册，内置接口优先，不会被前缀遮蔽（加载时也已拒绝冲突前缀）。
    def service_name(route) -> str:
        raw = route.prefix.strip("/").split("/")[0]
        return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in raw) or "proxy"

    def make_forwarder(route):
        async def forward(request: Request, path: str) -> Response:
            principal = request.state.principal
            if principal["kind"] == "api_key":
                if "proxy" not in principal["scopes"] and \
                        route.required_scope not in principal["scopes"]:
                    raise GatewayError(403, "forbidden", "该 API Key 缺少代理权限。")
            # 访问日志中间件据此额外写一份 services/<name>/。
            request.scope["proxy_service"] = service_name(route)
            return await request.app.state.proxy.forward(request, route, path)
        return forward

    def make_ws_forwarder(route):
        async def forward(websocket: WebSocket, path: str) -> None:
            # 准入：Cookie 会话 / Bearer（旧 token 或 API Key）/ 代理 WS 的一次性票据。
            principal = await websocket_principal(websocket, f"ws:{websocket.url.path}")
            if principal is None:
                await websocket.close(code=1008)
                return
            if principal["kind"] == "api_key" and "proxy" not in principal["scopes"] \
                    and route.required_scope not in principal["scopes"]:
                await websocket.close(code=1008)
                return
            await websocket.app.state.proxy.forward_websocket(websocket, route, path, principal)
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
