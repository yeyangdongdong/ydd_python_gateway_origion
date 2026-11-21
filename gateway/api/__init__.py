"""按资源拆分的 HTTP 路由；每个 build_xxx_router 接收装配好的 AppContext。"""

from fastapi import APIRouter

from ..container import AppContext
from .admin import build_admin_router
from .agents import build_agents_router
from .auth import build_auth_router
from .chat import build_chat_router
from .sessions import build_sessions_router
from .users import build_users_router


def build_routers(ctx: AppContext) -> list[APIRouter]:
    return [
        build_auth_router(ctx),
        build_chat_router(ctx),
        build_agents_router(ctx),
        build_sessions_router(ctx),
        build_admin_router(ctx),
        build_users_router(ctx),
    ]
