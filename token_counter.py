"""Token 计数与上下文分析。"""
import json
from collections import Counter, defaultdict
from typing import Any, Dict, List

import tiktoken

from config import MAX_CONTEXT_TOKENS

FALLBACK_CHARS_PER_TOKEN = 3.5


def _get_encoder(model: str):
    """按模型取编码器，不认识的模型统一回退到通用编码。"""
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        return tiktoken.get_encoding("cl100k_base")


def count_text_tokens(text: str, model: str) -> int:
    """精确计数字符串 token，失败时自动回退到粗估。"""
    if not text:
        return 0

    try:
        encoder = _get_encoder(model)
        return len(encoder.encode(text))
    except Exception:
        return max(1, int(len(text) / FALLBACK_CHARS_PER_TOKEN))


def _stringify_message(message: Dict[str, Any]) -> str:
    """把消息归一化成字符串，方便统一计数。"""
    text_parts: List[str] = [message.get("role", "")]
    content = message.get("content", "")

    if isinstance(content, str):
        text_parts.append(content)
    else:
        text_parts.append(json.dumps(content, ensure_ascii=False))

    tool_calls = message.get("tool_calls", [])
    if tool_calls:
        text_parts.append(json.dumps(tool_calls, ensure_ascii=False))

    if message.get("tool_call_id"):
        text_parts.append(str(message["tool_call_id"]))

    return "\n".join(part for part in text_parts if part)


def count_message_tokens(message: Dict[str, Any], model: str) -> int:
    """精确统计单条消息 token。"""
    return count_text_tokens(_stringify_message(message), model)


def count_messages_tokens(messages: List[Dict[str, Any]], model: str) -> int:
    """统计整个上下文的 token。"""
    return sum(count_message_tokens(message, model) for message in messages)


def _collect_tool_call_names(messages: List[Dict[str, Any]]) -> Dict[str, str]:
    """从 assistant 的 tool_calls 中建立 id -> 工具名映射。"""
    mapping: Dict[str, str] = {}
    for message in messages:
        for tool_call in message.get("tool_calls", []):
            tool_call_id = tool_call.get("id")
            function = tool_call.get("function", {})
            if tool_call_id:
                mapping[tool_call_id] = function.get("name", "unknown")
    return mapping


def _collect_read_paths(messages: List[Dict[str, Any]]) -> Counter:
    """统计 readFile 的重复读取路径，帮助识别浪费上下文的行为。"""
    path_counter: Counter = Counter()
    for message in messages:
        for tool_call in message.get("tool_calls", []):
            function = tool_call.get("function", {})
            if function.get("name") != "readFile":
                continue

            arguments = function.get("arguments", "")
            try:
                parsed_args = json.loads(arguments) if arguments else {}
            except json.JSONDecodeError:
                parsed_args = {}

            path = parsed_args.get("path")
            if path:
                path_counter[path] += 1
    return path_counter


def analyze_context(messages: List[Dict[str, Any]], model: str) -> Dict[str, Any]:
    """输出上下文结构分析，类似一个轻量版 devtools 面板。"""
    tool_name_by_id = _collect_tool_call_names(messages)
    tokens_by_tool = defaultdict(int)
    tokens_by_role = defaultdict(int)

    total_tokens = 0
    for message in messages:
        token_count = count_message_tokens(message, model)
        total_tokens += token_count
        role = message.get("role", "unknown")
        tokens_by_role[role] += token_count

        if role == "tool":
            tool_name = tool_name_by_id.get(message.get("tool_call_id", ""), "unknown")
            tokens_by_tool[tool_name] += token_count

    repeated_reads = [
        {"path": path, "count": count}
        for path, count in _collect_read_paths(messages).items()
        if count > 1
    ]
    repeated_reads.sort(key=lambda item: item["count"], reverse=True)

    return {
        "total_tokens": total_tokens,
        "window_tokens": MAX_CONTEXT_TOKENS,
        "usage_ratio": (total_tokens / MAX_CONTEXT_TOKENS) if MAX_CONTEXT_TOKENS else 0.0,
        "tokens_by_role": dict(tokens_by_role),
        "tokens_by_tool": dict(tokens_by_tool),
        "repeated_reads": repeated_reads,
    }


def format_context_report(analysis: Dict[str, Any]) -> str:
    """把分析结果格式化成适合 CLI 输出的简短报告。"""
    total_tokens = analysis["total_tokens"]
    window_tokens = analysis["window_tokens"]
    usage_ratio = analysis["usage_ratio"] * 100
    lines = [f"  [上下文: {total_tokens:,} / {window_tokens:,} tokens, {usage_ratio:.1f}%]"]

    tool_tokens = analysis.get("tokens_by_tool", {})
    if tool_tokens:
        sorted_tools = sorted(tool_tokens.items(), key=lambda item: item[1], reverse=True)[:3]
        tool_summary = ", ".join(f"{name}:{count}" for name, count in sorted_tools)
        lines.append(f"  [工具占比: {tool_summary}]")

    repeated_reads = analysis.get("repeated_reads", [])
    if repeated_reads:
        top_read = repeated_reads[0]
        lines.append(
            f"  [重复读取提醒: {top_read['path']} 已读取 {top_read['count']} 次]"
        )

    return "\n".join(lines)
