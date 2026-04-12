"""LLM Loop —— 核心对话循环

对应 Claude Code 的 QueryEngine.ts，是整个系统的"心脏"。
每次用户输入 → 调用 LLM → 如果有工具调用则执行 → 循环直到最终回复。
"""
import json
from typing import Any, Dict, List, Optional, Sequence

from openai import OpenAI
from auto_compact import maybe_auto_compact
from config import OPENAI_API_KEY, OPENAI_API_BASE, MAX_CONTEXT_TOKENS
from micro_compact import apply_micro_compact, should_auto_micro_compact
from state import state, SessionPhase
from token_counter import (
    count_message_tokens as precise_count_message_tokens,
    count_messages_tokens,
)

# 初始化 OpenAI 客户端（OpenAI 兼容端点）
_client_kwargs = {"api_key": OPENAI_API_KEY}
if OPENAI_API_BASE:
    _client_kwargs["base_url"] = OPENAI_API_BASE
client = OpenAI(**_client_kwargs)

SYSTEM_PROMPT = """你是一个有帮助的 AI 编程助手。你可以使用工具来帮助用户。
请用中文回复用户。"""


def count_message_tokens(msg: dict) -> int:
    """兼容旧接口，内部改为精确 token 计数。"""
    return precise_count_message_tokens(msg, state.model)


def estimate_total_tokens(messages: list[dict]) -> int:
    """兼容旧接口，内部改为精确 token 计数。"""
    return count_messages_tokens(messages, state.model)


def build_api_messages(messages: list[dict]) -> list[dict]:
    """将内部消息格式转换为 OpenAI API 格式

    内部格式: role + content（统一字符串）
    API 格式:  role + content（字符串或 tool_calls 列表）
    """
    api_msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    for msg in messages:
        role = msg["role"]
        content = msg.get("content", "")

        if role == "tool":
            # tool_result 消息
            api_msgs.append({
                "role": "tool",
                "tool_call_id": msg["tool_call_id"],
                "content": str(content),
            })
        elif role == "assistant" and "tool_calls" in msg:
            # 带 tool_calls 的 assistant 消息
            api_msgs.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": msg["tool_calls"],
            })
        else:
            api_msgs.append({"role": role, "content": str(content)})

    return api_msgs


async def _append_message(
    messages: List[Dict[str, Any]],
    message: Dict[str, Any],
    session_store=None,
) -> None:
    """统一追加消息并做持久化，避免主循环里反复写样板代码。"""
    messages.append(message)
    if session_store is not None:
        await session_store.append_message(message, messages)


async def chat_loop(
    messages: list[dict],
    tools: list[dict],
    tool_handlers: dict,
    session_store=None,
    compactable_tools: Optional[Sequence[str]] = None,
    max_iterations: int = 20,
) -> list[dict]:
    """核心对话循环

    对应 Claude Code 的 QueryEngine.run()。

    流程:
      1. 调用 LLM
      2. 如果有 tool_calls → 执行工具 → 将结果追加到 messages → 回到 1
      3. 如果没有 tool_calls → 返回最终回复

    Args:
        messages: 对话历史（会被原地修改）
        tools: OpenAI 工具定义列表
        tool_handlers: {tool_name: async_handler} 映射
        max_iterations: 最大工具调用轮次（防止死循环）

    Returns:
        更新后的 messages 列表
    """
    state.set_phase(SessionPhase.RUNNING)
    compactable_tools = compactable_tools or []

    for _ in range(max_iterations):
        # 每轮开始先看上下文是否已经偏大，优先清理旧工具结果。
        current_tokens = count_messages_tokens(messages, state.model)
        state.set_last_context_tokens(current_tokens)
        if compactable_tools and should_auto_micro_compact(current_tokens, MAX_CONTEXT_TOKENS):
            compact_result = apply_micro_compact(
                messages,
                compactable_tools=compactable_tools,
                model=state.model,
            )
            if compact_result["changed"]:
                state.increment_micro_compact_count()
                print(
                    f"  [微压缩] 清理 {compact_result['replaced_count']} 条旧工具结果，"
                    f"释放 ~{compact_result['freed_tokens']} tokens"
                )
                if session_store is not None:
                    await session_store.append_snapshot(messages, "auto_micro_compact")

        # 构建发给 API 的消息
        api_messages = build_api_messages(messages)

        # 调用 LLM
        response = client.chat.completions.create(
            model=state.model,
            messages=api_messages,
            tools=tools if tools else None,
            max_tokens=4096,
        )

        choice = response.choices[0]
        assistant_msg = choice.message

        # 累计 token 用量
        usage = response.usage
        if usage:
            # 粗略成本估算（gpt-4o-mini: $0.15/1M input, $0.60/1M output）
            input_cost = (usage.prompt_tokens / 1_000_000) * 0.15
            output_cost = (usage.completion_tokens / 1_000_000) * 0.60
            state.accumulate_usage(usage.prompt_tokens, usage.completion_tokens, input_cost + output_cost)

        # 检查是否有工具调用
        tool_calls = assistant_msg.tool_calls

        if not tool_calls:
            # 没有工具调用 → 最终回复
            final_msg = {
                "role": "assistant",
                "content": assistant_msg.content or "",
            }
            await _append_message(messages, final_msg, session_store)

            # 本轮结束后做一次自动摘要压缩，避免上下文无限增长。
            auto_compact_result = await maybe_auto_compact(messages)
            if auto_compact_result.get("changed"):
                messages = auto_compact_result["messages"]
                print(
                    f"  [自动压缩] 生成历史摘要，释放 ~{auto_compact_result['freed_tokens']} tokens"
                )
                if session_store is not None:
                    await session_store.append_snapshot(messages, "auto_compact")
            elif auto_compact_result.get("error"):
                print(f"  [自动压缩] {auto_compact_result['error']}")

            state.set_last_context_tokens(count_messages_tokens(messages, state.model))
            state.set_phase(SessionPhase.IDLE)
            return messages

        # 有工具调用 → 构建 assistant 消息（含 tool_calls）
        assistant_record = {
            "role": "assistant",
            "content": assistant_msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ],
        }
        await _append_message(messages, assistant_record, session_store)

        # 执行所有工具调用
        for tc in tool_calls:
            fn_name = tc.function.name
            fn_args_str = tc.function.arguments

            try:
                fn_args = json.loads(fn_args_str) if fn_args_str else {}
            except json.JSONDecodeError:
                fn_args = {}

            print(f"\n  🔧 调用工具: {fn_name}({json.dumps(fn_args, ensure_ascii=False)[:100]})")

            handler = tool_handlers.get(fn_name)
            if handler is None:
                result = f"错误: 未知工具 '{fn_name}'"
            else:
                try:
                    result = await handler(fn_args)
                except Exception as e:
                    result = f"工具执行错误: {e}"

            # 将工具结果追加到消息
            await _append_message(messages, {
                "role": "tool",
                "tool_call_id": tc.id,
                "content": str(result),
            }, session_store)

    # 超过最大迭代次数
    await _append_message(messages, {
        "role": "assistant",
        "content": "[已达到最大工具调用轮次，停止执行]",
    }, session_store)
    state.set_phase(SessionPhase.IDLE)
    return messages
