"""WebSocket 示例：逐块显示模型输出。先运行 python run.py。"""

import argparse
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
from dotenv import load_dotenv
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException


async def chat(base_url: str, token: str, content: str) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=120) as client:
        response = await client.post("/sessions", json={"title": "WebSocket 示例"})
        response.raise_for_status()
        session_id = response.json()["id"]

    request_id = uuid4().hex
    print(f"会话编号：{session_id}")
    print(f"请求编号：{request_id}")
    print(f"问题：{content}")

    address = urlsplit(base_url)
    scheme = "wss" if address.scheme == "https" else "ws"
    path = address.path.rstrip("/") + f"/sessions/{session_id}/stream"
    websocket_url = urlunsplit((scheme, address.netloc, path, "", ""))

    async with connect(websocket_url, additional_headers=headers) as websocket:
        await websocket.send(json.dumps({"content": content, "request_id": request_id}, ensure_ascii=False))
        printed_delta = False
        async with asyncio.timeout(120):
            while True:
                event = json.loads(await websocket.recv())
                if event["type"] == "start":
                    print(f"运行编号：{event['run_id']}")
                    print("回答：", end="", flush=True)
                elif event["type"] == "delta":
                    print(event["content"], end="", flush=True)
                    printed_delta = True
                elif event["type"] == "done":
                    # 重放成功结果时只有 done，没有 start 和 delta。
                    if not printed_delta:
                        print(event["reply"], end="")
                    print("\n回答已完整保存。")
                    return
                elif event["type"] == "error":
                    print(f"\n生成失败：{event['error']['message']}")
                    raise SystemExit(1)


def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    parser = argparse.ArgumentParser(description="调用 Gateway 的 WebSocket 流式问答接口")
    parser.add_argument("content", nargs="?", default="用简单的话解释什么是 Agent Gateway。")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default=os.getenv("GATEWAY_TOKEN", ""))
    args = parser.parse_args()
    try:
        asyncio.run(chat(args.base_url.rstrip("/"), args.token.strip(), args.content))
    except httpx.HTTPStatusError as error:
        print(f"请求失败（HTTP {error.response.status_code}）：{error.response.text}")
        raise SystemExit(1) from None
    except (httpx.RequestError, WebSocketException, OSError, TimeoutError):
        print("\n连接失败、中断或超时，请检查 Gateway 地址及 token。")
        print("已收到的部分文本不代表成功；请根据会话编号和请求编号查询最终状态。")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
