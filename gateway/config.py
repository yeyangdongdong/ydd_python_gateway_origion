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
        except ValueError as exc:
            raise ValueError("MODEL_TIMEOUT_SECONDS、GATEWAY_PROXY_TIMEOUT 应为数字，"
                             "MODEL_MAX_TOKENS 和 MODEL_CONTEXT_TOKENS 应为整数。") from exc
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
        )
