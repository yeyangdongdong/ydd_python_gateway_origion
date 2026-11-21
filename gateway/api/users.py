"""用户管理：仅管理员；最后一个管理员不可停用/降级。"""

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from ..auth import hash_password, validate_password
from ..container import AppContext
from ..errors import GatewayError
from . import deps
from .models import PasswordReset, UserCreate, UserUpdate


def build_users_router(ctx: AppContext) -> APIRouter:
    router = APIRouter()
    auth = [Depends(deps.require_auth)]

    @router.get("/users", dependencies=auth)
    async def list_users(request: Request):
        deps.require_admin(request)
        users = await run_in_threadpool(ctx.store.list_users)
        return [deps.user_public(user) for user in users]

    @router.post("/users", status_code=201, dependencies=auth)
    async def create_user(data: UserCreate, request: Request):
        deps.require_admin(request)
        if await run_in_threadpool(ctx.store.find_user, username=data.username):
            raise GatewayError(409, "username_taken", "用户名已存在。")
        try:
            validate_password(data.password, data.username)
        except ValueError as error:
            raise GatewayError(422, "invalid_password", str(error)) from error
        user = await run_in_threadpool(ctx.store.create_user, data.username,
                                       hash_password(data.password), data.role)
        await deps.record_audit(ctx, request, "user.create", target_type="user",
                                target_id=user["id"],
                                details={"username": user["username"], "role": user["role"]})
        return deps.user_public(user)

    @router.patch("/users/{user_id}", dependencies=auth)
    async def update_user(user_id: str, data: UserUpdate, request: Request):
        deps.require_admin(request)
        if data.role is None and data.status is None:
            raise GatewayError(422, "invalid_request", "请至少提供一个要修改的字段。")
        user = await run_in_threadpool(ctx.store.find_user, user_id=user_id)
        if user is None:
            raise GatewayError(404, "user_not_found", "用户不存在。")
        await deps.ensure_not_last_admin(ctx, user, role=data.role, status=data.status)
        updated = await run_in_threadpool(ctx.store.update_user, user_id,
                                          role=data.role, status=data.status)
        if data.status == "disabled":
            await run_in_threadpool(ctx.store.revoke_user_sessions, user_id)
        await deps.record_audit(ctx, request, "user.update", target_type="user",
                                target_id=user_id,
                                details={"fields": [key for key, value in
                                                    (("role", data.role), ("status", data.status))
                                                    if value is not None]})
        return deps.user_public(updated)

    @router.post("/users/{user_id}/reset-password", dependencies=auth)
    async def reset_password(user_id: str, data: PasswordReset, request: Request):
        deps.require_admin(request)
        user = await run_in_threadpool(ctx.store.find_user, user_id=user_id)
        if user is None:
            raise GatewayError(404, "user_not_found", "用户不存在。")
        try:
            validate_password(data.password, user["username"])
        except ValueError as error:
            raise GatewayError(422, "invalid_password", str(error)) from error
        await run_in_threadpool(ctx.store.update_user, user_id,
                                password_hash=hash_password(data.password))
        await run_in_threadpool(ctx.store.revoke_user_sessions, user_id)
        await deps.record_audit(ctx, request, "user.reset_password", target_type="user",
                                target_id=user_id, details={"username": user["username"]})
        return {"reset": True, "user_id": user_id}

    @router.delete("/users/{user_id}/sessions", status_code=204, dependencies=auth)
    async def revoke_user_sessions(user_id: str, request: Request):
        deps.require_admin(request)
        user = await run_in_threadpool(ctx.store.find_user, user_id=user_id)
        if user is None:
            raise GatewayError(404, "user_not_found", "用户不存在。")
        await run_in_threadpool(ctx.store.revoke_user_sessions, user_id)
        await deps.record_audit(ctx, request, "user.revoke_sessions", target_type="user",
                                target_id=user_id)

    return router
