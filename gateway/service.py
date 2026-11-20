"""HTTP 和 WebSocket 共用同一条对话流程。"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from weakref import WeakValueDictionary

import anyio
from starlette.concurrency import run_in_threadpool

from .config import Settings
from .context import build_context
from .errors import GatewayError
from .provider import ModelProvider
from .storage import Store

logger = logging.getLogger(__name__)


class ChatService:
    def __init__(self, store: Store, provider: ModelProvider, settings: Settings):
        self.store = store
        self.provider = provider
        self.settings = settings
        # 等待者和执行者持有强引用；没有请求时，锁会自动释放出字典。
        self.session_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def stream(self, session_id: str, content: str, request_id: str) -> AsyncIterator[dict]:
        lock = self.session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            # 先排队再读取历史，后一轮才能看到前一轮的完整答案。
            session = await run_in_threadpool(self.store.get_session, session_id)
            previous = await run_in_threadpool(self.store.find_request, session_id, request_id)
            if previous:
                if previous["input"] != content:
                    raise GatewayError(409, "request_conflict", "同一 request_id 不能用于不同内容。")
                if previous["status"] != "completed":
                    raise GatewayError(409, "request_not_completed",
                                       "该请求未成功；请查询运行记录，确认后使用新的 request_id 重试。")
                yield {"type": "done", "session_id": session_id, "request_id": request_id,
                       "run_id": previous["id"], "reply": previous["reply"], "replayed": True}
                return

            history = await run_in_threadpool(self.store.messages, session_id, completed_only=True)
            # 先裁剪再写运行记录：超预算时抛 413，用户改短输入后可以用同一个 request_id 重试。
            prepared = build_context(
                history,
                system_prompt=session["system_prompt"],
                user_content=content,
                context_tokens=self.settings.context_tokens,
                max_tokens=self.settings.max_tokens,
            )
            if prepared.trimmed_messages:
                logger.info("Trimmed %d history messages in session %s (%d/%d tokens)",
                            prepared.trimmed_messages, session_id,
                            prepared.estimated_tokens, prepared.budget_tokens)
            run_id = await run_in_threadpool(self.store.start_run, session_id, request_id, content)
            identifiers = {"session_id": session_id, "request_id": request_id, "run_id": run_id}
            context_info = {
                "trimmed_messages": prepared.trimmed_messages,
                "estimated_tokens": prepared.estimated_tokens,
                "budget_tokens": prepared.budget_tokens,
            }
            settled = False
            try:
                yield {"type": "start", **identifiers, "context": context_info}
                parts = []
                # 总时限也能中止持续发送无效事件、却始终没有完成的上游连接。
                async with asyncio.timeout(self.settings.request_timeout):
                    async with aclosing(self.provider.stream(prepared.messages, session["model"])) as chunks:
                        async for chunk in chunks:
                            parts.append(chunk)
                            yield {"type": "delta", **identifiers, "content": chunk}
                reply = "".join(parts)
                if not reply.strip():
                    raise GatewayError(502, "empty_reply", "模型没有返回文本，请检查模型配置。")
                await run_in_threadpool(self.store.complete_run, session_id, run_id, reply)
                settled = True
                yield {"type": "done", **identifiers, "reply": reply, "replayed": False,
                       "context": context_info}
            except Exception as exc:
                if isinstance(exc, GatewayError):
                    error = exc
                elif isinstance(exc, TimeoutError):
                    error = GatewayError(504, "model_timeout", "模型响应超时，请稍后使用新 request_id 重试。")
                else:
                    # 不记录异常文本，避免上游响应或用户内容进入日志。
                    logger.error("Chat failed: %s", type(exc).__name__)
                    error = GatewayError(500, "internal_error", "本轮处理失败，请检查服务和数据库状态。")
                await run_in_threadpool(self.store.fail_run, run_id, error)
                settled = True
                yield {"type": "error", **identifiers, "status": error.status_code,
                       "error": error.as_dict()}
            finally:
                if not settled:
                    error = GatewayError(409, "interrupted", "连接断开或服务停止，本轮已中断。")
                    # 连接已经取消，也要完成这个短事务，不能留下永久 running 的记录。
                    with anyio.CancelScope(shield=True):
                        await run_in_threadpool(self.store.fail_run, run_id, error, interrupted=True)
