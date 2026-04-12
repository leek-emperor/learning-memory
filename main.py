"""learning-memory —— 从零实现 Claude Code 的记忆系统（极简版）

用法:
  uv run main.py

命令:
  /help    - 显示帮助
  /exit    - 退出
  /model   - 切换模型
  /tools   - 列出可用工具
  /stats   - 显示会话统计
  /clear   - 清空对话历史
"""
import asyncio
import os
import uuid
import time
from datetime import datetime

from config import OPENAI_API_MODEL, SESSION_DIR, MEMORY_DIR
from micro_compact import apply_micro_compact
from token_counter import analyze_context, count_messages_tokens, format_context_report
from session import SessionStore, add_command_history, setup_command_history
from state import state
from loop import chat_loop
from tools import registry
from file_tools import register_file_tools
from search import register_search_tool
from config import MAX_CONTEXT_TOKENS


def print_banner():
    print()
    print("  ╔══════════════════════════════════════╗")
    print("  ║   learning-memory v0.1               ║")
    print("  ║   从零实现 Claude Code 记忆系统         ║")
    print("  ╚══════════════════════════════════════╝")
    print()
    print(f"  模型: {state.model}")
    print(f"  工具: {', '.join(registry.list_tools())}")
    print(f"  启动目录: {os.path.abspath(state.cwd)}")
    print(f"  工作区: {os.path.abspath(state.workspace_root)}")
    print(f"  会话 ID: {state.session_id}")
    print()
    print("  输入消息开始对话，/help 查看命令")
    print()


def _format_timestamp(timestamp: float) -> str:
    """把时间戳格式化成易读的本地时间。"""
    if not timestamp:
        return "-"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def print_stats(messages: list[dict]):
    """打印会话统计信息"""
    analysis = analyze_context(messages, state.model)
    print()
    print(f"  💰 累计成本: ${state.total_cost_usd:.4f}")
    print(f"  📊 输入 token: {state.total_input_tokens:,}")
    print(f"  📊 输出 token: {state.total_output_tokens:,}")
    print(f"  🧠 当前上下文: {analysis['total_tokens']:,} / {MAX_CONTEXT_TOKENS:,}")
    print(f"  🗜  微压缩次数: {state.micro_compact_count}")
    print(f"  📝 自动压缩次数: {state.auto_compact_count}")
    elapsed = time.time() - state.start_time
    print(f"  ⏱  会话时长: {elapsed:.0f}s")
    print()


def _print_resume_candidates(sessions: list[dict]) -> None:
    """输出 `/resume` 候选列表。"""
    print("\n  可恢复的历史会话:")
    for index, item in enumerate(sessions, 1):
        print(
            f"    {index}. {item.get('session_id', '-')}"
            f" | {_format_timestamp(float(item.get('updated_at', 0)))}"
            f" | {item.get('message_count', 0)} 条消息"
            f" | {item.get('model', '-')}"
        )
    print()


def _restore_state_from_meta(meta: dict) -> None:
    """把 meta 快照恢复回运行时状态。"""
    if meta.get("model"):
        state.model = meta["model"]

    state.restore_usage(
        meta.get("input_tokens", 0),
        meta.get("output_tokens", 0),
        meta.get("total_cost_usd", 0.0),
    )
    state.set_last_context_tokens(meta.get("last_context_tokens", 0))
    state.last_processed_msg_index = meta.get("last_processed_msg_index", 0)
    state.restore_micro_compact_count(meta.get("micro_compact_count", 0))
    state.restore_auto_compact_count(meta.get("auto_compact_count", 0))
    state.reset_auto_compact_failures()

    restored_cwd = meta.get("cwd", "")
    restored_workspace = meta.get("workspace_root", "")
    if restored_cwd and os.path.isdir(restored_cwd):
        state.cwd = restored_cwd
    if restored_workspace and os.path.isdir(restored_workspace):
        state.workspace_root = restored_workspace


async def handle_command(
    cmd: str,
    messages: list[dict],
    session_store: SessionStore,
) -> tuple[bool, list[dict], SessionStore]:
    """处理斜杠命令。返回 handled / messages / session_store。"""
    cmd = cmd.strip()

    if cmd == "/exit" or cmd == "/quit":
        print("\n  再见！\n")
        return True, messages, session_store

    elif cmd == "/help":
        print("""
  可用命令:
    /help    - 显示帮助
    /exit    - 退出
    /model   - 切换模型（gpt-4o-mini / gpt-4o / gpt-3.5-turbo）
    /tools   - 列出可用工具
    /stats   - 显示会话统计（token、成本）
    /clear   - 清空对话历史
    /compact - 手动触发微压缩
    /resume  - 恢复历史会话
    /remember - 记忆管理（第 3 篇实现）
""")
        return True, messages, session_store

    elif cmd == "/model":
        print(f"\n  当前模型: {state.model}")
        print("  可选: gpt-4o-mini, gpt-4o, gpt-3.5-turbo")
        new_model = input("  切换到: ").strip()
        if new_model:
            state.model = new_model
            print(f"  ✅ 已切换到 {state.model}\n")
        return True, messages, session_store

    elif cmd == "/tools":
        tools = registry.list_tools()
        print(f"\n  已注册 {len(tools)} 个工具:")
        for t in tools:
            print(f"    - {t}")
        print()
        return True, messages, session_store

    elif cmd == "/stats":
        print_stats(messages)
        return True, messages, session_store

    elif cmd == "/clear":
        messages.clear()
        await session_store.append_snapshot(messages, "manual_clear")
        print("\n  ✅ 对话历史已清空\n")
        return True, messages, session_store

    elif cmd == "/compact":
        compact_result = apply_micro_compact(
            messages,
            compactable_tools=registry.get_compactable_tools(),
            model=state.model,
        )
        if compact_result["changed"]:
            state.increment_micro_compact_count()
            await session_store.append_snapshot(messages, "manual_micro_compact")
            print(
                f"\n  ✅ 已微压缩 {compact_result['replaced_count']} 条旧工具结果，"
                f"释放 ~{compact_result['freed_tokens']} tokens\n"
            )
        else:
            print("\n  ℹ️ 当前没有可压缩的旧工具结果\n")
        return True, messages, session_store

    elif cmd == "/resume":
        candidates = [
            item for item in SessionStore.list_sessions()
            if item.get("session_id") != state.session_id
        ]
        if not candidates:
            print("\n  ℹ️ 没有可恢复的历史会话\n")
            return True, messages, session_store

        _print_resume_candidates(candidates)
        choice = input("  输入序号恢复（回车取消）: ").strip()
        if not choice:
            print()
            return True, messages, session_store

        try:
            selected = candidates[int(choice) - 1]
        except (ValueError, IndexError):
            print("\n  ❌ 输入无效\n")
            return True, messages, session_store

        await session_store.flush_now(messages)
        selected_session_id = selected["session_id"]
        restored_messages = SessionStore.load_messages(selected_session_id)
        restored_meta = SessionStore.read_meta(selected_session_id)

        state.session_id = selected_session_id
        _restore_state_from_meta(restored_meta)
        session_store = SessionStore(selected_session_id)

        print(
            f"\n  ✅ 已恢复会话 {selected_session_id}"
            f"（{len(restored_messages)} 条消息，更新时间 {_format_timestamp(float(restored_meta.get('updated_at', 0)))}）\n"
        )
        return True, restored_messages, session_store

    elif cmd == "/remember":
        print("\n  ⏳ /remember 将在第 3 篇实现\n")
        return True, messages, session_store

    return False, messages, session_store


async def main():
    # 确保目录存在
    os.makedirs(SESSION_DIR, exist_ok=True)
    os.makedirs(MEMORY_DIR, exist_ok=True)

    # 初始化状态，直接使用 `.env` 中配置的模型
    state.model = OPENAI_API_MODEL
    state.cwd = os.getcwd()
    # 文件工具默认只允许访问 `启动目录/workspace`。
    state.workspace_root = os.path.join(state.cwd, "workspace")
    os.makedirs(state.workspace_root, exist_ok=True)
    state.session_id = str(uuid.uuid4())[:8]
    setup_command_history()

    # 注册工具
    register_file_tools(registry)
    register_search_tool(registry)

    # 获取工具定义和处理器
    tools = registry.get_openai_tools()
    handlers = registry.get_handlers()

    # 打印启动信息
    print_banner()
    history_sessions = [
        item for item in SessionStore.list_sessions()
        if item.get("session_id") != state.session_id
    ]
    if history_sessions:
        print(f"  检测到 {len(history_sessions)} 个历史会话，可使用 /resume 恢复")
        print()

    # 初始化会话存储和对话历史
    session_store = SessionStore(state.session_id)
    messages: list[dict] = []

    # 主循环
    while True:
        try:
            user_input = input("  你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\n  再见！\n")
            break

        if not user_input:
            continue

        add_command_history(user_input)

        # 处理斜杠命令
        if user_input.startswith("/"):
            handled, messages, session_store = await handle_command(
                user_input,
                messages,
                session_store,
            )
            if handled:
                if user_input in {"/exit", "/quit"}:
                    break
                continue

        # 更新交互时间
        state.touch_interaction()

        # 添加用户消息
        user_message = {"role": "user", "content": user_input}
        messages.append(user_message)
        await session_store.append_message(user_message, messages)

        # 显示当前上下文与占比
        state.set_last_context_tokens(count_messages_tokens(messages, state.model))
        print(format_context_report(analyze_context(messages, state.model)))
        print()

        # 调用对话循环
        try:
            messages = await chat_loop(
                messages,
                tools,
                handlers,
                session_store=session_store,
                compactable_tools=registry.get_compactable_tools(),
            )
        except Exception as e:
            print(f"\n  ❌ 错误: {e}\n")
            # 用户消息已经进入 transcript，这里保留现场方便排查。
            continue

        # 打印助手回复
        if messages and messages[-1]["role"] == "assistant":
            print(f"\n  Claude> {messages[-1]['content']}\n")
            print(format_context_report(analyze_context(messages, state.model)))
            print()

    await session_store.close(messages)


if __name__ == "__main__":
    asyncio.run(main())
