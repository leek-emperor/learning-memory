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

from config import OPENAI_API_MODEL, SESSION_DIR, MEMORY_DIR
from state import state
from loop import chat_loop, estimate_total_tokens
from tools import registry
from file_tools import register_file_tools
from search import register_search_tool


def print_banner():
    print()
    print("  ╔══════════════════════════════════════╗")
    print("  ║   learning-memory v0.1              ║")
    print("  ║   从零实现 Claude Code 记忆系统      ║")
    print("  ╚══════════════════════════════════════╝")
    print()
    print(f"  模型: {state.model}")
    print(f"  工具: {', '.join(registry.list_tools())}")
    print(f"  工作目录: {os.path.abspath(state.cwd)}")
    print()
    print("  输入消息开始对话，/help 查看命令")
    print()


def print_stats():
    """打印会话统计信息"""
    print()
    print(f"  💰 累计成本: ${state.total_cost_usd:.4f}")
    print(f"  📊 输入 token: {state.total_input_tokens:,}")
    print(f"  📊 输出 token: {state.total_output_tokens:,}")
    elapsed = time.time() - state.start_time
    print(f"  ⏱  会话时长: {elapsed:.0f}s")
    print()


def handle_command(cmd: str, messages: list[dict]) -> bool:
    """处理斜杠命令。返回 True 表示已处理（不发给 LLM）。"""
    cmd = cmd.strip()

    if cmd == "/exit" or cmd == "/quit":
        print("\n  再见！\n")
        return True

    elif cmd == "/help":
        print("""
  可用命令:
    /help    - 显示帮助
    /exit    - 退出
    /model   - 切换模型（gpt-4o-mini / gpt-4o / gpt-3.5-turbo）
    /tools   - 列出可用工具
    /stats   - 显示会话统计（token、成本）
    /clear   - 清空对话历史
    /compact - 手动压缩（第 2 篇实现）
    /resume  - 恢复历史会话（第 2 篇实现）
    /remember - 记忆管理（第 3 篇实现）
""")
        return True

    elif cmd == "/model":
        print(f"\n  当前模型: {state.model}")
        print("  可选: gpt-4o-mini, gpt-4o, gpt-3.5-turbo")
        new_model = input("  切换到: ").strip()
        if new_model:
            state.model = new_model
            print(f"  ✅ 已切换到 {state.model}\n")
        return True

    elif cmd == "/tools":
        tools = registry.list_tools()
        print(f"\n  已注册 {len(tools)} 个工具:")
        for t in tools:
            print(f"    - {t}")
        print()
        return True

    elif cmd == "/stats":
        print_stats()
        return True

    elif cmd == "/clear":
        messages.clear()
        print("\n  ✅ 对话历史已清空\n")
        return True

    elif cmd == "/compact":
        print("\n  ⏳ /compact 将在第 2 篇实现\n")
        return True

    elif cmd == "/resume":
        print("\n  ⏳ /resume 将在第 2 篇实现\n")
        return True

    elif cmd == "/remember":
        print("\n  ⏳ /remember 将在第 3 篇实现\n")
        return True

    return False


async def main():
    # 确保目录存在
    os.makedirs(SESSION_DIR, exist_ok=True)
    os.makedirs(MEMORY_DIR, exist_ok=True)

    # 初始化状态，直接使用 `.env` 中配置的火山引擎 endpoint ID。
    state.model = OPENAI_API_MODEL
    state.cwd = os.getcwd()
    state.session_id = str(uuid.uuid4())[:8]

    # 注册工具
    register_file_tools(registry)
    register_search_tool(registry)

    # 获取工具定义和处理器
    tools = registry.get_openai_tools()
    handlers = registry.get_handlers()

    # 打印启动信息
    print_banner()

    # 对话历史（后续第 2 篇会持久化到 JSONL）
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

        # 处理斜杠命令
        if user_input.startswith("/"):
            if handle_command(user_input, messages):
                continue

        # 更新交互时间
        state.touch_interaction()

        # 添加用户消息
        messages.append({"role": "user", "content": user_input})

        # 显示 token 预估
        tokens = estimate_total_tokens(messages)
        print(f"  [上下文: ~{tokens:,} tokens]\n")

        # 调用对话循环
        try:
            messages = await chat_loop(messages, tools, handlers)
        except Exception as e:
            print(f"\n  ❌ 错误: {e}\n")
            # 移除最后一条用户消息（避免重复）
            if messages and messages[-1]["role"] == "user":
                messages.pop()
            continue

        # 打印助手回复
        if messages and messages[-1]["role"] == "assistant":
            print(f"\n  Claude> {messages[-1]['content']}\n")


if __name__ == "__main__":
    asyncio.run(main())
