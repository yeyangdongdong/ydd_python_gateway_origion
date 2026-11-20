"""统一演示模型和兼容 OpenAI Chat Completions 的流式接口。"""

import json
from collections.abc import AsyncIterator

import httpx

from .config import Settings
from .errors import GatewayError


async def _sse_data(response: httpx.Response) -> AsyncIterator[str]:
    """SSE 用空行分隔事件；同一事件的多个 data 行需要合并。"""
    data: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data:
                yield "\n".join(data)
                data.clear()
        elif line.startswith("data:"):
            # SSE 只移除冒号后的一个空格，不能随意去掉内容中的空白。
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
        # 注释、event、id 等行不包含本接口需要的模型正文。
    if data:
        yield "\n".join(data)


class ModelProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # trust_env=True 会让 httpx 读取系统代理。Windows 上开着代理软件时，
        # 连 http://127.0.0.1 的本地模型也会被送去代理，表现为莫名的 502。
        # 因此默认直连；需要代理访问远程服务时，设置 MODEL_USE_SYSTEM_PROXY=true。
        self.client = httpx.AsyncClient(
            timeout=settings.request_timeout,
            trust_env=settings.use_system_proxy,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def stream(
        self, messages: list[dict[str, str]], model: str
    ) -> AsyncIterator[str]:
        if self.settings.provider == "demo":
            answer = "[演示模式] 收到：" + messages[-1]["content"]
            for offset in range(0, len(answer), 8):
                yield answer[offset : offset + 8]
            return

        headers = {"Accept": "text/event-stream"}
        if self.settings.api_key:
            headers["Authorization"] = "Bearer " + self.settings.api_key
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "max_tokens": self.settings.max_tokens,
        }
        finished = False
        received_done = False
        has_text = False
        try:
            async with self.client.stream(
                "POST",
                self.settings.base_url.rstrip("/") + "/chat/completions",
                headers=headers,
                json=payload,
            ) as response:
                if response.status_code in {401, 403}:
                    raise GatewayError(502, "model_auth_error", "模型服务拒绝访问，请检查 MODEL_API_KEY 和模型权限。")
                if response.status_code == 429:
                    raise GatewayError(503, "model_rate_limited", "模型服务限流或额度不足，请检查账户后再重试。")
                if not response.is_success:
                    raise GatewayError(502, "model_http_error", f"模型服务返回 HTTP {response.status_code}，请检查服务地址和模型名称。")

                async for event in _sse_data(response):
                    if event == "[DONE]":
                        received_done = True
                        break
                    try:
                        chunk = json.loads(event)
                    except json.JSONDecodeError as exc:
                        raise GatewayError(502, "model_protocol_error", "模型服务返回了无法解析的流式数据。") from exc
                    if not isinstance(chunk, dict) or "error" in chunk:
                        raise GatewayError(502, "model_stream_error", "模型服务在生成过程中报告错误。")
                    choices = chunk.get("choices")
                    if not isinstance(choices, list):
                        raise GatewayError(502, "model_protocol_error", "模型响应缺少 choices 数组，请确认服务兼容 Chat Completions。")
                    if not choices:
                        # include_usage 等扩展可能在末尾发送一个仅包含用量的事件。
                        continue
                    choice = choices[0]
                    if not isinstance(choice, dict) or not isinstance(choice.get("delta"), dict):
                        raise GatewayError(502, "model_protocol_error", "模型响应中的 delta 格式错误。")
                    delta = choice["delta"]
                    if delta.get("refusal"):
                        raise GatewayError(502, "model_refused", "模型拒绝了本次请求，请调整输入后重试。")
                    if delta.get("tool_calls") or delta.get("function_call"):
                        raise GatewayError(502, "model_tools_unsupported", "基础版只支持文本回答，暂不执行模型工具调用。")
                    content = delta.get("content")
                    if content is not None and not isinstance(content, str):
                        raise GatewayError(502, "model_protocol_error", "模型返回的文本内容格式错误。")
                    if content:
                        has_text = has_text or bool(content.strip())
                        yield content
                    reason = choice.get("finish_reason")
                    if reason is not None:
                        if reason != "stop":
                            raise GatewayError(502, "model_incomplete", "模型未完整完成回答，可能达到输出上限或被服务中止。")
                        finished = True
        except httpx.TimeoutException as exc:
            raise GatewayError(504, "model_timeout", "等待模型响应超时，请检查服务或增大 MODEL_TIMEOUT_SECONDS。") from exc
        except httpx.RequestError as exc:
            raise GatewayError(502, "model_connection_error", "无法连接模型服务或连接已中断，请检查 MODEL_BASE_URL 和网络。") from exc

        # 断开 TCP 连接不等于成功：必须收到模型的完成标志和 SSE 结束标志。
        if not finished or not received_done:
            raise GatewayError(502, "model_incomplete", "模型流提前结束，未收到完整的完成标志。")
        if not has_text:
            raise GatewayError(502, "model_empty_reply", "模型没有返回文本回答，请检查模型是否支持文本对话。")
