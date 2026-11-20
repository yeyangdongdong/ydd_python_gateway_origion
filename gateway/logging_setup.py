"""结构化日志：JSON Lines，按类别/服务分文件，异步写盘。

设计要点：

- 每条记录是一行 JSON，字段固定并允许附加嵌套对象（http/params/auth/upstream/audit）；
- 按类别落盘：access / app / proxy / auth / audit，被代理的服务再按服务名分目录；
- 写盘走 QueueHandler + QueueListener，不阻塞事件循环；
- 文件按天切分并按 `GATEWAY_LOG_BACKUP_DAYS` 清理旧文件；
- 日志本身不负责脱敏——调用方只写脱敏后的数据（见 access_log.py）。
"""

import contextvars
import json
import logging
import logging.handlers
import os
import queue
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock

from .config import PROJECT_DIR

# 让日志里能拿到当前请求的 trace_id；异步与线程池都会继承 contextvars。
TRACE_ID: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")

LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

LOG_LEVEL_DOC = "DEBUG / INFO / WARNING / ERROR / CRITICAL"


@dataclass(frozen=True)
class LogSettings:
    enabled: bool = True
    directory: Path = Path(r"D:\Document\logs\ydd-gateway")
    level: str = "INFO"
    stdout: bool = False
    params: bool = True
    content_preview: int = 200
    param_max_bytes: int = 65536
    max_bytes: int = 50 * 1024 * 1024
    backup_days: int = 30
    audit_backup_days: int = 180

    def __post_init__(self) -> None:
        if self.level.upper() not in LEVELS:
            raise ValueError(f"GATEWAY_LOG_LEVEL 只能是 {LOG_LEVEL_DOC}。")
        if self.content_preview < 0:
            raise ValueError("GATEWAY_LOG_CONTENT_PREVIEW 不能为负数。")
        if self.param_max_bytes <= 0 or self.max_bytes <= 0:
            raise ValueError("GATEWAY_LOG_PARAM_MAX_BYTES 与 GATEWAY_LOG_MAX_BYTES 必须是正整数。")
        if self.backup_days < 1 or self.audit_backup_days < 1:
            raise ValueError("日志保留天数必须大于 0。")

    @classmethod
    def from_env(cls) -> "LogSettings":
        directory = Path(os.getenv("GATEWAY_LOG_DIR", r"D:\Document\logs\ydd-gateway"))
        if not directory.is_absolute():
            directory = PROJECT_DIR / directory
        try:
            settings = cls(
                enabled=os.getenv("GATEWAY_LOG_ENABLED", "true").strip().lower()
                not in {"0", "false", "no", "off", ""},
                directory=directory,
                level=os.getenv("GATEWAY_LOG_LEVEL", "INFO").strip().upper(),
                stdout=os.getenv("GATEWAY_LOG_STDOUT", "false").strip().lower()
                in {"1", "true", "yes", "on"},
                params=os.getenv("GATEWAY_LOG_PARAMS", "true").strip().lower()
                not in {"0", "false", "no", "off"},
                content_preview=int(os.getenv("GATEWAY_LOG_CONTENT_PREVIEW", "200")),
                param_max_bytes=int(os.getenv("GATEWAY_LOG_PARAM_MAX_BYTES", "65536")),
                max_bytes=int(os.getenv("GATEWAY_LOG_MAX_BYTES", str(50 * 1024 * 1024))),
                backup_days=int(os.getenv("GATEWAY_LOG_BACKUP_DAYS", "30")),
                audit_backup_days=int(os.getenv("GATEWAY_AUDIT_BACKUP_DAYS", "180")),
            )
        except ValueError as exc:
            raise ValueError(f"日志配置无效：{exc}") from exc
        return settings


class JsonFormatter(logging.Formatter):
    """把一条日志渲染成单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(
                timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # trace_id 在产出端由 TraceIdFilter 写进记录；队列监听线程没有请求上下文。
        trace_id = getattr(record, "trace_id", "") or TRACE_ID.get()
        if trace_id:
            payload["trace_id"] = trace_id
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TraceIdFilter(logging.Filter):
    """在日志产生的线程里抓取 trace_id，随记录一起进队列。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = TRACE_ID.get()
        return True


def category_of(record: logging.LogRecord) -> tuple[str, str | None]:
    """返回 (类别, 服务名)；服务名只对 gateway.service.<name> 有值。"""
    explicit = getattr(record, "category", None)
    if isinstance(explicit, str) and explicit:
        return explicit, None
    name = record.name
    if name.startswith("gateway.service."):
        return "service", name.split(".", 2)[2]
    if name.startswith("gateway.access"):
        return "access", None
    if name.startswith("gateway.audit"):
        return "audit", None
    if name.startswith("gateway.auth"):
        return "auth", None
    if name.startswith("gateway.proxy"):
        return "proxy", None
    return "app", None


class DailyFileHandler(logging.Handler):
    """按天切分文件，单文件超过上限时追加序号；按保留天数清理。"""

    NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\.(\d+))?\.log$")

    def __init__(self, directory: Path, *, max_bytes: int, backup_days: int,
                 encoding: str = "utf-8") -> None:
        super().__init__()
        self.directory = directory
        self.max_bytes = max_bytes
        self.backup_days = backup_days
        self.encoding = encoding
        self._lock = Lock()
        self._handle = None
        self._day: date | None = None
        self._index = 0
        self._size = 0
        self._cleaned_for: date | None = None

    # 日志库会在多线程下调用 emit，这里自己加锁并与文件句柄生命周期绑定。
    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record) + "\n"
            encoded = line.encode(self.encoding)
            with self._lock:
                self._ensure_open(encoded)
                self._handle.write(line)
                self._handle.flush()
                self._size += len(encoded)
        except Exception:          # 日志失败不能影响业务
            self.handleError(record)

    def _ensure_open(self, encoded: bytes) -> None:
        today = date.today()
        if self._handle is not None and self._day == today \
                and self._size + len(encoded) <= self.max_bytes:
            return
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        if self._day != today:
            self._day, self._index, self._size = today, 0, 0
        self._cleanup(today)
        self.directory.mkdir(parents=True, exist_ok=True)
        while True:
            if self._index == 0:
                path = self.directory / f"{today.isoformat()}.log"
            else:
                path = self.directory / f"{today.isoformat()}.{self._index}.log"
            if not path.exists() or self._size == 0:
                break
            # 当天已有内容但新句柄（例如重启后）→ 追加到现有文件，超限再换序号
            self._size = path.stat().st_size
            if self._size + len(encoded) <= self.max_bytes:
                break
            self._index += 1
        self._handle = path.open("a", encoding=self.encoding)
        self._size = path.stat().st_size

    def _cleanup(self, today: date) -> None:
        if self._cleaned_for == today:
            return
        self._cleaned_for = today
        if not self.directory.exists():
            return
        cutoff = today - timedelta(days=self.backup_days)
        for path in self.directory.glob("*.log"):
            match = self.NAME.match(path.name)
            if not match:
                continue
            try:
                day = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            if day < cutoff:
                path.unlink(missing_ok=True)

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
        super().close()


class LogRouter(logging.Handler):
    """把记录分发到不同类别的文件 handler；服务类日志按服务名分目录。"""

    def __init__(self, settings: LogSettings) -> None:
        super().__init__()
        self.settings = settings
        self._handlers: dict[str, DailyFileHandler] = {}
        self._lock = Lock()

    def handler_for(self, category: str, service: str | None) -> DailyFileHandler:
        key = f"service/{service}" if service else category
        with self._lock:
            handler = self._handlers.get(key)
            if handler is None:
                if service:
                    directory = self.settings.directory / "services" / service
                    days = self.settings.backup_days
                else:
                    directory = self.settings.directory / category
                    days = (self.settings.audit_backup_days if category == "audit"
                            else self.settings.backup_days)
                handler = DailyFileHandler(directory,
                                           max_bytes=self.settings.max_bytes,
                                           backup_days=days)
                handler.setFormatter(JsonFormatter())
                self._handlers[key] = handler
            return handler

    def emit(self, record: logging.LogRecord) -> None:
        category, service = category_of(record)
        self.handler_for(category, service).handle(record)

    def close(self) -> None:
        with self._lock:
            for handler in self._handlers.values():
                handler.close()
            self._handlers.clear()
        super().close()


@dataclass
class LoggingRuntime:
    listener: logging.handlers.QueueListener | None
    logger: logging.Logger
    handlers: list[logging.Handler]

    def stop(self) -> None:
        if self.listener is not None:
            self.listener.stop()
            self.listener = None
        for handler in self.handlers:
            try:
                handler.close()
            except Exception:
                pass


def setup_logging(settings: LogSettings, *, logger_name: str = "") -> LoggingRuntime:
    """安装全局日志：文件按类别落盘，可选同时输出 stdout。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    router = LogRouter(settings)
    handlers: list[logging.Handler] = [router]
    if settings.stdout:
        stream = logging.StreamHandler()
        stream.setFormatter(JsonFormatter())
        handlers.append(stream)

    events: queue.Queue = queue.Queue(-1)
    listen = logging.handlers.QueueListener(events, *handlers, respect_handler_level=True)
    listen.start()

    handler = logging.handlers.QueueHandler(events)
    handler.addFilter(TraceIdFilter())
    root.addHandler(handler)
    root.setLevel(settings.level.upper())

    # uvicorn 的访问日志既重复又无法脱敏，统一由我们的中间件记录。
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("uvicorn.access").propagate = False
    logging.getLogger("uvicorn.error").setLevel(settings.level.upper())

    return LoggingRuntime(listener=listen, logger=logging.getLogger(logger_name or "gateway.app"),
                          handlers=handlers)
