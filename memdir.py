"""长期记忆目录管理：memory/*.md + MEMORY.md 索引。"""
import os
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from config import MEMORY_DIR

INDEX_FILE_NAME = "MEMORY.md"
INDEX_MAX_LINES = 200
INDEX_MAX_BYTES = 25 * 1024

VALID_TYPES = {"user", "feedback", "project", "reference"}


@dataclass
class MemoryItem:
    """单条记忆的元信息与正文。"""

    memory_id: str
    file_name: str
    name: str
    type: str
    description: str
    created_at: float
    body: str


FRONTMATTER_BOUNDARY = "---"
FRONTMATTER_LINE_PATTERN = re.compile(r"^(?P<key>[a-zA-Z_]+)\s*:\s*(?P<value>.*)$")


def _ensure_memory_dir() -> None:
    os.makedirs(MEMORY_DIR, exist_ok=True)


def _index_path() -> str:
    return os.path.join(MEMORY_DIR, INDEX_FILE_NAME)


def _slugify(value: str) -> str:
    """把 name 变成文件名安全的 slug。"""
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or "memory"


def _memory_file_name(memory_type: str, name: str) -> str:
    return f"{memory_type}--{_slugify(name)}.md"


def _build_frontmatter(item: MemoryItem) -> str:
    return (
        f"{FRONTMATTER_BOUNDARY}\n"
        f"name: {item.name}\n"
        f"type: {item.type}\n"
        f"description: {item.description}\n"
        f"created_at: {item.created_at}\n"
        f"{FRONTMATTER_BOUNDARY}\n"
    )


def _parse_frontmatter(lines: List[str]) -> Tuple[Dict[str, str], int]:
    """解析 frontmatter，返回 dict 和正文起始行号。"""
    if not lines or lines[0].strip() != FRONTMATTER_BOUNDARY:
        return {}, 0

    meta: Dict[str, str] = {}
    for idx in range(1, len(lines)):
        raw = lines[idx].rstrip("\n")
        if raw.strip() == FRONTMATTER_BOUNDARY:
            return meta, idx + 1

        match = FRONTMATTER_LINE_PATTERN.match(raw)
        if match:
            meta[match.group("key")] = match.group("value")

    return meta, 0


def _read_memory_file(file_path: str) -> Optional[MemoryItem]:
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            lines = file.read().splitlines()
    except OSError:
        return None

    meta, body_start = _parse_frontmatter(lines)
    if not meta:
        return None

    memory_type = meta.get("type", "").strip()
    if memory_type not in VALID_TYPES:
        return None

    name = meta.get("name", "").strip()
    description = meta.get("description", "").strip()
    created_at_raw = meta.get("created_at", "0").strip()
    try:
        created_at = float(created_at_raw)
    except ValueError:
        created_at = 0.0

    body = "\n".join(lines[body_start:]).strip()
    file_name = os.path.basename(file_path)
    memory_id = os.path.splitext(file_name)[0]
    return MemoryItem(
        memory_id=memory_id,
        file_name=file_name,
        name=name,
        type=memory_type,
        description=description,
        created_at=created_at,
        body=body,
    )


def scan_memories() -> List[MemoryItem]:
    """扫描 memory 目录下的所有记忆文件。"""
    _ensure_memory_dir()
    result: List[MemoryItem] = []
    for file_name in os.listdir(MEMORY_DIR):
        if not file_name.endswith(".md"):
            continue
        if file_name == INDEX_FILE_NAME:
            continue

        file_path = os.path.join(MEMORY_DIR, file_name)
        item = _read_memory_file(file_path)
        if item is not None:
            result.append(item)

    result.sort(key=lambda item: item.created_at, reverse=True)
    return result


def read_memory_index() -> str:
    """读取 MEMORY.md 作为索引注入内容。"""
    path = _index_path()
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as file:
            return file.read()
    except OSError:
        return ""


def _truncate_index_if_needed(content: str) -> str:
    raw = content.encode("utf-8", errors="ignore")
    lines = content.splitlines()
    if len(lines) <= INDEX_MAX_LINES and len(raw) <= INDEX_MAX_BYTES:
        return content

    trimmed_lines = lines[-INDEX_MAX_LINES:]
    trimmed = "\n".join(trimmed_lines)
    trimmed = (
        "> ⚠️ MEMORY.md 已超过上限，已从末尾截断显示（索引仍然可用）。\n\n" + trimmed
    )
    raw_trimmed = trimmed.encode("utf-8", errors="ignore")
    if len(raw_trimmed) > INDEX_MAX_BYTES:
        trimmed = raw_trimmed[-INDEX_MAX_BYTES:].decode("utf-8", errors="ignore")
    return trimmed


def update_index(_item: Optional[MemoryItem] = None) -> None:
    """兼容旧命名：更新单条后整体重建索引。"""
    rebuild_index()


def rebuild_index() -> None:
    """根据当前所有记忆文件重建 MEMORY.md。"""
    _ensure_memory_dir()
    lines = ["# MEMORY", ""]
    for item in scan_memories():
        lines.append(
            f"- [{item.type}] {item.name} ({item.file_name}) - {item.description}"
        )

    content = _truncate_index_if_needed("\n".join(lines).rstrip() + "\n")
    with open(_index_path(), "w", encoding="utf-8") as file:
        file.write(content)


def _find_existing_by_type_and_name(memory_type: str, name: str) -> Optional[MemoryItem]:
    normalized_name = name.strip()
    for item in scan_memories():
        if item.type == memory_type and item.name.strip() == normalized_name:
            return item
    return None


def write_memory(
    memory_type: str,
    name: str,
    description: str,
    body: str,
    created_at: Optional[float] = None,
) -> MemoryItem:
    """写入或更新一条记忆，并更新索引。"""
    if memory_type not in VALID_TYPES:
        raise ValueError(f"不支持的记忆类型: {memory_type}")

    _ensure_memory_dir()
    created_at_value = float(created_at) if created_at is not None else time.time()

    existing = _find_existing_by_type_and_name(memory_type, name)
    file_name = existing.file_name if existing else _memory_file_name(memory_type, name)
    memory_id = os.path.splitext(file_name)[0]

    item = MemoryItem(
        memory_id=memory_id,
        file_name=file_name,
        name=name.strip(),
        type=memory_type,
        description=description.strip(),
        created_at=existing.created_at if existing else created_at_value,
        body=body.strip(),
    )

    file_path = os.path.join(MEMORY_DIR, file_name)
    content = _build_frontmatter(item) + "\n" + item.body + "\n"
    with open(file_path, "w", encoding="utf-8") as file:
        file.write(content)

    rebuild_index()
    return item


def delete_memory(file_name: str) -> bool:
    """删除单条记忆文件（不做索引清理，避免复杂度；后续可再补）。"""
    file_path = os.path.join(MEMORY_DIR, file_name)
    if not os.path.isfile(file_path):
        return False
    try:
        os.remove(file_path)
        rebuild_index()
        return True
    except OSError:
        return False


def clear_all_memories() -> int:
    """清空所有记忆文件（不删除 MEMORY.md）。"""
    count = 0
    for item in scan_memories():
        if delete_memory(item.file_name):
            count += 1
    rebuild_index()
    return count
