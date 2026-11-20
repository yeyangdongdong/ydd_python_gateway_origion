"""审计留痕：数据库表（可查询）+ 审计日志文件（可采集），双写同一条事件。

审计写失败不会影响业务请求，但会留下 ERROR 日志——内部工具的取舍是"可用优先"。
"""

import json
import logging
from uuid import uuid4

from .logging_setup import TRACE_ID
from .storage import Store, now

logger = logging.getLogger("gateway.audit")


class AuditTrail:
    def __init__(self, store: Store) -> None:
        self.store = store

    def append(self, *, action: str, actor_type: str, actor_id: str = "",
               actor_name: str = "", target_type: str = "", target_id: str = "",
               result: str = "ok", reason: str = "", ip: str = "",
               trace_id: str = "", details: dict | None = None) -> dict:
        event = {
            "id": uuid4().hex,
            "ts": now(),
            "trace_id": trace_id or TRACE_ID.get(),
            "actor_type": actor_type,
            "actor_id": actor_id,
            "actor_name": actor_name,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "result": result,
            "reason": reason,
            "ip": ip,
            "details": json.dumps(details or {}, ensure_ascii=False, default=str),
        }
        try:
            self.store.append_audit(event)
        except Exception:      # 审计不可用不应阻断业务
            logger.exception("Audit write failed: %s", action)

        logger.info("audit", extra={"fields": {
            "event": "audit",
            "audit": {
                "id": event["id"],
                "action": action,
                "result": result,
                "actor": {"type": actor_type, "id": actor_id, "name": actor_name},
                "target": {"type": target_type, "id": target_id},
                "reason": reason,
                "ip": ip,
                "details": details or {},
            },
        }})
        return event

    def list(self, limit: int = 100, offset: int = 0, *, action: str | None = None,
             result: str | None = None, actor_id: str | None = None) -> list[dict]:
        rows = self.store.list_audit(limit, offset, action=action,
                                     result=result, actor_id=actor_id)
        events = []
        for row in rows:
            try:
                details = json.loads(row.get("details") or "{}")
            except json.JSONDecodeError:
                details = {"raw": row.get("details")}
            events.append({
                "id": row["id"],
                "ts": row["ts"],
                "trace_id": row.get("trace_id") or "",
                "action": row["action"],
                "result": row["result"],
                "reason": row.get("reason") or "",
                "ip": row.get("ip") or "",
                "actor": {"type": row["actor_type"], "id": row.get("actor_id") or "",
                          "name": row.get("actor_name") or ""},
                "target": {"type": row.get("target_type") or "",
                           "id": row.get("target_id") or ""},
                "details": details,
            })
        return events
