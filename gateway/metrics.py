"""轻量指标：HTTP 计数/耗时、WS 连接数、登录失败与审计失败；输出 Prometheus 文本。

不引入 prometheus_client：指标很少，手写文本格式即可，也少一个依赖。
"""

import time
from collections import Counter
from threading import Lock

BUILD_VERSION = "0.1.0"


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Metrics:
    def __init__(self) -> None:
        self.started = time.monotonic()
        self._lock = Lock()
        self._requests: Counter[tuple[str, int]] = Counter()
        self._duration_sum: Counter[str] = Counter()
        self._duration_count: Counter[str] = Counter()
        self.ws_connections = 0
        self.login_failures = 0
        self.audit_failures = 0
        self.api_key_ip_rejections = 0

    def observe_http(self, method: str, status: int, seconds: float) -> None:
        with self._lock:
            self._requests[(method, status)] += 1
            self._duration_sum[method] += seconds
            self._duration_count[method] += 1

    def ws_opened(self) -> None:
        with self._lock:
            self.ws_connections += 1

    def ws_closed(self) -> None:
        with self._lock:
            self.ws_connections -= 1

    def login_failure(self) -> None:
        with self._lock:
            self.login_failures += 1

    def audit_failure(self) -> None:
        with self._lock:
            self.audit_failures += 1

    def api_key_ip_rejected(self) -> None:
        with self._lock:
            self.api_key_ip_rejections += 1

    def render(self) -> str:
        lines = [
            "# HELP gateway_http_requests_total HTTP 请求数。",
            "# TYPE gateway_http_requests_total counter",
        ]
        with self._lock:
            requests = sorted(self._requests.items())
            durations = sorted(self._duration_sum.items())
            counts = dict(self._duration_count)
            ws = self.ws_connections
            login_failures = self.login_failures
            audit_failures = self.audit_failures
            ip_rejections = self.api_key_ip_rejections
        for (method, status), value in requests:
            lines.append(
                f'gateway_http_requests_total{{method="{_escape(method)}",'
                f'status="{status}"}} {value}')

        lines += ["# HELP gateway_http_request_duration_seconds 请求耗时（秒）。",
                  "# TYPE gateway_http_request_duration_seconds summary"]
        for method, total in durations:
            lines.append(f'gateway_http_request_duration_seconds_sum'
                         f'{{method="{_escape(method)}"}} {total:.6f}')
            lines.append(f'gateway_http_request_duration_seconds_count'
                         f'{{method="{_escape(method)}"}} {counts.get(method, 0)}')

        lines += [
            "# HELP gateway_ws_connections 当前聊天 WebSocket 连接数。",
            "# TYPE gateway_ws_connections gauge",
            f'gateway_ws_connections{{kind="chat"}} {ws}',
            "# HELP gateway_login_failures_total 登录失败次数。",
            "# TYPE gateway_login_failures_total counter",
            f"gateway_login_failures_total {login_failures}",
            "# HELP gateway_audit_failures_total 审计写入失败次数。",
            "# TYPE gateway_audit_failures_total counter",
            f"gateway_audit_failures_total {audit_failures}",
            "# HELP gateway_api_key_ip_rejections_total API Key 因 IP 白名单被拒绝的次数。",
            "# TYPE gateway_api_key_ip_rejections_total counter",
            f"gateway_api_key_ip_rejections_total {ip_rejections}",
            "# HELP gateway_uptime_seconds 进程运行秒数。",
            "# TYPE gateway_uptime_seconds gauge",
            f"gateway_uptime_seconds {time.monotonic() - self.started:.1f}",
            "# HELP gateway_build_info 版本信息。",
            "# TYPE gateway_build_info gauge",
            f'gateway_build_info{{version="{BUILD_VERSION}"}} 1',
        ]
        return "\n".join(lines) + "\n"


class MetricsMiddleware:
    """统计所有 HTTP 响应（含 4xx/5xx 与中间件直接生成的 413）。"""

    def __init__(self, app, metrics: Metrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = 500

        async def send_wrapper(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            self.metrics.observe_http(scope.get("method", "?"), status,
                                      time.perf_counter() - started)
