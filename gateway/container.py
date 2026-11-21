"""一次装配好的依赖集合：create_app 构建，路由与依赖注入读取。"""

from dataclasses import dataclass

from .audit import AuditTrail
from .auth import LoginLimiter
from .config import Settings
from .limits import UserLimiter
from .proxy import Proxy, ProxyRoute
from .service import ChatService
from .storage import Store
from .tickets import TicketStore


@dataclass
class AppContext:
    settings: Settings
    store: Store
    audit: AuditTrail
    tickets: TicketStore
    routes: list[ProxyRoute]
    login_limiter: LoginLimiter
    chat_limiter: UserLimiter
    # 这两个在 lifespan 里创建，路由通过 ctx.chat / ctx.proxy 访问。
    chat: ChatService | None = None
    proxy: Proxy | None = None
