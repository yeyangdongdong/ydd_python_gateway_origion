"""管理示例：创建、修改、清空、删除。先运行 python run.py。

这个脚本会走一遍完整的“管理”流程，每一步都打印服务端返回，方便对照 README。
运行结束后它自己删除创建的 Agent 和会话，不会留下垃圾数据。
"""

import argparse
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv


def show(step: str, value) -> None:
    print(f"{step}：{value}")


def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    parser = argparse.ArgumentParser(description="演示 Gateway 的管理接口")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default=os.getenv("GATEWAY_TOKEN", ""))
    args = parser.parse_args()
    token = args.token.strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    try:
        with httpx.Client(base_url=args.base_url.rstrip("/"), headers=headers, timeout=120) as client:
            # 1. 列表接口支持分页，这里取第一页。
            agents = client.get("/agents", params={"limit": 10, "offset": 0}).json()
            show("当前 Agent 数量", len(agents))

            # 2. 创建一个临时 Agent，并修改它的名称。
            agent = client.post("/agents", json={
                "name": "管理示例助手",
                "system_prompt": "请用一句话回答。",
            }).json()
            show("新建 Agent", agent["id"])
            renamed = client.patch(f"/agents/{agent['id']}", json={"name": "改名后的助手"})
            renamed.raise_for_status()
            show("修改后名称", renamed.json()["name"])

            # 3. 用这个 Agent 建会话，问一句，便于演示清空历史。
            session = client.post("/sessions", json={
                "agent_id": agent["id"], "title": "管理示例会话",
            }).json()["id"]
            show("新建会话", session)
            answer = client.post(f"/sessions/{session}/chat", json={"content": "你好"})
            answer.raise_for_status()
            show("回答", answer.json()["reply"])

            messages = client.get(f"/sessions/{session}/messages", params={"limit": 10}).json()
            show("消息条数", len(messages))

            # 4. 清空历史会返回删除条数，之后旧 request_id 可以再次使用。
            cleared = client.delete(f"/sessions/{session}/messages")
            cleared.raise_for_status()
            show("清空的历史条数", cleared.json()["deleted"])
            show("清空后的消息", client.get(f"/sessions/{session}/messages").json())

            # 5. 重命名会话，再删除会话，最后删除 Agent。
            title = client.patch(f"/sessions/{session}", json={"title": "改名后的会话"})
            title.raise_for_status()
            show("修改后标题", title.json()["title"])
            show("删除会话状态码", client.delete(f"/sessions/{session}").status_code)
            show("删除 Agent 状态码", client.delete(f"/agents/{agent['id']}").status_code)
    except httpx.HTTPStatusError as error:
        print(f"请求失败（HTTP {error.response.status_code}）：{error.response.text}")
        print("409 常见于会话正在生成、Agent 还有会话，或默认助手被要求删除。")
        raise SystemExit(1) from None
    except httpx.RequestError:
        print("无法连接 Gateway，请先运行 python run.py。")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
