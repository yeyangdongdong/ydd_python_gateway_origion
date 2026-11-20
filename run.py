"""运行：python run.py。默认仅监听本机。"""

import argparse

import uvicorn

from gateway.app import create_app
from gateway.config import Settings
from gateway.logging_setup import LogSettings, setup_logging


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 Python Agent Gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
        log_settings = LogSettings.from_env()
        # 路由表也在这里读取：配置写错时立即退出，而不是启动到一半才失败。
        app = create_app(settings, log_settings)
    except ValueError as error:
        parser.error(str(error))
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        if settings.auth_mode == "local" and not settings.cookie_secure \
                and not settings.allow_insecure_lan:
            parser.error("非回环地址上的本地认证需要 HTTPS：请用 Caddy 前置并只监听 127.0.0.1；"
                         "临时调试可显式设置 GATEWAY_ALLOW_INSECURE_LAN=true。")
        if not settings.gateway_token and settings.auth_mode != "local":
            parser.error("对外监听前请启用本地认证（GATEWAY_AUTH_MODE=local）或设置 GATEWAY_TOKEN。")
    runtime = setup_logging(log_settings)
    try:
        # log_config=None：日志格式与去向完全由我们接管（含 access 日志的脱敏）。
        uvicorn.run(app, host=args.host, port=args.port, workers=1,
                    ws_max_size=65536, timeout_graceful_shutdown=10, log_config=None)
    finally:
        runtime.stop()


if __name__ == "__main__":
    main()
