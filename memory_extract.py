"""每轮对话后的长期记忆提取。"""
import json
from typing import Any, Dict, List

from openai import OpenAI

from config import OPENAI_API_BASE, OPENAI_API_KEY
from memdir import VALID_TYPES, write_memory
from state import state

MAX_EXTRACT_MESSAGES = 12

_client_kwargs = {"api_key": OPENAI_API_KEY}
if OPENAI_API_BASE:
    _client_kwargs["base_url"] = OPENAI_API_BASE
client = OpenAI(**_client_kwargs)


def _should_skip_message(message: Dict[str, Any]) -> bool:
    return bool(
        message.get("memory_context")
        or message.get("auto_compact_summary")
        or message.get("compact_boundary")
        or message.get("workspace_snapshot")
    )


def _build_extract_prompt(messages: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for index, message in enumerate(messages, 1):
        role = message.get("role", "unknown")
        content = message.get("content", "")
        lines.append(f"[{index}] role={role}")
        lines.append(str(content))
        lines.append("")

    transcript = "\n".join(lines)
    return (
        "你是一个长期记忆提取助手。请从下面新增对话中提取值得跨会话保存的稳定信息。\n"
        "可用类型只有 user / feedback / project / reference。\n"
        "不要保存临时状态、一次性调试信息、Git 历史、短期目录快照。\n"
        "只返回 JSON 数组，每项格式为："
        "{\"type\":\"user\",\"name\":\"...\",\"description\":\"...\",\"body\":\"...\"}。\n"
        "如果没有值得保存的内容，返回 []。\n\n"
        f"{transcript}"
    )


def _parse_memories(content: str) -> List[Dict[str, str]]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return []

    if not isinstance(data, list):
        return []

    parsed: List[Dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue

        memory_type = str(item.get("type", "")).strip()
        if memory_type not in VALID_TYPES:
            continue

        name = str(item.get("name", "")).strip()
        description = str(item.get("description", "")).strip()
        body = str(item.get("body", "")).strip()
        if not (name and description and body):
            continue

        parsed.append(
            {
                "type": memory_type,
                "name": name,
                "description": description,
                "body": body,
            }
        )
    return parsed


async def extract_memories_from_messages(messages: List[Dict[str, Any]]) -> int:
    """分析新增消息窗口，必要时写入长期记忆。"""
    if state.memory_written_this_turn:
        state.last_processed_msg_index = len(messages)
        return 0

    new_messages = messages[state.last_processed_msg_index :]
    filtered_messages = [message for message in new_messages if not _should_skip_message(message)]
    if not filtered_messages:
        state.last_processed_msg_index = len(messages)
        return 0

    prompt = _build_extract_prompt(filtered_messages[-MAX_EXTRACT_MESSAGES:])
    try:
        response = client.chat.completions.create(
            model=state.model,
            messages=[
                {
                    "role": "system",
                    "content": "你只负责提取长期有价值的稳定记忆，输出必须是 JSON。",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=600,
        )
        content = response.choices[0].message.content or "[]"
    except Exception:
        state.last_processed_msg_index = len(messages)
        return 0

    extracted_items = _parse_memories(content)
    saved_count = 0
    for item in extracted_items:
        write_memory(
            memory_type=item["type"],
            name=item["name"],
            description=item["description"],
            body=item["body"],
        )
        saved_count += 1

    if saved_count:
        state.mark_memory_written_this_turn()

    state.last_processed_msg_index = len(messages)
    return saved_count
