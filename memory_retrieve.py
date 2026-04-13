"""长期记忆检索与注入。"""
import json
import time
from typing import Dict, List

from openai import OpenAI

from config import OPENAI_API_BASE, OPENAI_API_KEY
from memdir import MemoryItem, scan_memories
from state import state

STALE_MEMORY_DAYS = 7
MAX_RELEVANT_MEMORIES = 5

_client_kwargs = {"api_key": OPENAI_API_KEY}
if OPENAI_API_BASE:
    _client_kwargs["base_url"] = OPENAI_API_BASE
client = OpenAI(**_client_kwargs)


def _is_stale(memory: MemoryItem) -> bool:
    return (time.time() - memory.created_at) > (STALE_MEMORY_DAYS * 24 * 3600)


def _build_candidate_prompt(user_input: str, candidates: List[MemoryItem]) -> str:
    candidate_lines = []
    for item in candidates:
        candidate_lines.append(
            f"- id={item.memory_id} | type={item.type} | name={item.name} | description={item.description}"
        )

    return (
        "你是一个记忆检索助手。请从下面候选长期记忆中，选择与当前用户输入最相关的最多 5 条。\n"
        "只返回 JSON，对象格式为 {\"memory_ids\": [\"id1\", \"id2\"]}。\n"
        "如果没有明显相关内容，返回 {\"memory_ids\": []}。\n\n"
        f"用户输入：{user_input}\n\n"
        "候选记忆：\n"
        + "\n".join(candidate_lines)
    )


def _parse_memory_ids(content: str) -> List[str]:
    try:
        data = json.loads(content)
        raw_ids = data.get("memory_ids", [])
        if isinstance(raw_ids, list):
            return [str(item) for item in raw_ids[:MAX_RELEVANT_MEMORIES]]
    except json.JSONDecodeError:
        pass
    return []


def _memory_context_message(memory: MemoryItem) -> Dict[str, str]:
    prefix = "[相关长期记忆]"
    if _is_stale(memory):
        prefix += "\n[注意] 此记忆可能已过时，使用前请再次验证。"

    content = (
        f"{prefix}\n"
        f"类型: {memory.type}\n"
        f"名称: {memory.name}\n"
        f"描述: {memory.description}\n\n"
        f"{memory.body}"
    )
    return {
        "role": "user",
        "content": content,
        "memory_context": True,
        "memory_id": memory.memory_id,
    }


async def inject_relevant_memories(messages: List[Dict], user_input: str) -> List[Dict]:
    """根据当前输入检索相关长期记忆，并返回要注入的上下文消息。"""
    candidates = [
        item
        for item in scan_memories()
        if item.memory_id not in state.recent_surfaced_memory_ids
    ]
    if not candidates:
        return []

    prompt = _build_candidate_prompt(user_input, candidates[:40])
    try:
        response = client.chat.completions.create(
            model=state.model,
            messages=[
                {
                    "role": "system",
                    "content": "你只负责从候选长期记忆中选择最相关的项目。",
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=256,
        )
        content = response.choices[0].message.content or '{"memory_ids": []}'
    except Exception:
        return []

    selected_ids = _parse_memory_ids(content)
    if not selected_ids:
        return []

    memory_map = {item.memory_id: item for item in candidates}
    selected = [
        memory_map[memory_id]
        for memory_id in selected_ids
        if memory_id in memory_map
    ][:MAX_RELEVANT_MEMORIES]

    if not selected:
        return []

    state.note_surfaced_memory_ids([item.memory_id for item in selected])
    return [_memory_context_message(item) for item in selected]
