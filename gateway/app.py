"""应用装配：上下文、生命周期、中间件、异常处理与路由注册。

业务路由在 `gateway/api/` 下按资源拆分；这里只负责把它们和进程级资源接起来。
"""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .access_log import AccessLogMiddleware
from .api import build_routers
from .api.models import CHAT_PAGE
from .audit import AuditTrail
from .auth import LoginLimiter
from .config import Settings
from .container import AppContext
from .errors import GatewayError
from .limits import BodyLimitMiddleware, SecurityHeadersMiddleware, UserLimiter
from .logging_setup import LogSettings
from .maintenance import audit_janitor, prune_audit
from .provider import ModelProvider
from .proxy import Proxy, load_routes
from .proxy_routes import build_proxy_router, register_forwarders
from .service import ChatService
from .storage import Store
from .tickets import TicketStore

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None,
               log_settings: LogSettings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.db_path)
    ctx = AppContext(
        settings=settings,
        store=store,
        audit=AuditTrail(store),
        tickets=TicketStore(),
        routes=load_routes(settings.routes_path),
        login_limiter=LoginLimiter(max_failures=settings.login_max_failures,
                                   window_seconds=settings.login_window_seconds,
                                   lock_seconds=settings.login_lock_seconds),
        chat_limiter=UserLimiter(concurrency=settings.chat_concurrency,
                                 per_minute=settings.chat_per_minute),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await run_in_threadpool(ctx.store.initialize, settings.model)
        try:
            removed = await run_in_threadpool(prune_audit, ctx.store,
                                              settings.audit_retention_days)
            if removed:
                await run_in_threadpool(
                    ctx.audit.append, action="audit.prune", actor_type="system",
                    actor_name="startup",
                    details={"deleted": removed,
                             "retention_days": settings.audit_retention_days})
        except Exception:      # 清理失败不应阻止服务启动
            logger.exception("Startup audit prune failed")
        try:
            await run_in_threadpool(ctx.audit.append, action="gateway.start",
                                    actor_type="system", actor_name="gateway",
                                    details={"version": "0.1.0",
                                             "routes": len(ctx.routes)})
        except Exception:      # 审计不可用不应阻止服务启动
            logger.exception("Startup audit failed")
        if settings.auth_mode == "local":
            users = await run_in_threadpool(ctx.store.count_users)
            if users == 0:
                logger.warning("本地认证已启用但还没有用户："
                               "python -m gateway.admin create-user --username <名字> --admin")

        provider = ModelProvider(settings)
        ctx.chat = ChatService(ctx.store, provider, settings)
        ctx.proxy = Proxy(ctx.routes, settings.proxy_timeout) if ctx.routes else None
        # 兼容旧测试/外部代码对 app.state.chat / app.state.proxy 的读取。
        app.state.chat, app.state.proxy = ctx.chat, ctx.proxy
        if ctx.proxy is not None:
            ctx.proxy.start()
        janitor = asyncio.create_task(audit_janitor(
            ctx.store, ctx.audit, settings.audit_retention_days))
        try:
            yield
        finally:
            janitor.cancel()
            with suppress(asyncio.CancelledError):
                await janitor
            await provider.close()
            if ctx.proxy is not None:
                await ctx.proxy.close()

    app = FastAPI(title="ydd_python_gateway_origion", version="0.1.0",
                  description="易于理解的 Python Agent Gateway 基础版。", lifespan=lifespan)
    app.state.ctx = ctx

    # 中间件从内到外：BodyLimit → AccessLog（可选）→ SecurityHeaders。
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes,
                       exempt_prefixes=tuple(route.prefix for route in ctx.routes))
    if log_settings is not None and log_settings.enabled:
        app.state.log_settings = log_settings
        app.add_middleware(AccessLogMiddleware, settings=log_settings)
    app.add_middleware(SecurityHeadersMiddleware)

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
        # 页面本身不含任何密钥，只是让浏览器能打开聊天界面；数据仍需登录/凭证。
        return FileResponse(CHAT_PAGE, media_type="text/html",
                            headers={"Cache-Control": "no-store"})

    @app.get("/health")
    async def health():
        # auth_required 供页面提示“是否需要登录”，不是一个安全边界。
        return {"status": "ok", "provider": settings.provider,
                "auth_required": settings.auth_mode == "local" or bool(settings.gateway_token)}

    for router in build_routers(ctx):
        app.include_router(router)
    app.include_router(build_proxy_router(ctx))
    # 反向代理最后注册：内置接口优先，不会被前缀遮蔽。
    register_forwarders(app, ctx)
    return app
