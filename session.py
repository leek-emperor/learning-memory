"""会话持久化 —— JSONL 日志、外部大内容存储、历史恢复。"""
import atexit
import asyncio
import copy
import json
import os
import time
from typing import Any, Dict, List, Optional

from config import DATA_ROOT, SESSION_DIR
from state import state

TRANSCRIPT_FILE = "transcript.jsonl"
META_FILE = "meta.json"
BLOBS_DIR = "blobs"
COMMAND_HISTORY_FILE = os.path.join(DATA_ROOT, "command_history.txt")
EXTERNAL_CONTENT_THRESHOLD = 4000
CONTENT_PREVIEW_CHARS = 200
FLUSH_DELAY_SECONDS = 0.1


def _safe_json_dump(data: Any) -> str:
    """统一 JSON 输出，避免每处重复指定参数。"""
    return json.dumps(data, ensure_ascii=False)


def setup_command_history() -> None:
    """启用终端命令历史，支持方向键浏览历史输入。"""
    try:
        import readline
    except ImportError:
        return

    os.makedirs(os.path.dirname(COMMAND_HISTORY_FILE), exist_ok=True)
    if os.path.exists(COMMAND_HISTORY_FILE):
        try:
            readline.read_history_file(COMMAND_HISTORY_FILE)
        except OSError:
            pass

    readline.set_history_length(1000)

    def _save_history() -> None:
        try:
            readline.write_history_file(COMMAND_HISTORY_FILE)
        except OSError:
            pass

    atexit.register(_save_history)


def add_command_history(command: str) -> None:
    """将命令加入 readline 历史。"""
    try:
        import readline
    except ImportError:
        return

    if command.strip():
        readline.add_history(command)


class SessionStore:
    """单会话持久化管理器。

    类比前端里持久化中间层：
    - `messages` 是内存态
    - `transcript.jsonl` 是 append-only action log
    - `meta.json` 是当前快照
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.session_dir = os.path.join(SESSION_DIR, session_id)
        self.transcript_path = os.path.join(self.session_dir, TRANSCRIPT_FILE)
        self.meta_path = os.path.join(self.session_dir, META_FILE)
        self.blob_dir = os.path.join(self.session_dir, BLOBS_DIR)
        self._pending_lines: List[str] = []
        self._flush_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._blob_counter = 0
        self._created_at = time.time()

        os.makedirs(self.blob_dir, exist_ok=True)
        self._restore_local_state()

    def _restore_local_state(self) -> None:
        """从已有文件恢复本地计数器，避免覆盖历史会话。"""
        meta = self.read_meta(self.session_id)
        if meta:
            self._created_at = float(meta.get("created_at", self._created_at))

        if not os.path.isdir(self.blob_dir):
            return

        existing_names = sorted(os.listdir(self.blob_dir))
        if not existing_names:
            return

        last_name = existing_names[-1]
        stem = os.path.splitext(last_name)[0]
        if stem.startswith("msg_"):
            try:
                self._blob_counter = int(stem.split("_", 1)[1])
            except (IndexError, ValueError):
                self._blob_counter = len(existing_names)

    def _next_blob_name(self, extension: str = ".txt") -> str:
        """生成递增 blob 文件名，便于排查问题。"""
        self._blob_counter += 1
        return f"msg_{self._blob_counter:06d}{extension}"

    def _write_blob(self, content: str) -> Dict[str, Any]:
        """将超大内容写到独立文件，并在 JSONL 中存引用。"""
        file_name = self._next_blob_name()
        blob_path = os.path.join(self.blob_dir, file_name)
        with open(blob_path, "w", encoding="utf-8") as file:
            file.write(content)

        return {
            "storage": "external",
            "path": f"{BLOBS_DIR}/{file_name}",
            "preview": content[:CONTENT_PREVIEW_CHARS],
            "char_count": len(content),
        }

    def _inline_or_externalize_content(self, content: Any) -> Any:
        """按阈值决定内容是内联还是外部存储。"""
        if isinstance(content, str) and len(content) > EXTERNAL_CONTENT_THRESHOLD:
            return self._write_blob(content)
        return content

    def _serialize_message(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """序列化消息，必要时将大内容转成外部引用。"""
        stored_message = copy.deepcopy(message)
        stored_message["content"] = self._inline_or_externalize_content(
            stored_message.get("content", "")
        )
        return stored_message

    def _restore_content(self, content: Any) -> Any:
        """从外部存储引用恢复原始内容。"""
        if not isinstance(content, dict):
            return content

        if content.get("storage") != "external":
            return content

        relative_path = content.get("path", "")
        full_path = os.path.join(self.session_dir, relative_path)
        if not os.path.exists(full_path):
            return f"[外部内容缺失: {relative_path}]"

        try:
            with open(full_path, "r", encoding="utf-8") as file:
                return file.read()
        except OSError as exc:
            return f"[外部内容读取失败: {relative_path}, 错误: {exc}]"

    def _deserialize_message(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """反序列化单条消息。"""
        restored = copy.deepcopy(payload)
        restored["content"] = self._restore_content(restored.get("content", ""))
        return restored

    def _build_metadata(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """生成当前会话快照，供 `/resume` 和统计信息使用。"""
        return {
            "version": 1,
            "session_id": self.session_id,
            "created_at": self._created_at,
            "updated_at": time.time(),
            "message_count": len(messages),
            "input_tokens": state.total_input_tokens,
            "output_tokens": state.total_output_tokens,
            "total_cost_usd": state.total_cost_usd,
            "last_context_tokens": state.last_context_tokens,
            "model": state.model,
            "cwd": state.cwd,
            "workspace_root": state.workspace_root,
            "last_processed_msg_index": state.last_processed_msg_index,
            "micro_compact_count": state.micro_compact_count,
            "auto_compact_count": state.auto_compact_count,
            "summary_exists": state.auto_compact_count > 0,
        }

    async def _flush_lines(self, messages: List[Dict[str, Any]]) -> None:
        """把排队的 JSONL 行和最新 meta 一次性刷盘。"""
        async with self._lock:
            if self._pending_lines:
                os.makedirs(self.session_dir, exist_ok=True)
                with open(self.transcript_path, "a", encoding="utf-8") as file:
                    file.write("".join(self._pending_lines))
                self._pending_lines.clear()

            metadata = self._build_metadata(messages)
            with open(self.meta_path, "w", encoding="utf-8") as file:
                json.dump(metadata, file, ensure_ascii=False, indent=2)

    async def _delayed_flush(self, messages: List[Dict[str, Any]]) -> None:
        """用一个很短的延时合并短时间内的多次写入。"""
        try:
            await asyncio.sleep(FLUSH_DELAY_SECONDS)
            await self._flush_lines(messages)
        finally:
            self._flush_task = None

    def _schedule_flush(self, messages: List[Dict[str, Any]]) -> None:
        """确保同一时间只有一个延迟刷盘任务。"""
        if self._flush_task is not None and not self._flush_task.done():
            return
        self._flush_task = asyncio.create_task(self._delayed_flush(messages))

    async def append_message(self, message: Dict[str, Any], messages: List[Dict[str, Any]]) -> None:
        """追加一条消息事件。"""
        event = {
            "type": "message",
            "version": 1,
            "session_id": self.session_id,
            "timestamp": time.time(),
            "message": self._serialize_message(message),
        }
        self._pending_lines.append(_safe_json_dump(event) + "\n")
        self._schedule_flush(messages)

    async def append_snapshot(self, messages: List[Dict[str, Any]], reason: str) -> None:
        """在压缩或清空时，把当前消息快照写成一个替换事件。"""
        event = {
            "type": "rewrite",
            "version": 1,
            "session_id": self.session_id,
            "timestamp": time.time(),
            "reason": reason,
            "messages": [self._serialize_message(message) for message in messages],
        }
        self._pending_lines.append(_safe_json_dump(event) + "\n")
        self._schedule_flush(messages)

    async def flush_now(self, messages: List[Dict[str, Any]]) -> None:
        """立即刷盘，通常在退出、切换会话前调用。"""
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self._flush_lines(messages)

    async def close(self, messages: List[Dict[str, Any]]) -> None:
        """退出程序前的收尾动作。"""
        await self.flush_now(messages)

    @staticmethod
    def read_meta(session_id: str) -> Dict[str, Any]:
        """读取指定会话的 meta 文件。"""
        meta_path = os.path.join(SESSION_DIR, session_id, META_FILE)
        if not os.path.exists(meta_path):
            return {}

        try:
            with open(meta_path, "r", encoding="utf-8") as file:
                return json.load(file)
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def list_sessions(limit: int = 20) -> List[Dict[str, Any]]:
        """扫描会话目录，供 `/resume` 展示最近历史会话。"""
        if not os.path.isdir(SESSION_DIR):
            return []

        sessions: List[Dict[str, Any]] = []
        for session_id in os.listdir(SESSION_DIR):
            session_dir = os.path.join(SESSION_DIR, session_id)
            if not os.path.isdir(session_dir):
                continue

            meta = SessionStore.read_meta(session_id)
            if not meta:
                transcript_path = os.path.join(session_dir, TRANSCRIPT_FILE)
                updated_at = os.path.getmtime(transcript_path) if os.path.exists(transcript_path) else 0
                meta = {
                    "session_id": session_id,
                    "updated_at": updated_at,
                    "message_count": 0,
                    "model": "",
                }

            sessions.append(meta)

        sessions.sort(key=lambda item: float(item.get("updated_at", 0)), reverse=True)
        return sessions[:limit]

    @staticmethod
    def load_messages(session_id: str) -> List[Dict[str, Any]]:
        """从 JSONL 重建内存消息数组。"""
        transcript_path = os.path.join(SESSION_DIR, session_id, TRANSCRIPT_FILE)
        if not os.path.exists(transcript_path):
            return []

        store = SessionStore(session_id)
        restored_messages: List[Dict[str, Any]] = []
        with open(transcript_path, "r", encoding="utf-8") as file:
            for raw_line in file:
                line = raw_line.strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type")
                if event_type == "message":
                    payload = event.get("message", {})
                    restored_messages.append(store._deserialize_message(payload))
                elif event_type == "rewrite":
                    payload_list = event.get("messages", [])
                    restored_messages = [
                        store._deserialize_message(payload) for payload in payload_list
                    ]

        return restored_messages
