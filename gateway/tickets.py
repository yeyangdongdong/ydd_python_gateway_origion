"""短期票据：让浏览器也能建立 WebSocket 连接。

浏览器的原生 WebSocket API 不能设置请求头，所以没法携带
`Authorization: Bearer <token>`。这里的做法是：

1. 页面先用携带 token 的普通 HTTP 请求换取一张票据；
2. 再在 WebSocket 地址后面带上 ?ticket=...，由服务端校验并立即作废。

票据只在一小段时间内有效、只能使用一次，并且绑定到一个会话，
这样它即使被记录在浏览器历史或日志里，能用上的时间窗口也很小。
"""

import secrets
import time
from dataclasses import dataclass

# 票据只需要撑过“取票到连上 WebSocket”这一步，30 秒足够，泄露窗口也小。
TICKET_SECONDS = 30.0


@dataclass(frozen=True)
class Ticket:
    session_id: str
    expires_at: float


class TicketStore:
    """进程内的一次性票据表。

    FastAPI 的异步接口都在同一个事件循环里执行，这里的读改写在两次
    await 之间不会被打断，因此不需要额外的锁。服务重启后票据全部失效，
    页面重新取一张即可；本项目也限定单进程运行，不需要跨进程共享。
    """

    def __init__(self, seconds: float = TICKET_SECONDS) -> None:
        self.seconds = seconds
        self.tickets: dict[str, Ticket] = {}

    def issue(self, session_id: str) -> tuple[str, int]:
        """签发一张票据，返回 (票据内容, 有效秒数)。"""
        self._purge()
        value = secrets.token_urlsafe(32)
        self.tickets[value] = Ticket(session_id, time.monotonic() + self.seconds)
        return value, int(self.seconds)

    def take(self, value: str) -> str | None:
        """取出并删除票据；有效时返回它绑定的会话编号。

        无论校验成功还是失败，票据都会被删除：多试几次不会提高成功率，
        而“用错会话的票据”也不会被留到下一次。
        """
        ticket = self.tickets.pop(value, None)
        if ticket is None or ticket.expires_at < time.monotonic():
            return None
        return ticket.session_id

    def _purge(self) -> None:
        # 票据数量很少，签发时顺手清理过期项就够了，不需要后台任务。
        now = time.monotonic()
        for value in [key for key, ticket in self.tickets.items() if ticket.expires_at < now]:
            del self.tickets[value]
