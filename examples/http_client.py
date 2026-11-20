"""HTTP 示例：创建会话、发送消息、读取历史。先运行 python run.py。"""

import argparse
import os
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import load_dotenv


def main() -> None:
    # 从示例所在项目读取配置，换一个终端运行时也能使用同一个 Gateway token。
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    parser = argparse.ArgumentParser(description="调用 Gateway 的 HTTP 问答接口")
    parser.add_argument("content", nargs="?", default="你好，请介绍一下自己。")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default=os.getenv("GATEWAY_TOKEN", ""))
    args = parser.parse_args()
    token = args.token.strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    try:
        with httpx.Client(base_url=args.base_url.rstrip("/"), headers=headers, timeout=120) as client:
            response = client.post("/sessions", json={"agent_id": "default", "title": "HTTP 示例"})
            response.raise_for_status()
            session_id = response.json()["id"]
            request_id = uuid4().hex
            print(f"会话编号：{session_id}")
            print(f"请求编号：{request_id}")
            print(f"问题：{args.content}")

            response = client.post(
                f"/sessions/{session_id}/chat",
                json={"content": args.content, "request_id": request_id},
            )
            response.raise_for_status()
            answer = response.json()
            print(f"回答：{answer['reply']}")
            print(f"运行编号：{answer['run_id']}")

            response = client.get(f"/sessions/{session_id}/messages")
            response.raise_for_status()
            print("\n已保存的消息：")
            for message in response.json():
                print(f"{message['role']}：{message['content']}")
    except httpx.HTTPStatusError as error:
        print(f"请求失败（HTTP {error.response.status_code}）：{error.response.text}")
        raise SystemExit(1) from None
    except httpx.RequestError:
        print("无法完成请求，请检查 Gateway 是否启动以及地址是否正确。")
        print("如果消息已经发出，请先按会话编号和请求编号查询状态，再决定是否重试。")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
