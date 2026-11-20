"""把会话历史裁剪到模型的上下文预算之内。

模型每次只看得到"系统提示词 + 历史 + 当前提问"。历史无限增长时，总长度
迟早超过模型的上下文窗口，上游会直接报错。这里的做法是：

1. 用字符启发式估算 token 数（不引入分词器依赖）；
2. 按预算从最旧的"一问一答"开始整对丢弃，保证角色仍然严格交替；
3. 丢弃了内容就补一句提示，让模型知道前面还有对话被省略；
4. 连"系统提示词 + 当前提问"都放不下时直接报错，绝不悄悄截断用户的原话。

裁剪只影响这一次发给模型的内容，数据库里的历史一条都不删。
"""

import math
from dataclasses import dataclass

from .errors import GatewayError

# 一条消息除正文外的固定开销（角色标记、分隔符等）。
MESSAGE_OVERHEAD = 4

# 估算总有误差，留一点余量，宁可少带历史也不要超限。
SAFETY_TOKENS = 128

NOTICE = "（较早的 {count} 条对话已省略，以适配模型上下文。）"

# 这些字符按 1 字约 1 token 估算；其余字符按 3 个约 1 token。
WIDE_RANGES = (
    (0x3000, 0x303F),  # 中文标点
    (0x3040, 0x30FF),  # 日文假名
    (0x4E00, 0x9FFF),  # 中日韩统一表意文字
    (0xAC00, 0xD7AF),  # 韩文
    (0xFF00, 0xFFEF),  # 全角字符
)


def _is_wide(character: str) -> bool:
    code = ord(character)
    return any(start <= code <= end for start, end in WIDE_RANGES)


def estimate_tokens(text: str) -> int:
    """粗略估算一段文本的 token 数。

    中文里 1 个汉字通常就是 1 个 token；英文和代码大约是 3~4 个字符 1 个 token。
    这里固定按 3 个字符 1 个 token 估算，偏保守：低估会重新触发上游超限，
    高估只是少带一点历史，代价更小。
    """
    wide = sum(1 for character in text if _is_wide(character))
    narrow = len(text) - wide
    return wide + math.ceil(narrow / 3)


def message_tokens(message: dict[str, str]) -> int:
    """一条消息占用的 token 数，包含固定开销。"""
    return estimate_tokens(message["content"]) + MESSAGE_OVERHEAD


def _turns(history: list[dict]) -> list[list[dict]]:
    """把历史切成"一问一答"的轮次，丢弃时以轮次为单位。"""
    turns: list[list[dict]] = []
    for message in history:
        # 用户消息开启新一轮；正常数据里历史严格交替，这里对异常数据也保持安全。
        if message["role"] == "user" or not turns:
            turns.append([message])
        else:
            turns[-1].append(message)
    return turns


@dataclass(frozen=True)
class PreparedContext:
    """一次模型调用真正要发送的内容，以及它的预算使用情况。"""

    messages: list[dict[str, str]]
    # 被省略的历史消息条数（0 表示全部保留）。
    trimmed_messages: int
    # 估算的提示词 token 总数。
    estimated_tokens: int
    # 本次允许的提示词 token 上限。
    budget_tokens: int


def build_context(
    history: list[dict],
    *,
    system_prompt: str,
    user_content: str,
    context_tokens: int,
    max_tokens: int,
) -> PreparedContext:
    """按预算组装消息列表；放不下当前提问时抛 413。"""
    system_message = {"role": "system", "content": system_prompt}
    user_message = {"role": "user", "content": user_content}

    # 上下文窗口同时要容纳提示词和回答，所以先给回答留出 max_tokens。
    budget = context_tokens - max_tokens - SAFETY_TOKENS
    fixed = message_tokens(system_message) + message_tokens(user_message)

    # 先按"肯定会省略"预留提示语的位置，避免为是否省略而反复试算。
    notice_reserve = message_tokens({"role": "system", "content": NOTICE.format(count=len(history))})
    remaining = budget - fixed - notice_reserve

    if remaining < 0:
        raise GatewayError(
            413, "context_too_large",
            f"本次提问连同系统提示词约需 {fixed} 个 token，超过可用预算 {budget}。"
            "请缩短输入，或调大 MODEL_CONTEXT_TOKENS。",
        )

    kept: list[list[dict]] = []
    for turn in reversed(_turns(history)):
        cost = sum(message_tokens(message) for message in turn)
        if cost > remaining:
            # 更旧的轮次只会更长，可以直接停止。
            break
        kept.append(turn)
        remaining -= cost
    kept.reverse()

    trimmed = len(history) - sum(len(turn) for turn in kept)
    messages = [system_message]
    if trimmed:
        messages.append({"role": "system", "content": NOTICE.format(count=trimmed)})
    for turn in kept:
        # 只保留 role 和 content：数据库行还带着 id、时间等字段，不能发给模型。
        messages.extend({"role": message["role"], "content": message["content"]} for message in turn)
    messages.append(user_message)

    return PreparedContext(
        messages=messages,
        trimmed_messages=trimmed,
        estimated_tokens=sum(message_tokens(message) for message in messages),
        budget_tokens=budget,
    )
