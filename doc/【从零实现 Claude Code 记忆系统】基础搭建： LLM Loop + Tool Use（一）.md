<br />

> 从零实现 Claude Code 记忆系统 · 实践篇第 1 篇
>
> 这一篇先不做“记忆”，而是先把整个系统最重要的骨架搭起来：一个能调用工具的 LLM Loop。

***

## 本篇要解决什么问题

一个像 Claude Code、Cursor 这样的助手，最核心的不是聊天，而是下面这条链路：

1. 接收用户输入
2. 判断要不要调用工具
3. 执行工具，把结果塞回上下文
4. 继续推理，直到输出最终答案

这套机制，就是 LLM Loop。

这一篇的目标很简单：先实现一个最小可用版本，让它具备三种基础能力：

- 对话
- 读写文件
- 搜索网页

最终产物是一个 CLI 程序。你运行 `uv run main.py` 后，就能像使用一个带工具能力的 AI 助手一样和它交互。

项目地址：<https://github.com/leek-emperor/agent-browser>

***

## 一、先看整体架构

这一版故意拆成几个小模块，目标不是“功能很多”，而是“边界清楚，后续好扩展”。

```text
┌──────────────────────────────────────────────────────────┐
│  main.py（CLI 入口）                                     │
│  ├─ 读取用户输入                                         │
│  ├─ 处理斜杠命令 (/help /exit /model /stats ...)         │
│  └─ 调用 chat_loop()                                    │
│                                                          │
│  loop.py（核心循环）                                     │
│  ├─ 构建 API 消息                                       │
│  ├─ 调用 OpenAI 兼容接口                                │
│  ├─ 如果有 tool_calls → 执行工具 → 继续循环              │
│  └─ 如果没有 tool_calls → 返回最终回复                    │
│                                                          │
│  tools.py（工具注册表）                                  │
│  ├─ 声明式注册工具                                      │
│  ├─ 生成 OpenAI function calling 所需结构               │
│  └─ 提供 compactable 标记，为后续压缩预留接口           │
│                                                          │
│  file_tools.py（文件工具）                               │
│  ├─ readFile                                            │
│  ├─ writeFile                                           │
│  └─ listFiles                                           │
│                                                          │
│  search.py（Web Search）                                 │
│  ├─ Tavily 后端                                         │
│  ├─ SearXNG 后端                                        │
│  └─ 统一暴露 search_web()                               │
│                                                          │
│  state.py（会话状态）                                    │
│  ├─ idle / running / requires_action 状态机             │
│  ├─ token / cost 累计                                   │
│  └─ 全局单例状态                                        │
│                                                          │
│  config.py（全局配置）                                   │
│  └─ 统一维护模型、搜索后端、存储目录等配置                │
└──────────────────────────────────────────────────────────┘
```

如果你是前端开发，可以这样理解：

- `main.py` 像页面入口，负责交互
- `loop.py` 像 orchestrator，负责串联整个流程
- `tools.py` 像插件注册中心
- `file_tools.py` / `search.py` 像具体服务层
- `state.py` 像极简全局 store
- `config.py` 像集中配置模块

***

## 二、主流程从哪里开始

程序入口在 `main.py`，它只做几件事：

1. 初始化目录和状态
2. 注册工具
3. 进入命令行输入循环
4. 普通消息交给 `chat_loop()`
5. 斜杠命令自己处理

这个拆分的好处是：CLI 层只负责“收发消息”，至于要不要调工具、怎么调，都交给 `loop.py`。

### 2.1 `main.py` 负责什么

先看启动主流程的核心代码：

```python
async def main():
    os.makedirs(SESSION_DIR, exist_ok=True)
    os.makedirs(MEMORY_DIR, exist_ok=True)

    # 初始化运行时状态
    state.model = OPENAI_API_MODEL
    state.cwd = os.getcwd()
    state.session_id = str(uuid.uuid4())[:8]

    # 注册工具
    register_file_tools(registry)
    register_search_tool(registry)

    tools = registry.get_openai_tools()
    handlers = registry.get_handlers()
    messages: list[dict] = []

    while True:
        user_input = input("  你> ").strip()
        if not user_input:
            continue

        # 斜杠命令由 CLI 自己处理，不发给 LLM
        if user_input.startswith("/") and handle_command(user_input, messages):
            continue

        state.touch_interaction()
        messages.append({"role": "user", "content": user_input})

        # 进入 LLM Loop
        messages = await chat_loop(messages, tools, handlers)

        if messages and messages[-1]["role"] == "assistant":
            print(f"\n  Claude> {messages[-1]['content']}\n")
```

这里最关键的点只有两个：

- `main.py` 不处理工具调用细节，它只负责把输入交给 `chat_loop()`
- 工具在启动时统一注册，运行时只传 `tools` 和 `handlers`

### 2.2 当前有哪些斜杠命令

当前已经实现：

- `/help`
- `/exit`
- `/model`
- `/tools`
- `/stats`
- `/clear`

另外还预留了：

- `/compact`
- `/resume`
- `/remember`

这些命令暂时只输出提示，但接口已经留好了，后面做短期记忆和长期记忆时可以直接接上。

***

## 三、核心：`chat_loop()` 到底在做什么

整个系统的“心脏”在 `loop.py`。它负责一件事：不停问模型“接下来该回答，还是该调工具”。

```text
用户输入
  ↓
构建 API 消息
  ↓
调用模型
  ↓
模型是否返回 tool_calls？
  ├─ 否：直接结束，输出文本回复
  └─ 是：执行工具，把结果追加回消息，再继续下一轮
```

### 3.1 为什么要有“内部消息格式”

内部先统一保存成这种结构：

```python
{"role": "user", "content": "..."}
{"role": "assistant", "content": "..."}
{"role": "tool", "tool_call_id": "...", "content": "..."}
```

这样做的好处是内部格式稳定，后面做持久化、压缩、恢复时更方便。等到真正发请求时，再转换成 OpenAI API 需要的格式。

核心转换代码如下：

```python
def build_api_messages(messages: list[dict]) -> list[dict]:
    api_msgs = [{"role": "system", "content": SYSTEM_PROMPT}]

    for msg in messages:
        role = msg["role"]
        content = msg.get("content", "")

        if role == "tool":
            api_msgs.append({
                "role": "tool",
                "tool_call_id": msg["tool_call_id"],
                "content": str(content),
            })
        elif role == "assistant" and "tool_calls" in msg:
            api_msgs.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": msg["tool_calls"],
            })
        else:
            api_msgs.append({"role": role, "content": str(content)})

    return api_msgs
```

### 3.2 一次工具调用是怎么完成的

假设用户输入：

```text
帮我看看 main.py 的内容
```

大概会经历下面这几步：

```text
① messages 里追加 user 消息
② build_api_messages() 转成接口消息
③ 调用模型
④ 模型返回 tool_calls: readFile({"path": "main.py"})
⑤ 执行 readFile handler
⑥ 把工具结果作为 tool 消息塞回 messages
⑦ 再次调用模型
⑧ 模型基于工具结果给出最终回答
```

真正最关键的，就是下面这段循环：

```python
async def chat_loop(messages: list[dict], tools: list[dict], tool_handlers: dict):
    state.set_phase(SessionPhase.RUNNING)

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

        # 没有工具调用，说明可以直接结束
        if not tool_calls:
            messages.append({
                "role": "assistant",
                "content": assistant_msg.content or "",
            })
            state.set_phase(SessionPhase.IDLE)
            return messages

        # 先把 assistant 的工具调用意图记下来
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

        # 再执行工具，把结果塞回上下文
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
    state.set_phase(SessionPhase.IDLE)
    return messages
```

如果你是前端开发，可以把它理解成一个“事件循环版副作用系统”：

- 用户输入像一次 action
- 模型决定要不要触发副作用
- 工具调用就是副作用执行
- 工具结果回流后，再触发下一轮推理

### 3.3 为什么要限制最大迭代次数

当前 `chat_loop()` 默认最多循环 20 次，这个保护很重要。否则模型如果反复读同一个文件，或者一直生成错误参数，就可能无限循环。

### 3.4 成本统计是怎么做的

每次模型调用结束后，会从 `response.usage` 里取 token 消耗，再更新到全局状态中：

```python
usage = response.usage
if usage:
    input_cost = (usage.prompt_tokens / 1_000_000) * 0.15
    output_cost = (usage.completion_tokens / 1_000_000) * 0.60
    state.accumulate_usage(
        usage.prompt_tokens,
        usage.completion_tokens,
        input_cost + output_cost,
    )
```

这样执行 `/stats` 时，就能看到 token、成本和会话时长。

***

## 四、为什么要做一个工具注册表

如果只有一个工具，直接在 `chat_loop()` 里 `if / elif` 也能写。但工具一多，这种写法会迅速失控。

所以这里单独做了一个 `ToolRegistry`，把工具定义统一抽象成：

```text
name         工具名
description  工具描述
parameters   参数 JSON Schema
handler      实际执行函数
```

### 4.1 这个设计解决了什么问题

它主要解决三件事：

1. 工具注册是声明式的
2. OpenAI 工具结构可以自动生成
3. 新增工具时不用修改核心循环

核心代码如下：

```python
class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, dict] = {}
        self._order: list[str] = []

    def register(self, name: str, description: str, parameters: dict, handler, compactable: bool = False):
        self._tools[name] = {
            "description": description,
            "parameters": parameters,
            "handler": handler,
            "compactable": compactable,
        }
        if name not in self._order:
            self._order.append(name)

    def get_openai_tools(self) -> list[dict]:
        result = []
        for name in self._order:
            tool = self._tools[name]
            result.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool["description"],
                    "parameters": tool["parameters"],
                },
            })
        return result

    def get_handlers(self) -> dict[str, callable]:
        return {name: self._tools[name]["handler"] for name in self._order}
```

注册工具时就很简单：

```python
registry.register(...)
registry.register(...)
```

后面新增一个工具时，通常只需要两步：

1. 写 handler
2. 调用 `registry.register(...)`

### 4.2 `compactable` 是干什么的

`compactable=True` 这一篇还没真正用上，但它是为后面的“上下文压缩”预留的。

比如：

- 文件内容、搜索结果通常可以压缩
- 某些关键操作结果可能不应该清掉

所以这里先把这个标记留好，后面不用回头改工具层。

***

## 五、文件工具：先把最基础的能力补齐

对编程助手来说，最常用的基础能力就是：

- 读文件
- 写文件
- 列目录

所以 `file_tools.py` 先把这三件事做好。

### 5.1 `readFile`

`readFile` 最重要的不是“能读文件”，而是“只能安全地读工作目录里的文件”。

核心代码如下：

```python
async def read_file(args: dict) -> str:
    path = args.get("path", "")
    if not path:
        return "错误: 缺少 path 参数"

    # 支持相对路径，统一按当前工作目录解析
    if not os.path.isabs(path):
        path = os.path.join(state.cwd, path)

    real_path = os.path.realpath(path)
    real_cwd = os.path.realpath(state.cwd)

    # 路径安全检查：不允许读工作目录之外的文件
    if not real_path.startswith(real_cwd):
        return f"错误: 不允许读取工作目录之外的文件: {path}"

    if not os.path.isfile(real_path):
        return f"错误: 文件不存在: {path}"

    with open(real_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 限制返回大小，避免大文件塞爆上下文
    if len(content) > 30000:
        content = content[:30000] + "\n\n... [文件过大，已截断]"
    return content
```

这里最关键的两个点是：

- 相对路径统一解析到 `state.cwd`
- 用 `realpath` 做路径边界检查，防止越权访问

### 5.2 `writeFile`

`writeFile` 的逻辑和 `readFile` 类似：

- 支持相对路径
- 只能写工作目录内的文件
- 目录不存在时自动创建

这版实现故意保持简单，先把“安全边界”和“能用”做好。

### 5.3 `listFiles`

`listFiles` 用于列出目录内容，支持 glob，例如：

```text
*.py
**/*.md
src/**/*.ts
```

返回时会做三件事：

- 统一转成相对路径
- 目录名追加 `/`
- 最多返回 100 条

这样模型既容易理解，也不容易把上下文塞满。

***

## 六、Web Search：让助手拿到“最新信息”

如果没有联网能力，模型就只能依赖训练数据。为了让它能回答“最近发生了什么”，这里补了一个 `webSearch` 工具。

### 6.1 为什么同时支持 Tavily 和 SearXNG

这是一个典型的“统一接口，多后端实现”设计。

当前支持两种后端：

#### Tavily

官网：<https://www.tavily.com/>

优点：

- 接入简单
- 结果通常更稳定
- 自带 `answer` 摘要字段

缺点：

- 需要 API Key
- 免费额度有限

#### SearXNG

优点：

- 免费
- 可以自建
- 没有调用次数压力

缺点：

- 需要自己部署
- 结果质量依赖搜索源

### 6.2 统一接口的价值

不管底层是 Tavily 还是 SearXNG，对外都统一暴露成：

```python
async def search_web(query: str, max_results: int = 5) -> str
```

核心分发代码如下：

```python
async def search_web(query: str, max_results: int = 5) -> str:
    if SEARCH_BACKEND == "searxng":
        return await search_searxng(query, max_results)
    return await search_tavily(query, max_results)


async def web_search_handler(args: dict) -> str:
    query = args.get("query", "")
    if not query:
        return "错误: 缺少 query 参数"
    return await search_web(query)
```

这样：

- `chat_loop()` 不关心具体搜索后端
- `ToolRegistry` 不关心具体搜索后端
- 切换后端时只需要改 `.env` 里的 `SEARCH_BACKEND`

### 6.3 当前配置方式

搜索相关配置统一放在项目根目录的 `.env`：

- `SEARCH_BACKEND`
- `TAVILY_API_KEY`
- `SEARXNG_URL`

`config.py` 负责加载 `.env`、校验配置，并计算会话目录和记忆目录。

### 6.4 返回格式为什么是纯文本

搜索结果最后会整理成纯文本，而不是原始 JSON，例如：

```text
摘要: ...

1. 标题
   URL
   摘要

2. 标题
   URL
   摘要
```

原因很简单：工具结果最终还是给 LLM 读，整理成文本通常比直接塞原始 JSON 更容易被利用。

***

## 七、状态机：为什么这里要有一个全局 `state`

如果把运行时数据到处传，代码很快会变乱。所以这里单独抽了一个 `SessionState`，统一保存会话级状态。

### 7.1 当前维护了哪些状态

它主要保存：

- `session_id`
- `start_time`
- `last_interaction_time`
- `total_cost_usd`
- `total_input_tokens`
- `total_output_tokens`
- `phase`
- `cwd`
- `model`
- `last_processed_msg_index`

你可以把它理解成一个极简的全局 store。

### 7.2 三态模型为什么先留完整

当前状态机是：

```text
idle -> running -> requires_action
```

这一篇主要用到的是：

- `idle`
- `running`

`requires_action` 暂时还没真正参与流程，但后面做权限确认或人工介入时会用到。

### 7.3 为什么用单例

当前项目是单进程 CLI，状态天然只有一份，所以用单例足够直接：

- 不用层层传参
- CLI、Loop、Tools 都能访问统一状态
- 后续如果改成服务端，再改成 request-scoped state 也不迟

***

## 八、项目结构

结合当前代码，项目核心结构可以理解成这样：

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
├── README.md
├── .gitignore
├── .python-version
```

这里最重要的不是文件多少，而是边界比较清楚：

- CLI 入口只负责交互
- 核心循环只负责调度
- 工具注册表只负责定义工具
- 工具模块只负责具体动作
- 状态模块只负责保存运行态
- 配置模块只负责集中配置

***

## 八、如何运行当前版本

当前项目使用 `uv` 运行，依赖声明在 `pyproject.toml` 中。

### 8.1 启动方式

```bash
cd learning-memory
cp .env.example .env
uv run main.py
```

### 8.2 配置方式

当前版本推荐直接修改项目根目录的 `.env`，而不是手动 `export` 一堆环境变量。

例如：

```bash
OPENAI_API_BASE=...
OPENAI_API_MODEL=...
SEARCH_BACKEND=tavily  # 或 searxng
```

### 8.3 交互示例

下面是一段更贴近当前实现的交互流程：

```text


  ╔══════════════════════════════════════╗
  ║   learning-memory v0.1              ║
  ║   从零实现 Claude Code 记忆系统      ║
  ╚══════════════════════════════════════╝

  模型: gpt-4o-mini
  工具: readFile, writeFile, listFiles, webSearch
  工作目录: /Users/abc/learning-memory

  输入消息开始对话，/help 查看命令

  你> 你好
  [上下文: ~1 tokens]

  [状态机] idle → running
  [状态机] running → idle

  Claude> 你好！很高兴能帮到你。如果有编程相关的问题、代码需求或者技术疑问，都可以随时告诉我，我会尽力提供帮助😊！

  你> 搜索一下北京 今天的天气
  [上下文: ~21 tokens]

  [状态机] idle → running

  🔧 调用工具: webSearch({"query": "北京今天的天气"})
  [状态机] running → idle

  Claude> 北京今日天气情况如下（数据来源：百度天气、中央气象台，更新至4月6日12时）：
- **天气状况**：晴
- **气温范围**：8℃~22℃，当前实时气温约18.3℃
- **风力风向**：东南风3级（午后转为西南风微风）
- **湿度**：50%
- **日出日落**：日出05:43，日落18:48

体感温度舒适，建议穿着薄外套或长袖单衣，白天紫外线较强可适当做好防晒措施☀️

  你> 帮我看看当前目录有哪些 文件
  [上下文: ~587 tokens]

  [状态机] idle → running

  🔧 调用工具: listFiles({})
  [状态机] running → idle

  Claude> 当前目录下的文件和子目录如下：

📁 子目录：
- __pycache__/

📄 文件：
- README.md
- config.py
- file_tools.py
- loop.py
- main.py
- pyproject.toml
- search.py
- state.py
- tools.py
- uv.lock



```

***

## 九、和 Claude Code 思路的对应关系

虽然这个项目是极简实现，但保留了几项非常关键的设计思想。

| 本项目模块           | Claude Code 中的对应思路 | 当前保留了什么              |
| --------------- | ------------------ | -------------------- |
| `main.py`       | CLI 入口层            | 命令处理、输入输出、启动流程       |
| `loop.py`       | QueryEngine        | 工具调用循环、消息转换、最终回复收敛   |
| `tools.py`      | Tool.ts / tools.ts | 声明式工具注册、统一 schema 输出 |
| `file_tools.py` | 文件工具层              | 路径安全检查、返回大小限制        |
| `search.py`     | WebSearchTool      | 双后端统一接口              |
| `state.py`      | Bootstrap State    | 全局状态、状态机、成本统计        |
| `config.py`     | 配置管理层              | 模型、搜索、目录等集中配置        |

真正值得保留的，不是文件名相似，而是下面这些设计：

- 内部消息格式统一
- 工具声明式注册
- 核心循环不和具体工具耦合
- 状态和配置单独收口
- 为后续压缩和记忆预留接口

***

## 十、这篇代码为后续内容预留了什么

这一篇虽然只实现了最基础的 LLM Loop，但后续扩展点已经留好了。

### 给第 2 篇“短期记忆”预留的接口

- `messages`：后续可以做 JSONL 持久化
- `estimate_total_tokens()`：后续可以接更精确的 token 统计
- `compactable`：后续可以做消息压缩
- `/compact`、`/resume`：命令入口已经留出

### 给第 3 篇“长期记忆”预留的接口

- `MEMORY_DIR`：已经有独立目录
- `registry`：可以继续注册记忆相关工具
- `chat_loop()`：后续可以插入记忆提取或检索流程
- `last_processed_msg_index`：可以记录消息处理进度

也就是说，这一篇不是一次性 demo，而是后续两篇可以继续往上长的底座。

***

## 十一、这一篇最值得记住的事

如果你只记住一句话，我希望是这句：

> AI 编程助手的第一步，不是做“记忆”，而是先做一个稳定的 LLM Loop。

因为只有当下面这条链路先跑顺：

```text
用户输入 -> 模型判断 -> 调用工具 -> 返回结果 -> 再次推理 -> 输出答案
```

后面的记忆、压缩、恢复、权限控制，才有地方挂进去。

***

项目地址：<https://github.com/leek-emperor/agent-browser>

***

## 系列导航

| # | 篇名                                 | 状态   |
| - | ---------------------------------- | ---- |
| 1 | **构建带工具调用的 LLM Loop + Web Search** | ✅ 本篇 |
| 2 | 短期记忆：会话持久化与上下文压缩                   | 待创作  |
| 3 | 长期记忆：项目指令与自动记忆                     | 待创作  |

