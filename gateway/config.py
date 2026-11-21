"""从环境变量或项目目录的 .env 文件读取配置。"""

import math
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent.parent

# 打开 / 关闭的写法都接受，避免因为 "True"、"1" 这类差异导致配置悄悄失效。
TRUE_WORDS = {"1", "true", "yes", "on"}
FALSE_WORDS = {"0", "false", "no", "off"}


def read_flag(name: str, default: bool) -> bool:
    """读取布尔型环境变量；无法识别时报错，而不是默默当成 False。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in TRUE_WORDS:
        return True
    if value in FALSE_WORDS:
        return False
    raise ValueError(f"{name} 只能是 true 或 false。")


@dataclass(frozen=True)
class Settings:
    # demo 不访问模型服务，适合先运行项目和学习调用方式。
    provider: str = "demo"
    model: str = "demo"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    gateway_token: str = ""
    db_path: Path = PROJECT_DIR / "data" / "gateway.sqlite3"
    request_timeout: float = 60.0
    max_tokens: int = 2048
    # 模型的上下文窗口总长度（提示词 + 回答）。历史裁剪按它计算预算。
    context_tokens: int = 8192
    # 默认不读取系统代理：本机模型服务（127.0.0.1）常被代理拦截并返回 502。
    use_system_proxy: bool = False
    # 可选的反向代理：路由表文件与上游超时。文件不存在表示不启用。
    routes_path: Path = PROJECT_DIR / "routes.json"
    proxy_timeout: float = 30.0
    # 认证模式：legacy 只认 GATEWAY_TOKEN；local 启用本地账号 + 会话（可同时接受旧 token）。
    auth_mode: str = "legacy"
    session_idle_seconds: int = 8 * 3600
    session_absolute_seconds: int = 7 * 24 * 3600
    cookie_secure: bool = False
    # 允许在非回环地址上以明文 HTTP 使用本地认证（不推荐，仅用于临时内网调试）。
    allow_insecure_lan: bool = False
    login_max_failures: int = 5
    login_window_seconds: int = 300
    login_lock_seconds: int = 900
    # 内置接口的请求体上限；代理前缀豁免（由代理路由自行控制）。
    max_body_bytes: int = 1024 * 1024
    # 每个用户的聊天并发与每分钟请求数。
    chat_concurrency: int = 3
    chat_per_minute: int = 60
    # 新建 API Key 的默认有效天数；0 表示默认不过期。
    api_key_default_days: int = 180
    # 允许跨源访问的精确 origin 列表；空表示关闭 CORS。
    cors_origins: tuple[str, ...] = ()
    # WebSocket 协议层 ping 配置（0 表示关闭）。
    ws_ping_interval: float = 20.0
    ws_ping_timeout: float = 20.0
    # 审计表保留天数；文件保留由 GATEWAY_AUDIT_BACKUP_DAYS 控制，两者独立。
    audit_retention_days: int = 180
    # 备份目录与保留快照数。
    backup_dir: Path = Path(r"D:\Document\backup\logs")
    backup_keep: int = 14

    def __post_init__(self) -> None:
        if self.provider not in {"demo", "openai"}:
            raise ValueError("GATEWAY_PROVIDER 只能是 demo 或 openai。")
        if self.provider == "openai" and (not self.model.strip() or self.model == "demo"):
            raise ValueError("真实模型模式需要设置 MODEL_NAME，例如服务商提供的模型名称。")
        url = urlsplit(self.base_url)
        if url.scheme not in {"http", "https"} or not url.hostname:
            raise ValueError("MODEL_BASE_URL 必须是完整的 http 或 https 地址。")
        if url.username or url.password or url.query or url.fragment:
            raise ValueError("MODEL_BASE_URL 不应包含账号、密码、查询参数或片段。")
        if not math.isfinite(self.request_timeout) or self.request_timeout <= 0:
            raise ValueError("MODEL_TIMEOUT_SECONDS 必须是大于 0 的有限数字。")
        if self.max_tokens <= 0:
            raise ValueError("MODEL_MAX_TOKENS 必须是正整数。")
        if self.context_tokens <= 0:
            raise ValueError("MODEL_CONTEXT_TOKENS 必须是正整数。")
        # 上下文窗口要同时放提示词和回答；留不出空间时，任何请求都会超限。
        if self.max_tokens + 256 > self.context_tokens:
            raise ValueError("MODEL_CONTEXT_TOKENS 需要比 MODEL_MAX_TOKENS 至少大 256，"
                             "否则没有空间容纳系统提示词和历史。")
        if not math.isfinite(self.proxy_timeout) or self.proxy_timeout <= 0:
            raise ValueError("GATEWAY_PROXY_TIMEOUT 必须是大于 0 的有限数字。")
        if self.auth_mode not in {"legacy", "local"}:
            raise ValueError("GATEWAY_AUTH_MODE 只能是 legacy 或 local。")
        for name, value in (("GATEWAY_SESSION_IDLE_SECONDS", self.session_idle_seconds),
                            ("GATEWAY_SESSION_ABSOLUTE_SECONDS", self.session_absolute_seconds),
                            ("GATEWAY_LOGIN_MAX_FAILURES", self.login_max_failures),
                            ("GATEWAY_LOGIN_WINDOW_SECONDS", self.login_window_seconds),
                            ("GATEWAY_LOGIN_LOCK_SECONDS", self.login_lock_seconds)):
            if value <= 0:
                raise ValueError(f"{name} 必须是正整数。")
        for name, value in (("GATEWAY_MAX_BODY_BYTES", self.max_body_bytes),
                            ("GATEWAY_CHAT_CONCURRENCY", self.chat_concurrency),
                            ("GATEWAY_CHAT_PER_MINUTE", self.chat_per_minute),
                            ("GATEWAY_AUDIT_RETENTION_DAYS", self.audit_retention_days),
                            ("GATEWAY_API_KEY_DEFAULT_DAYS", self.api_key_default_days)):
            if value < 0 or (value == 0 and name != "GATEWAY_API_KEY_DEFAULT_DAYS"):
                raise ValueError(f"{name} 必须为正整数。")
        if self.backup_keep < 0:
            raise ValueError("GATEWAY_BACKUP_KEEP 不能为负数。")
        for name, value in (("GATEWAY_WS_PING_INTERVAL", self.ws_ping_interval),
                            ("GATEWAY_WS_PING_TIMEOUT", self.ws_ping_timeout)):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} 必须是不小于 0 的有限数字。")
        for origin in self.cors_origins:
            parsed = urlsplit(origin)
            if origin == "*" or parsed.scheme not in {"http", "https"} \
                    or not parsed.netloc or parsed.path or parsed.query \
                    or parsed.fragment or parsed.username or parsed.password:
                raise ValueError(f"GATEWAY_CORS_ORIGINS 里的 origin 不合法：{origin}")

    @classmethod
    def from_env(cls) -> "Settings":
        # 已设置的系统环境变量优先于 .env，方便部署时覆盖本地配置。
        load_dotenv(PROJECT_DIR / ".env", override=False)
        database = Path(os.getenv("GATEWAY_DB_PATH", "data/gateway.sqlite3"))
        if not database.is_absolute():
            database = PROJECT_DIR / database
        routes = Path(os.getenv("GATEWAY_ROUTES_PATH", "routes.json"))
        if not routes.is_absolute():
            routes = PROJECT_DIR / routes
        try:
            timeout = float(os.getenv("MODEL_TIMEOUT_SECONDS", "60"))
            max_tokens = int(os.getenv("MODEL_MAX_TOKENS", "2048"))
            context_tokens = int(os.getenv("MODEL_CONTEXT_TOKENS", "8192"))
            proxy_timeout = float(os.getenv("GATEWAY_PROXY_TIMEOUT", "30"))
            idle = int(os.getenv("GATEWAY_SESSION_IDLE_SECONDS", str(8 * 3600)))
            absolute = int(os.getenv("GATEWAY_SESSION_ABSOLUTE_SECONDS", str(7 * 24 * 3600)))
            max_failures = int(os.getenv("GATEWAY_LOGIN_MAX_FAILURES", "5"))
            window = int(os.getenv("GATEWAY_LOGIN_WINDOW_SECONDS", "300"))
            lock = int(os.getenv("GATEWAY_LOGIN_LOCK_SECONDS", "900"))
            max_body = int(os.getenv("GATEWAY_MAX_BODY_BYTES", str(1024 * 1024)))
            chat_concurrency = int(os.getenv("GATEWAY_CHAT_CONCURRENCY", "3"))
            chat_per_minute = int(os.getenv("GATEWAY_CHAT_PER_MINUTE", "60"))
            audit_retention = int(os.getenv("GATEWAY_AUDIT_RETENTION_DAYS", "180"))
            backup_keep = int(os.getenv("GATEWAY_BACKUP_KEEP", "14"))
            api_key_days = int(os.getenv("GATEWAY_API_KEY_DEFAULT_DAYS", "180"))
            ws_interval = float(os.getenv("GATEWAY_WS_PING_INTERVAL", "20"))
            ws_timeout = float(os.getenv("GATEWAY_WS_PING_TIMEOUT", "20"))
            cors_origins = tuple(
                origin.strip().rstrip("/") for origin in
                os.getenv("GATEWAY_CORS_ORIGINS", "").split(",") if origin.strip())
        except ValueError as exc:
            raise ValueError("模型与登录相关的数值配置必须为整数/数字。") from exc
        return cls(
            provider=os.getenv("GATEWAY_PROVIDER", "demo").strip().lower(),
            model=os.getenv("MODEL_NAME", "demo").strip(),
            base_url=os.getenv("MODEL_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/"),
            api_key=os.getenv("MODEL_API_KEY", "").strip(),
            gateway_token=os.getenv("GATEWAY_TOKEN", "").strip(),
            db_path=database,
            request_timeout=timeout,
            max_tokens=max_tokens,
            context_tokens=context_tokens,
            use_system_proxy=read_flag("MODEL_USE_SYSTEM_PROXY", False),
            routes_path=routes,
            proxy_timeout=proxy_timeout,
            auth_mode=os.getenv("GATEWAY_AUTH_MODE", "legacy").strip().lower(),
            session_idle_seconds=idle,
            session_absolute_seconds=absolute,
            cookie_secure=read_flag("GATEWAY_COOKIE_SECURE", False),
            allow_insecure_lan=read_flag("GATEWAY_ALLOW_INSECURE_LAN", False),
            login_max_failures=max_failures,
            login_window_seconds=window,
            login_lock_seconds=lock,
            max_body_bytes=max_body,
            chat_concurrency=chat_concurrency,
            chat_per_minute=chat_per_minute,
            audit_retention_days=audit_retention,
            backup_dir=Path(os.getenv("GATEWAY_BACKUP_DIR", r"D:\Document\backup\logs")),
            backup_keep=backup_keep,
            api_key_default_days=api_key_days,
            cors_origins=cors_origins,
            ws_ping_interval=ws_interval,
            ws_ping_timeout=ws_timeout,
        )
