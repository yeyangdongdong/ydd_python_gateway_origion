"""审计保留期清理：启动时做一次，之后每天巡检。"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from starlette.concurrency import run_in_threadpool

from .audit import AuditTrail
from .storage import Store

logger = logging.getLogger(__name__)


def cutoff_for(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def prune_audit(store: Store, days: int, *, dry_run: bool = False) -> int:
    """删除超过保留期的审计行；返回删除（或 dry-run 时将删除）的条数。"""
    cutoff = cutoff_for(days)
    if dry_run:
        return store.count_audit_before(cutoff)
    return store.prune_audit(cutoff)


async def audit_janitor(store: Store, audit: AuditTrail, days: int,
                        interval_seconds: float = 86400) -> None:
    """后台任务：先睡后做，避免和启动流程抢数据库。"""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            removed = await run_in_threadpool(prune_audit, store, days)
            if removed:
                await run_in_threadpool(
                    audit.append, action="audit.prune", actor_type="system",
                    actor_name="janitor",
                    details={"deleted": removed, "retention_days": days})
                logger.info("Pruned %d audit rows older than %d days", removed, days)
        except Exception:      # 巡检失败不应该让进程退出
            logger.exception("Audit janitor failed")
