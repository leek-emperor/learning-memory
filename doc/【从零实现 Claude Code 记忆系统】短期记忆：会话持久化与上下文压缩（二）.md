<br />

> 从零实现 Claude Code 记忆系统 · 实践篇第 2 篇
>
> 上一篇搭好了 LLM Loop 骨架，这一篇给它加上"短期记忆"——让对话不丢失、不超限。

***

## 本篇要解决什么问题

上一篇的程序有一个致命问题：**关掉就忘了**。

你告诉它"我叫 Leek Emperor"，它记住了。但关掉程序再打开，它什么都不记得。更严重的是，如果对话很长、工具调用很多，上下文窗口迟早会被撑爆。

这一篇要解决两件事：

1. **会话持久化**：对话写入磁盘，退出后可以恢复
2. **上下文压缩**：对话太长时自动清理，防止超限

最终效果：你可以聊几十轮、读十几个文件，程序会自动管理上下文空间。中途关掉再打开，用 `/resume` 接着聊。

项目地址：<https://github.com/leek-emperor/learning-memory>

***

## 一、先看改了什么

```
新增文件:
  session.py          会话持久化（JSONL 读写 + 恢复）
  token_counter.py    Token 计数 + 上下文分析
  micro_compact.py    微压缩
  auto_compact.py     自动压缩

修改文件:
  state.py            新增压缩统计、workspace_root 等字段
  loop.py             集成实时持久化 + 自动压缩触发
  main.py             集成 /resume、/compact、上下文报告
  file_tools.py       路径安全检查改用 workspace_root
```

***

## 二、会话持久化：让对话活到下次启动

### 2.1 核心思路

Claude Code 用 JSONL（每行一个 JSON 对象）记录会话日志。我们也用同样的方式：

```text
~/.learning-memory/sessions/<session-id>/
  ├── transcript.jsonl    ← 消息流（append-only）
  ├── meta.json           ← 会话快照（每次覆写）
  └── blobs/              ← 大内容外部存储
      ├── msg_000001.txt
      └── msg_000002.txt
```

为什么选 JSONL 而不是一个大的 JSON 文件？

- **追加写入**：不用每次重写整个文件，性能好
- **崩溃安全**：程序崩了最多丢最后几行，不会损坏整个文件
- **流式读取**：可以逐行解析，不需要一次性加载到内存

Claude Code 的 `transcript.ts` 就是这么干的。

### 2.2 两种事件类型

`transcript.jsonl` 里写的不只是消息，而是"事件"。当前有两种：

```python
# 普通消息事件（每产生一条消息就追加一行）
{"type": "message", "message": {"role": "user", "content": "你好"}, ...}

# 重写事件（压缩或清空时，把当前消息快照完整写下来）
{"type": "rewrite", "reason": "auto_micro_compact", "messages": [...], ...}
```

为什么要区分？

因为微压缩和自动压缩会**原地修改** `messages` 数组。如果只记录增量消息，恢复时就会丢失压缩操作。所以压缩后要写一个 `rewrite` 事件，把压缩后的完整消息快照保存下来。

恢复时，按顺序回放所有事件：

```python
for event in transcript:
    if event["type"] == "message":
        messages.append(deserialize(event["message"]))
    elif event["type"] == "rewrite":
        messages = [deserialize(m) for m in event["messages"]]
```

### 2.3 大内容外部存储

工具结果可能很大——读一个 6000 字符的文件，这条消息就占不少空间。如果 JSONL 里每行都塞完整内容，文件会膨胀很快。

所以超过阈值（默认 4000 字符）的内容会被存到 `blobs/` 目录下的独立文件，JSONL 里只存一个引用：

```python
# JSONL 中存的是这个（而不是完整文件内容）
{
  "storage": "external",
  "path": "blobs/msg_000001.txt",
  "preview": "import os\nfrom config import OPENAI_API_KEY...",
  "char_count": 6234
}
```

恢复时，遇到 `storage: "external"` 的引用，会自动从 blob 文件读回完整内容。

这对应 Claude Code 的 `ContentReplacement` 机制。

### 2.4 延迟批量刷盘

如果每产生一条消息就 `open → write → close`，频繁的磁盘 I/O 会拖慢响应。

所以这里用了一个 100ms 的延迟刷盘：

```python
async def append_message(self, message, messages):
    # 先把消息加入待写入队列
    self._pending_lines.append(json.dumps(event) + "\n")
    # 启动一个 100ms 后执行的刷盘任务
    self._schedule_flush(messages)
```

如果 100ms 内又来了新消息（比如工具调用循环中连续产生多条），它们会被合并到同一次写入中。

这对应 Claude Code 的 `dirty flag` 机制——标记"有脏数据"，等空闲时再刷盘。

### 2.5 `/resume` 恢复流程

```text
/resume
  │
  ├─ 扫描 sessions/ 目录
  │   └─ 读取每个会话的 meta.json（session_id、时间、消息数、模型）
  │
  ├─ 展示候选列表
  │   1. 1db60327 | 2026-04-12 14:27:36 | 4 条消息
  │
  ├─ 用户选择序号
  │
  ├─ 从 transcript.jsonl 重建 messages 数组
  │   ├─ 逐行回放 message / rewrite 事件
  │   └─ 遇到 external 引用 → 从 blobs/ 读回完整内容
  │
  ├─ 从 meta.json 恢复运行时状态
  │   ├─ 累计 token 和成本
  │   ├─ 压缩次数
  │   └─ 工作目录
  │
  └─ 创建新的 SessionStore，继续对话
```

恢复后，Claude 能记得之前聊过什么：

```text
  你> 你好呀，你知道我叫什么么

  Claude> 当然知道啦！你是 Leek Emperor 😎～
```

### 2.6 命令输入历史

除了对话历史，还支持方向键浏览之前输入过的命令。这个功能用 Python 标准库的 `readline` 实现：

```python
readline.read_history_file(history_path)   # 启动时加载
readline.add_history(command)               # 每次输入后记录
readline.write_history_file(history_path)   # 退出时保存
```

命令历史独立于对话历史——它记录的是你在终端里敲过什么，而不是 AI 的回复。

***

## 三、Token 计数：精确知道上下文有多满

### 3.1 为什么需要精确计数

上一篇用的粗估（字符数 / 3.5）误差很大。比如一段中文可能 1 个字符就接近 1 个 token，而一段英文代码可能 4 个字符才 1 个 token。

这一篇换成 `tiktoken`——OpenAI 官方的 token 计数库：

```python
import tiktoken

def count_text_tokens(text: str, model: str) -> int:
    try:
        encoder = tiktoken.encoding_for_model(model)
        return len(encoder.encode(text))
    except KeyError:
        # 不认识的模型回退到通用编码
        return len(tiktoken.get_encoding("cl100k_base").encode(text))
```

如果 `tiktoken` 不可用，自动回退到粗估。

### 3.2 上下文分析

光知道总 token 数还不够。还需要知道**空间被什么占满了**：

```python
def analyze_context(messages, model):
    # 统计各角色的 token 占比
    tokens_by_role = {"user": 1200, "assistant": 800, "tool": 35000}

    # 统计各工具的 token 占比
    tokens_by_tool = {"readFile": 32000, "listFiles": 3000}

    # 识别重复文件读取
    repeated_reads = [{"path": "test.md", "count": 3}]
```

每轮对话后会打印一个简短报告：

```text
  [上下文: 7,437 / 128,000 tokens, 5.8%]
  [工具占比: readFile:6221]
```

当出现重复读取时，还会额外提醒：

```text
  [重复读取提醒: test.md 已读取 3 次]
```

这个分析结果不只是给人看的——微压缩和自动压缩都依赖它来判断"该不该压缩"。

***

## 四、微压缩：零成本回收空间

### 4.1 核心思想

对话中，工具结果往往是上下文膨胀的最大元凶。比如读一个文件 6000 字符，搜索一次 3000 字符。这些结果在产生时有用，但几轮之后就不再需要了。

微压缩做的事情很简单：**把旧工具结果的内容替换成一个占位符**。

```text
压缩前:
  tool: "import os\nfrom config import ...\n（6000 字符的文件内容）"

压缩后:
  tool: "[Old result cleared by micro compact: readFile]"
```

关键点：**只清内容，不清结构**。消息的 role、tool\_call\_id 都保留，所以对话结构完整无损。

### 4.2 哪些工具结果可以清

还记得第 1 篇在注册工具时留的 `compactable` 标记吗？现在它派上用场了：

```python
registry.register(
    name="readFile",
    ...
    compactable=True,   # ← 这个标记现在生效了
)
```

微压缩只会清除标记为 `compactable` 的工具结果。当前三个文件工具和 webSearch 都标记了。

### 4.3 保留最近 5 个

不是所有旧工具结果都该清——最近几个可能还在用。所以默认保留最近 5 个，只清更早的：

```python
candidate_indexes = [所有可压缩的 tool 消息索引]
replace_indexes = candidate_indexes[:-5]   # 保留最后 5 个
```

### 4.4 自动触发

每轮对话开始前，检查上下文是否超过窗口的 70%。如果超过，自动执行微压缩：

```text
  你> 读取一下当前目录下所有的文件内容

  [微压缩] 清理 9 条旧工具结果，释放 ~30811 tokens
```

也可以手动触发：

```text
  你> /compact

  ✅ 已微压缩 9 条旧工具结果，释放 ~30811 tokens
```

### 4.5 实际效果

下面是一段真实的运行记录（已脱敏）：

```text
  你> 读取一下 test.md 文件
  [上下文: 189 / 128,000 tokens, 0.1%]

  🔧 调用工具: readFile({"path": "test.md"})

  Claude> 已成功读取 test.md 文件内容...

  你> 读取一下当前目录下所有的文件内容，然后给我总结一下
  [上下文: 7,460 / 128,000 tokens, 5.8%]
  [工具占比: readFile:6221]

  🔧 调用工具: listFiles({"path": "./", "pattern": "**/*"})
  🔧 调用工具: readFile({"path": "claude-code-记忆系统-01-总览.md"})
  🔧 调用工具: readFile({"path": "claude-code-记忆系统-02-运行时状态.md"})
  ...（连续读了 7 个文件）

  [上下文: 41,354 / 128,000 tokens, 32.3%]
  [工具占比: readFile:38786, listFiles:615]

  你> /compact

  ✅ 已微压缩 9 条旧工具结果，释放 ~30811 tokens
```

一次 `/compact` 就释放了 \~30K tokens，效果非常明显。

***

## 五、自动压缩：用 AI 摘要旧对话

微压缩只能清工具结果。如果用户和 Claude 来回了 30 轮纯文本对话，微压缩无能为力。

这时候需要自动压缩出马——**用 LLM 把旧对话总结成一段摘要**。

### 5.1 触发条件

当上下文使用超过窗口的 85% 时触发：

```python
AUTO_COMPACT_TRIGGER_RATIO = 0.85
```

### 5.2 压缩流程

```text
上下文超过 85%
  │
  ├─ 检查熔断（连续失败 3 次 → 跳过）
  │
  ├─ 切分消息：旧消息 vs 最近 2 轮
  │
  ├─ 构建压缩 prompt（9 部分结构化摘要）
  │
  ├─ 调用 LLM 生成摘要
  │
  └─ 替换旧消息为：摘要 + 工作区快照 + 边界标记 + 最近对话
```

### 5.3 9 部分结构化摘要

Claude Code 的压缩 prompt 要求生成 9 个部分。这里也采用了类似结构：

```text
1) 用户目标
2) 当前实现状态
3) 关键文件
4) 重要工具结果
5) 已修复问题
6) 未解决问题
7) 当前约束
8) 建议下一步
9) 需要继续记住的细节
```

### 5.4 压缩后恢复

摘要替换了旧消息，但丢失了"当前工作区有什么文件"这个关键上下文。所以压缩后会自动补回一个工作区快照：

```python
def _build_workspace_snapshot() -> str:
    entries = sorted(os.listdir(state.workspace_root))[:20]
    return f"工作区顶层文件:\n" + "\n".join(f"- {name}" for name in entries)
```

同时在摘要和保留消息之间插入一个边界标记：

```python
{"role": "assistant", "content": "[以下开始为压缩后保留的最近原始对话]", "compact_boundary": True}
```

### 5.5 熔断机制

如果压缩连续失败 3 次（比如 API 限流），进入熔断状态，不再重试：

```text
  [自动压缩] 自动压缩已连续失败 3 次，已进入熔断状态
```

这对应 Claude Code 的熔断机制——之前有人观察到连续失败 50+ 次的会话，每天浪费约 25 万次 API 调用。

***

## 六、集成到主循环

### 6.1 每条消息实时持久化

`chat_loop()` 中，每产生一条消息就写入 JSONL：

```python
async def _append_message(messages, message, session_store):
    messages.append(message)
    if session_store is not None:
        await session_store.append_message(message, messages)
```

压缩和清空时，写入 `rewrite` 事件：

```python
await session_store.append_snapshot(messages, "auto_micro_compact")
```

### 6.2 每轮前自动微压缩

```python
for _ in range(max_iterations):
    # 每轮开始先检查上下文
    current_tokens = count_messages_tokens(messages, state.model)
    if should_auto_micro_compact(current_tokens, MAX_CONTEXT_TOKENS):
        compact_result = apply_micro_compact(messages, ...)
        if compact_result["changed"]:
            print(f"  [微压缩] 清理 {compact_result['replaced_count']} 条旧工具结果")
```

### 6.3 每轮后自动压缩检查

```python
# 模型给出最终回复后
auto_compact_result = await maybe_auto_compact(messages)
if auto_compact_result.get("changed"):
    messages = auto_compact_result["messages"]
    print(f"  [自动压缩] 生成历史摘要，释放 ~{auto_compact_result['freed_tokens']} tokens")
```

### 6.4 启动时检测历史会话

```python
history_sessions = [s for s in SessionStore.list_sessions() if s["session_id"] != state.session_id]
if history_sessions:
    print(f"  检测到 {len(history_sessions)} 个历史会话，可使用 /resume 恢复")
```

### 6.5 增强的 `/stats`

```text
  你> /stats

  💰 累计成本: $0.0005
  📊 输入 token: 2,284
  📊 输出 token: 324
  🧠 当前上下文: 180 / 128,000
  🗜  微压缩次数: 0
  📝 自动压缩次数: 0
  ⏱  会话时长: 86s
```

***

## 七、workspace\_root：安全边界

这一篇新增了一个 `workspace_root` 概念。文件工具的路径安全检查从 `cwd` 改成了 `workspace_root`：

```python
# 启动时
state.workspace_root = os.path.join(state.cwd, "workspace")
os.makedirs(state.workspace_root, exist_ok=True)

# 文件工具中
real_path = os.path.realpath(path)
real_workspace = os.path.realpath(state.workspace_root)
if not real_path.startswith(real_workspace):
    return f"错误: 不允许访问工作区之外的文件"
```

这样用户在 `learning-memory/` 目录下启动程序，但文件工具只能访问 `learning-memory/workspace/`。防止 AI 意外读取或修改程序自身的代码。

***

## 八、完整交互示例

下面是一段真实的运行记录（路径和模型名已脱敏）：

```text
  ╔══════════════════════════════════════╗
  ║   learning-memory v0.1               ║
  ║   从零实现 Claude Code 记忆系统         ║
  ╚══════════════════════════════════════╝

  模型: <your-model>
  工具: readFile, writeFile, listFiles, webSearch
  启动目录: /path/to/learning-memory
  工作区: /path/to/learning-memory/workspace
  会话 ID: 1db60327

  检测到 1 个历史会话，可使用 /resume 恢复

  你> /resume

  可恢复的历史会话:
    1. 1db60327 | 2026-04-12 14:27:36 | 4 条消息 | <your-model>

  输入序号恢复（回车取消）: 1

  ✅ 已恢复会话 1db60327（4 条消息，更新时间 2026-04-12 14:27:36）

  你> 你好呀，你知道我叫什么么
  [上下文: 161 / 128,000 tokens, 0.1%]

  Claude> 当然知道啦！你是 Leek Emperor 😎～

  你> /stats

  💰 累计成本: $0.0005
  📊 输入 token: 2,284
  📊 输出 token: 324
  🧠 当前上下文: 180 / 128,000
  🗜  微压缩次数: 0
  📝 自动压缩次数: 0
  ⏱  会话时长: 86s
```

***

## 九、和 Claude Code 思路的对应关系

| 本篇模块                      | Claude Code 中的对应思路                      | 保留了什么                                      |
| ------------------------- | --------------------------------------- | ------------------------------------------ |
| `session.py` SessionStore | transcript.ts                           | JSONL append-only、外部大内容存储、延迟刷盘、rewrite 事件  |
| `session.py` /resume      | history.ts + replLauncher.tsx           | 会话列表扫描、meta.json 快照、消息重建、状态恢复              |
| `token_counter.py`        | tokenEstimation.ts + contextAnalysis.ts | tiktoken 精确计数、粗估回退、工具占比分析、重复读取检测           |
| `micro_compact.py`        | microCompact.ts                         | compactable 白名单、keep\_recent、占位替换、自动触发     |
| `auto_compact.py`         | compact.ts + compact prompt             | 85% 阈值、9 部分摘要、轮次切分、工作区快照恢复、熔断机制            |
| `state.py` 新增字段           | Bootstrap State                         | 压缩统计、workspace\_root、last\_context\_tokens |

***

## 十、这一篇最值得记住的事

> 短期记忆的本质是"让对话在有限窗口内尽可能持久"。

具体来说就是三件事：

1. **持久化**：JSONL append-only + 大内容外部存储 + 延迟刷盘
2. **微压缩**：清旧工具结果的内容，保留结构，零 API 成本
3. **自动压缩**：用 AI 摘要旧对话，保留最近几轮，熔断保护

这三层加在一起，就能让一个 128K 窗口的模型支撑几百轮的长对话。

***

## 系列导航

| # | 篇名                                 | 状态    |
| - | ---------------------------------- | ----- |
| 1 | **构建带工具调用的 LLM Loop + Web Search** | ✅ 已完成 |
| 2 | **短期记忆：会话持久化与上下文压缩**               | ✅ 本篇  |
| 3 | 长期记忆：项目指令与自动记忆                     | 待创作   |

