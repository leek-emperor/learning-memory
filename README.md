# learning-memory

<br />


一个从零实现的极简 AI Coding Agent 骨架，目标是先搭出带工具调用的 LLM Loop，再在后续版本里逐步加上短期记忆、长期记忆和上下文压缩能力。

## 教程文章

- [【从零实现 Claude Code 记忆系统】基础搭建： LLM Loop + Tool Use（一）](https://mp.weixin.qq.com/s/VZ8SWmA4K_r-qGZ8SfXjVA)
- [【从零实现 Claude Code 记忆系统】短期记忆：会话持久化与上下文压缩（二）](https://mp.weixin.qq.com/s/TygKV5G8VzDN83S0hHM9kg)
- [【从零实现 Claude Code 记忆系统】长期记忆：项目指令与自动记忆（三）](https://mp.weixin.qq.com/s/IMdj2pnVt3e9Nbv4J3QQtQ)


当前版本先解决最核心的一件事：

```text
用户输入 -> 模型推理 -> 决定是否调用工具 -> 执行工具 -> 回填结果 -> 继续推理 -> 输出答案
```

如果你把它类比成前端应用：

- `main.py` 像页面入口，负责输入输出
- `loop.py` 像 orchestrator，负责把模型调用和工具调用串起来
- `tools.py` 像插件注册中心
- `file_tools.py` / `search.py` 像具体服务层
- `state.py` 像极简全局 store

## 当前能力

当前版本已经支持：

- 与模型进行多轮对话
- 让模型按需调用工具
- 读取文件 `readFile`
- 写入文件 `writeFile`
- 列出目录 `listFiles`
- 搜索网页 `webSearch`
- 统计 token 和成本

还没有做的内容：

- 短期记忆持久化
- 上下文压缩
- 长期记忆
- 权限确认

## 项目结构

```text
learning-memory/
├── main.py
├── config.py
├── state.py
├── loop.py
├── tools.py
├── file_tools.py
├── search.py
├── pyproject.toml
├── .env.example
└── README.md
```

各模块职责：

- `main.py`: CLI 入口，负责读取用户输入和处理斜杠命令
- `loop.py`: 核心 LLM Loop，负责模型调用、工具执行和消息回填
- `tools.py`: 工具注册表，统一维护工具 schema 和 handler
- `file_tools.py`: 文件相关工具实现
- `search.py`: Web Search 工具实现，支持 Tavily / SearXNG
- `state.py`: 会话状态、token 和成本统计
- `config.py`: 从 `.env` 加载配置，并计算数据目录

## 核心流程

整个项目最核心的是 `chat_loop()`：

```text
1. 接收用户消息
2. 拼装 OpenAI API 所需消息
3. 调用模型
4. 检查是否返回 tool_calls
5. 如果有工具调用：
   - 找到对应 handler
   - 执行工具
   - 把结果作为 tool 消息追加回上下文
   - 继续下一轮推理
6. 如果没有工具调用：
   - 直接返回最终回复
```

最小骨架如下：

```python
async def chat_loop(messages: list[dict], tools: list[dict], tool_handlers: dict):
    for _ in range(20):
        api_messages = build_api_messages(messages)
        response = client.chat.completions.create(
            model=state.model,
            messages=api_messages,
            tools=tools or None,
            max_tokens=4096,
        )

        assistant_msg = response.choices[0].message
        tool_calls = assistant_msg.tool_calls

        if not tool_calls:
            messages.append({
                "role": "assistant",
                "content": assistant_msg.content or "",
            })
            return messages

        messages.append({
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
        })

        for tc in tool_calls:
            fn_name = tc.function.name
            fn_args = json.loads(tc.function.arguments or "{}")
            handler = tool_handlers.get(fn_name)
            result = await handler(fn_args) if handler else f"错误: 未知工具 {fn_name}"

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": str(result),
            })

    messages.append({"role": "assistant", "content": "[已达到最大工具调用轮次，停止执行]"})
    return messages
```

## 工具系统

工具不是写死在 `loop.py` 里的，而是统一注册到 `ToolRegistry`：

```python
class ToolRegistry:
    def register(self, name: str, description: str, parameters: dict, handler, compactable: bool = False):
        ...

    def get_openai_tools(self) -> list[dict]:
        ...

    def get_handlers(self) -> dict[str, callable]:
        ...
```

这样做的好处是：

- 新增工具时不需要修改核心循环
- 可以自动生成 OpenAI function calling 所需的 schema
- 后面做压缩时，可以通过 `compactable` 标记哪些工具结果可清理

当前注册的工具有：

- `readFile`
- `writeFile`
- `listFiles`
- `webSearch`

## 运行要求

- macOS / Linux / Windows 都可以，当前开发环境是 macOS
- Python `>= 3.13`
- 推荐使用 `uv`

## 快速开始

### 1. 准备配置

先复制配置文件：

```bash
cp .env.example .env
```

然后填写 `.env`：

```bash
OPENAI_API_KEY=your-openai-compatible-api-key
OPENAI_API_BASE=https://your-openai-compatible-base-url
OPENAI_API_MODEL=your-model-name
MAX_CONTEXT_TOKENS=128000

SEARCH_BACKEND=tavily
TAVILY_API_KEY=your-tavily-api-key
SEARXNG_URL=http://localhost:8888
```

说明：

- `OPENAI_API_BASE` 支持 OpenAI 兼容接口
- `SEARCH_BACKEND` 支持 `tavily` 或 `searxng`
- 如果你用 `tavily`，需要配置 `TAVILY_API_KEY`
- 如果你用 `searxng`，需要先准备自己的 SearXNG 服务地址

### 2. 安装依赖

推荐直接用 `uv`：

```bash
uv sync
```

### 3. 启动

```bash
uv run main.py
```

## 交互示例

```text
╔══════════════════════════════════════╗
║   learning-memory v0.1              ║
║   从零实现 Claude Code 记忆系统      ║
╚══════════════════════════════════════╝

模型: gpt-4o-mini
工具: readFile, writeFile, listFiles, webSearch
启动目录: /path/to/learning-memory
工作区: /path/to/learning-memory/workspace

输入消息开始对话，/help 查看命令

你> 当前文件夹下有什么文件
[状态机] idle → running
🔧 调用工具: listFiles({"path": ".", "pattern": "*"})
[状态机] running → idle

Claude> 当前文件夹下的文件和目录如下：
notes/
todo.txt
```

## 支持的命令

当前 CLI 支持这些斜杠命令：

- `/help`: 查看帮助
- `/exit`: 退出程序
- `/model`: 切换模型
- `/tools`: 查看已注册工具
- `/stats`: 查看 token / 成本统计
- `/clear`: 清空当前对话历史

已经预留但暂未实现：

- `/compact`
- `/resume`
- `/remember`

## 搜索后端说明

### Tavily

适合快速开始：

- 接入简单
- 结果通常比较稳定
- 需要 API Key

官网：<https://www.tavily.com/>

### SearXNG

适合自建和长期使用：

- 免费
- 可自部署
- 不依赖第三方配额

如果本地快速启动一个 SearXNG，可以参考类似方式：

```bash
docker run -d -p 8888:8080 searxng/searxng
```

## 状态和数据目录

`config.py` 会优先把数据目录放在：

```text
~/.learning-memory/
```

如果当前环境没有用户目录写权限，会回退到项目目录下的：

```text
.learning-memory/
```

当前会维护的状态包括：

- `session_id`
- `cwd`
- `workspace_root`
- `model`
- `phase`
- `total_input_tokens`
- `total_output_tokens`
- `total_cost_usd`

## 文件工作区

为了避免模型直接读写教程源码目录，文件工具默认不会把启动目录当成可操作根目录。

程序启动后会自动创建：

```text
workspace/
```

并把它作为文件工具的默认工作区。也就是说：

- `readFile("a.txt")` 实际读取的是 `workspace/a.txt`
- `writeFile("notes/todo.md")` 实际写入的是 `workspace/notes/todo.md`
- 如果路径试图跳出 `workspace/`，会被安全检查拦截

这样“程序从哪里启动”和“模型能操作哪里”就是两个不同概念，后面继续加会话恢复和记忆系统时也更清晰。

## 下一步会做什么

这个仓库当前重点是先把“带工具调用的 LLM Loop”打稳。后续可以继续往下扩展：

1. 会话持久化
2. 上下文压缩
3. 长期记忆提取和检索
4. 权限确认和人工介入

## 为什么这个项目适合学习

它不是一个“大而全”的 agent 框架，而是一个足够小、但关键骨架完整的练手项目。

你可以从这里学到：

- 工具调用型 Agent 的最小闭环怎么搭
- 消息格式为什么要分“内部格式”和“API 格式”
- 为什么工具注册要声明式
- 文件工具为什么一定要做路径安全检查
- Web Search 这种能力如何以统一接口抽象出来

如果你正在学 AI Agent，这个项目很适合作为第一步。
