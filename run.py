"""运行：python run.py。默认仅监听本机。"""

import argparse

import uvicorn

from gateway.app import create_app
from gateway.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 Python Agent Gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
    except ValueError as error:
        parser.error(str(error))
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not settings.gateway_token:
        parser.error("对外监听前请在 .env 中设置 GATEWAY_TOKEN。")
    uvicorn.run(create_app(settings), host=args.host, port=args.port, workers=1,
                ws_max_size=65536, timeout_graceful_shutdown=10)


if __name__ == "__main__":
    main()
