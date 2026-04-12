"""微压缩 —— 优先清理旧的工具结果，尽量不影响当前工作上下文。"""
from typing import Any, Dict, List, Sequence

from token_counter import count_messages_tokens

PLACEHOLDER_TEMPLATE = "[Old result cleared by micro compact: {tool_name}]"
MICRO_COMPACT_TRIGGER_RATIO = 0.70


def _build_tool_name_map(messages: List[Dict[str, Any]]) -> Dict[str, str]:
    """建立 tool_call_id 到工具名的映射，方便判断哪些结果可压缩。"""
    result: Dict[str, str] = {}
    for message in messages:
        for tool_call in message.get("tool_calls", []):
            tool_call_id = tool_call.get("id")
            function = tool_call.get("function", {})
            if tool_call_id:
                result[tool_call_id] = function.get("name", "unknown")
    return result


def _is_placeholder(content: Any) -> bool:
    return isinstance(content, str) and content.startswith("[Old result cleared by micro compact:")


def should_auto_micro_compact(total_tokens: int, max_context_tokens: int) -> bool:
    """是否需要在每轮前自动做一次微压缩。"""
    if max_context_tokens <= 0:
        return False
    return total_tokens >= int(max_context_tokens * MICRO_COMPACT_TRIGGER_RATIO)


def apply_micro_compact(
    messages: List[Dict[str, Any]],
    compactable_tools: Sequence[str],
    model: str,
    keep_recent: int = 5,
) -> Dict[str, Any]:
    """对旧工具结果做占位替换，释放上下文空间。"""
    tool_name_by_id = _build_tool_name_map(messages)
    candidate_indexes: List[int] = []

    for index, message in enumerate(messages):
        if message.get("role") != "tool":
            continue

        tool_name = tool_name_by_id.get(message.get("tool_call_id", ""), "unknown")
        content = message.get("content", "")
        if tool_name not in compactable_tools or _is_placeholder(content):
            continue

        candidate_indexes.append(index)

    if len(candidate_indexes) <= keep_recent:
        return {
            "changed": False,
            "messages": messages,
            "freed_tokens": 0,
            "replaced_count": 0,
        }

    before_tokens = count_messages_tokens(messages, model)
    # `keep_recent=0` 时应该清掉所有候选，而不是因为 `[:-0]` 变成空切片。
    replace_indexes = candidate_indexes if keep_recent <= 0 else candidate_indexes[:-keep_recent]
    for index in replace_indexes:
        message = messages[index]
        tool_name = tool_name_by_id.get(message.get("tool_call_id", ""), "unknown")
        original_content = message.get("content", "")
        char_count = len(original_content) if isinstance(original_content, str) else 0
        message["content"] = PLACEHOLDER_TEMPLATE.format(tool_name=tool_name)
        message["micro_compacted"] = True
        message["original_char_count"] = char_count

    after_tokens = count_messages_tokens(messages, model)
    return {
        "changed": True,
        "messages": messages,
        "freed_tokens": max(0, before_tokens - after_tokens),
        "replaced_count": len(replace_indexes),
    }
