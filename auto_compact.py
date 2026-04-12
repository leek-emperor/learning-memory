"""自动压缩 —— 用 LLM 把较早的对话压成可继续工作的摘要。"""
import json
import os
from typing import Any, Dict, List

from openai import OpenAI

from config import MAX_CONTEXT_TOKENS, OPENAI_API_BASE, OPENAI_API_KEY
from state import state
from token_counter import count_messages_tokens

AUTO_COMPACT_TRIGGER_RATIO = 0.85
AUTO_COMPACT_KEEP_RECENT_TURNS = 2
AUTO_COMPACT_MAX_FAILURES = 3
SUMMARY_MAX_CHARS_PER_MESSAGE = 1500

_client_kwargs = {"api_key": OPENAI_API_KEY}
if OPENAI_API_BASE:
    _client_kwargs["base_url"] = OPENAI_API_BASE
client = OpenAI(**_client_kwargs)


def should_auto_compact(total_tokens: int, max_context_tokens: int) -> bool:
    """判断是否达到自动摘要压缩阈值。"""
    if max_context_tokens <= 0:
        return False
    return total_tokens >= int(max_context_tokens * AUTO_COMPACT_TRIGGER_RATIO)


def _trim_text(value: Any, limit: int = SUMMARY_MAX_CHARS_PER_MESSAGE) -> str:
    """限制单条消息展开长度，避免压缩 prompt 自己又过大。"""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[已截断，共 {len(text)} 字符]"


def _split_turns(messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """按 user 消息切成轮次，便于保留最近 N 轮原始对话。"""
    turns: List[List[Dict[str, Any]]] = []
    current_turn: List[Dict[str, Any]] = []

    for message in messages:
        if message.get("role") == "user" and current_turn:
            turns.append(current_turn)
            current_turn = [message]
        else:
            current_turn.append(message)

    if current_turn:
        turns.append(current_turn)

    return turns


def _pick_recent_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """保留最近若干轮对话，尽量不拆开一个 user turn。"""
    turns = _split_turns(messages)
    if len(turns) <= AUTO_COMPACT_KEEP_RECENT_TURNS:
        return list(messages)

    kept_turns = turns[-AUTO_COMPACT_KEEP_RECENT_TURNS:]
    kept_messages: List[Dict[str, Any]] = []
    for turn in kept_turns:
        kept_messages.extend(turn)
    return kept_messages


def _format_messages_for_prompt(messages: List[Dict[str, Any]]) -> str:
    """把旧消息整理成摘要输入，控制内容体积。"""
    lines: List[str] = []
    for index, message in enumerate(messages, 1):
        role = message.get("role", "unknown")
        lines.append(f"[{index}] role={role}")

        if message.get("tool_calls"):
            lines.append(
                "tool_calls=" + _trim_text(message.get("tool_calls", []), limit=800)
            )

        if message.get("tool_call_id"):
            lines.append(f"tool_call_id={message['tool_call_id']}")

        lines.append("content=" + _trim_text(message.get("content", "")))
        lines.append("")

    return "\n".join(lines)


def _build_workspace_snapshot() -> str:
    """压缩后补回最关键的当前工作上下文。"""
    try:
        entries = sorted(os.listdir(state.workspace_root))[:20]
    except OSError:
        entries = []

    entry_lines = "\n".join(f"- {name}" for name in entries) if entries else "- (空工作区)"
    return (
        f"启动目录: {state.cwd}\n"
        f"工作区: {state.workspace_root}\n"
        f"工作区顶层文件:\n{entry_lines}"
    )


def _build_compact_prompt(old_messages: List[Dict[str, Any]]) -> str:
    """构建结构化摘要提示词，覆盖请求、文件、错误与下一步工作。"""
    transcript = _format_messages_for_prompt(old_messages)
    return f"""请把下面这段旧对话压缩成一个供 AI 编程助手继续工作的摘要。

要求：
1. 用中文。
2. 保留用户目标、已做修改、涉及文件、失败尝试、工具调用结果、未完成任务。
3. 如果出现文件路径、命令、报错、工具参数，尽量保留原文。
4. 不要编造未发生的事实。
5. 输出固定 9 个部分，标题如下：
   1) 用户目标
   2) 当前实现状态
   3) 关键文件
   4) 重要工具结果
   5) 已修复问题
   6) 未解决问题
   7) 当前约束
   8) 建议下一步
   9) 需要继续记住的细节

以下是待压缩的旧对话：

{transcript}
"""


async def maybe_auto_compact(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """超过阈值时调用 LLM 生成摘要，并返回替换后的消息。"""
    total_tokens = count_messages_tokens(messages, state.model)
    if not should_auto_compact(total_tokens, MAX_CONTEXT_TOKENS):
        return {"changed": False, "messages": messages}

    if state.auto_compact_failures >= AUTO_COMPACT_MAX_FAILURES:
        return {
            "changed": False,
            "messages": messages,
            "error": "自动压缩已连续失败 3 次，已进入熔断状态",
        }

    recent_messages = _pick_recent_messages(messages)
    if len(recent_messages) >= len(messages):
        return {"changed": False, "messages": messages}

    old_messages = messages[: len(messages) - len(recent_messages)]
    prompt = _build_compact_prompt(old_messages)

    try:
        response = client.chat.completions.create(
            model=state.model,
            messages=[
                {
                    "role": "system",
                    "content": "你是一个负责做上下文压缩的助手，只能输出事实摘要。",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=1200,
        )
        summary = response.choices[0].message.content or "[自动压缩未生成摘要]"
    except Exception as exc:
        state.record_auto_compact_failure()
        return {
            "changed": False,
            "messages": messages,
            "error": f"自动压缩失败: {exc}",
        }

    state.increment_auto_compact_count()
    state.reset_auto_compact_failures()

    summary_message = {
        "role": "assistant",
        "content": "[历史上下文摘要]\n" + summary,
        "auto_compact_summary": True,
    }
    boundary_message = {
        "role": "assistant",
        "content": "[以下开始为压缩后保留的最近原始对话]",
        "compact_boundary": True,
    }
    workspace_message = {
        "role": "assistant",
        "content": "[当前工作区快照]\n" + _build_workspace_snapshot(),
        "workspace_snapshot": True,
    }

    new_messages = [summary_message, workspace_message, boundary_message] + recent_messages
    freed_tokens = max(
        0,
        total_tokens - count_messages_tokens(new_messages, state.model),
    )
    return {
        "changed": True,
        "messages": new_messages,
        "freed_tokens": freed_tokens,
    }
