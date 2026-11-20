"""反向代理的主动探活：判断上游是否还健康，并在恢复后放回来。

独立成模块的原因：探活是一套自洽的"定时任务 + 状态机"，不需要和转发逻辑混在一起。
它只依赖 `ProxyRoute` / `Upstream` 的属性，因此用 TYPE_CHECKING 避免与 proxy.py 循环导入。
"""

import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:  # 仅用于类型标注，运行时不导入，避免循环依赖
    from .proxy import ProxyRoute, Upstream

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HealthSettings:
    """主动探活的参数；全局默认值写在 routes.json 顶层的 health 里。"""

    enabled: bool = False
    path: str = "/health"
    # 用 GET 做默认：不少简单服务不实现 HEAD（会回 501），HEAD 反而容易被误判。
    method: str = "GET"
    interval_seconds: float = 10.0
    timeout_seconds: float = 2.0
    failures_to_down: int = 3
    successes_to_up: int = 2

    def override(self, raw: dict, where: str) -> "HealthSettings":
        """用路由级配置覆盖这份（全局）设置后重新校验。"""
        return HealthSettings.from_dict({**asdict(self), **raw}, where)

    @classmethod
    def from_dict(cls, raw: object, where: str) -> "HealthSettings":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ValueError(f"{where} 必须是对象。")
        known = {"enabled", "path", "method", "interval_seconds", "timeout_seconds",
                 "failures_to_down", "successes_to_up"}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"{where} 里有不认识的字段：{', '.join(sorted(unknown))}")

        enabled = raw.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError(f"{where} 的 enabled 只能是 true 或 false。")

        path = raw.get("path", "/health")
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError(f"{where} 的 path 必须以 / 开头。")

        method = raw.get("method", "GET")
        if not isinstance(method, str) or method.upper() not in {"HEAD", "GET"}:
            raise ValueError(f"{where} 的 method 只能是 HEAD 或 GET。")

        interval = raw.get("interval_seconds", 10.0)
        timeout = raw.get("timeout_seconds", 2.0)
        for name, value in (("interval_seconds", interval), ("timeout_seconds", timeout)):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{where} 的 {name} 必须是大于 0 的数字。")

        failures = raw.get("failures_to_down", 3)
        successes = raw.get("successes_to_up", 2)
        for name, value in (("failures_to_down", failures), ("successes_to_up", successes)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{where} 的 {name} 必须是正整数。")

        return cls(enabled=enabled, path=path, method=method.upper(),
                   interval_seconds=float(interval), timeout_seconds=float(timeout),
                   failures_to_down=failures, successes_to_up=successes)


class HealthMonitor:
    """单个后台协程，周期性地探测所有开启探活的上游。"""

    def __init__(self, routes: "list[ProxyRoute]", client: httpx.AsyncClient):
        self.routes = routes
        self.client = client
        self.task: asyncio.Task | None = None
        self.interval: float | None = None

    def start(self) -> None:
        intervals = [route.health.interval_seconds
                     for route in self.routes if route.health.enabled]
        if not intervals:
            return
        # 不同路由的周期取最小值：实现简单，代价是慢周期路由会被探得更勤。
        self.interval = min(intervals)
        self.task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        if self.task is None:
            return
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass
        finally:
            self.task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.probe_once()
            except asyncio.CancelledError:
                raise
            except Exception:      # 单次探活失败不能让整个任务退出
                logger.exception("Health check cycle failed")
            await asyncio.sleep(self.interval)

    async def probe_once(self) -> None:
        """并发探测；URL 与参数都相同的上游只探一次。"""
        probes: dict[tuple, list[tuple[HealthSettings, "Upstream"]]] = {}
        for route in self.routes:
            health = route.health
            if not health.enabled:
                continue
            for upstream in route.targets:
                key = (upstream.url, health.path, health.method, health.timeout_seconds)
                probes.setdefault(key, []).append((health, upstream))

        async def probe(key, targets) -> None:
            url, path, method, timeout = key
            ok, error = False, ""
            try:
                # 只要服务回了响应就说明它活着：4xx 是探活路径/方法不对，
                # 405/501 是方法不支持；只有 5xx 和连不上才算失败。
                response = await self.client.request(method, url + path, timeout=timeout)
                ok = response.status_code < 500 or response.status_code in {405, 501}
                error = "" if ok else f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                error = type(exc).__name__
            for health, upstream in targets:
                self._record(health, upstream, ok, error)

        await asyncio.gather(*(probe(key, targets) for key, targets in probes.items()))

    @staticmethod
    def _record(health: HealthSettings, upstream: "Upstream", ok: bool, error: str) -> None:
        if ok:
            upstream.consecutive_failures = 0
            upstream.consecutive_successes += 1
            if not upstream.healthy and upstream.consecutive_successes >= health.successes_to_up:
                upstream.healthy = True
                upstream.last_error = ""
                logger.info("Proxy upstream %s is healthy again", upstream.url)
            return

        upstream.consecutive_successes = 0
        upstream.consecutive_failures += 1
        upstream.last_error = error
        if upstream.healthy and upstream.consecutive_failures >= health.failures_to_down:
            upstream.healthy = False
            logger.warning("Proxy upstream %s marked unhealthy after %d failures (%s)",
                           upstream.url, upstream.consecutive_failures, error)

    def status(self) -> dict:
        """给 /proxy-status 用的只读快照。"""
        now = time.monotonic()
        return {"routes": [{
            "prefix": route.prefix,
            "health_enabled": route.health.enabled,
            "upstreams": [{
                "url": upstream.url,
                "healthy": upstream.healthy,
                "cooling": upstream.cooldown_until > now,
                "cooldown_seconds": round(max(0.0, upstream.cooldown_until - now), 1),
                "consecutive_failures": upstream.consecutive_failures,
                "last_error": upstream.last_error,
            } for upstream in route.targets],
        } for route in self.routes]}
