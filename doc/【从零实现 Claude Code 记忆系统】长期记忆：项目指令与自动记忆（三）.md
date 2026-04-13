<br />

> 从零实现 Claude Code 记忆系统 · 实践篇第 3 篇
>
> 上一篇实现了短期记忆（持久化 + 压缩），这一篇加上"长期记忆"——让 AI 越用越聪明。

***

## 本篇要解决什么问题

前两篇做完，程序已经能记住当前会话的内容了。但有一个根本性的限制：**关掉再开，它什么都不记得**。

你告诉它"我是前端工程师，在学 Android"，它在这个会话里记住了。但下次启动新会话，它又变成了一张白纸。同样的问题反复说，同样的偏好反复纠正。

这一篇要解决的就是**跨会话的长期记忆**——让 AI 自动从对话中提取值得记住的信息，下次启动时自动回忆。

最终效果：

- 你说"我是前端工程师"，下次新会话它就知道
- 你纠正"不要用 mock 测试数据库"，它再也不会犯
- 你提到"周四之后合并冻结"，它会在相关讨论中主动提醒

项目地址：<https://github.com/leek-emperor/learning-memory>

***

## 一、先看改了什么

```
新增文件:
  claude_md.py          CLAUDE.md 多层加载 + @include + HTML 过滤
  memdir.py             记忆目录管理（读写、索引、截断）
  memory_prompt.py      记忆系统指令 prompt 构建
  memory_extract.py     每轮自动记忆提取
  memory_retrieve.py    记忆检索（扫描 + LLM 选择 + 注入）

修改文件:
  state.py              新增 memory_written_this_turn、recent_surfaced_memory_ids
  loop.py               system prompt 注入 CLAUDE.md + 记忆指令
  main.py               /remember 实现 + /init + 启动时注入记忆
```

***

## 二、整体数据流

先看全貌，再逐个拆解：

```text
启动时:
  CLAUDE.md (全局 → 项目 → 本地)
    ↓ 加载
  MEMORY.md (记忆索引)
    ↓ 加载
  system prompt = CLAUDE.md + 记忆系统指令 + MEMORY.md 内容
    ↓
  注入到每轮 API 请求

每轮对话:
  ① 用户输入 "帮我重构登录模块"
  ② [检索] 扫描 memory/ → LLM 选择相关记忆 → 注入上下文
  ③ [调用] 带记忆上下文 + 用户消息一起发给模型
  ④ [提取] 对话结束后，分析新消息 → 写入 memory/ + 更新 MEMORY.md

跨会话:
  下次启动 → 自动加载 CLAUDE.md + MEMORY.md → 检索相关记忆
  → "我知道你是前端工程师，合并冻结从周四开始"
```

***

## 三、CLAUDE.md：用户手写的项目规则

### 3.1 为什么需要 CLAUDE.md？

有些知识不适合让 AI 自己猜，需要用户**显式声明**：

- 这个项目用 Bun 不用 npm
- 所有 API 变更必须同步更新文档
- 不要用 mock 测试数据库

Claude Code 用 `CLAUDE.md` 文件解决这个问题。我们也实现了同样的机制。

### 3.2 多层加载

按优先级从低到高加载三层 CLAUDE.md：

```text
~/.learning-memory/CLAUDE.md     ← 全局（所有项目共享）
./CLAUDE.md                      ← 项目级（可提交到 Git）
./CLAUDE.local.md                ← 本地覆盖（不提交 Git）
```

三层内容拼接后注入 system prompt。后面的可以覆盖前面的规则。

### 3.3 @include 指令

支持在 CLAUDE.md 中引用外部文件：

```markdown
# 项目规范

## 编码规范
@./docs/coding-standards.md

## API 文档
@./docs/api-spec.md
```

解析器会递归加载被引用的文件内容，替换 `@` 行。同时检测循环引用，避免无限递归。

### 3.4 HTML 注释过滤

CLAUDE.md 中的 `<!-- -->` 注释会被自动剥离，不发送给 LLM：

```markdown
<!-- 这是内部备注，Claude 看不到 -->
- 使用 Bun 作为包管理器
```

团队可以在 CLAUDE.md 中留下人类专属备注，不干扰 AI。

### 3.5 `/init` 命令

快速生成项目级 CLAUDE.md 模板：

```text
  你> /init

  项目名: my-app
  技术栈: Python, FastAPI, PostgreSQL
  编码规范: 优先小步修改，避免无关重构

  ✅ 已生成 ./CLAUDE.md
```

### 3.6 注入位置

CLAUDE.md 内容被拼接到 system prompt 的**最前面**——这是 LLM 注意力最高的位置：

```python
def build_system_prompt():
    sections = []
    claude_md_text = load_claude_md_text()      # 第一层
    memory_prompt = build_memory_system_prompt()  # 第二层
    sections.append(SYSTEM_PROMPT)                # 第三层（基础指令）
    return "\n\n".join(sections)
```

***

## 四、Memdir：文件即记忆

### 4.1 核心思想

Claude Code 的自动记忆系统叫 Memdir——**Memory + Directory**。不用数据库，不用向量引擎，直接用 Markdown 文件存储记忆。

我们也采用同样的方式：

```text
~/.learning-memory/memory/
├── MEMORY.md                    ← 入口索引（≤200 行 / 25KB）
├── user--senior-fe-dev.md       ← 用户画像
├── feedback--no-mock-db.md      ← 反馈指导
├── project--merge-freeze.md     ← 项目上下文
└── reference--linear-ingest.md  ← 外部引用
```

每个记忆文件就是一个普通的 Markdown，带 YAML frontmatter：

```markdown
---
name: Senior FE Dev
type: user
description: 10年Go经验，在学React和Android
created_at: 1744800000.0
---

用户是资深前端工程师，有 10 年 Go 后端经验。
目前在学 React 和 Android 开发。
解释后端概念时可以用 Go 类比。
```

### 4.2 四种记忆类型

```text
┌──────────────────────────────────────────────────────────────┐
│  user — 用户画像                                              │
│  保存时机: 了解用户角色、偏好、知识水平时                        │
│  示例: "10 年 Go 经验，React 新手"                             │
├──────────────────────────────────────────────────────────────┤
│  feedback — 反馈指导                                          │
│  保存时机: 用户纠正或确认行为时                                  │
│  示例: "集成测试必须连真实数据库，不用 mock"                      │
├──────────────────────────────────────────────────────────────┤
│  project — 项目上下文                                         │
│  保存时机: 了解项目结构、进度、约束时                             │
│  示例: "合并冻结从周四开始"                                     │
├──────────────────────────────────────────────────────────────┤
│  reference — 外部引用                                         │
│  保存时机: 了解外部系统的位置和用途时                             │
│  示例: "Pipeline bug 追踪在 Linear 项目 INGEST"                │
└──────────────────────────────────────────────────────────────┘
```

### 4.3 写入与去重

写入记忆时，先检查是否已存在同类型、同名的记忆：

```python
existing = _find_existing_by_type_and_name(memory_type, name)
if existing:
    file_name = existing.file_name   # 更新已有文件
    created_at = existing.created_at  # 保留原始创建时间
else:
    file_name = _memory_file_name(memory_type, name)  # 创建新文件
```

这避免了重复创建——比如用户在多个会话中都提到"我是前端工程师"，只会更新同一条记忆，不会创建多条。

### 4.4 MEMORY.md 索引

每条记忆写入后，自动重建 MEMORY.md 索引：

```markdown
# MEMORY

- [user] Senior FE Dev (user--senior-fe-dev.md) - 10年Go经验，在学React和Android
- [feedback] No Mock DB (feedback--no-mock-db.md) - 集成测试必须连真实数据库
- [project] Merge Freeze (project--merge-freeze.md) - 合并冻结从周四开始
```

MEMORY.md 是索引，不是记忆本身。每行一个条目，简洁明了。

### 4.5 截断保护

MEMORY.md 有双重限制：

```python
INDEX_MAX_LINES = 200
INDEX_MAX_BYTES = 25 * 1024   # 25KB
```

超出时从末尾截断，并附加警告。这防止单个文件占满上下文窗口。

### 4.6 文件名生成

记忆的文件名由类型和名称自动生成：

```python
def _slugify(value: str) -> str:
    # "Senior FE Dev" → "senior-fe-dev"
    # 支持中文: "合并冻结" → "合并冻结"

def _memory_file_name(memory_type: str, name: str) -> str:
    # "user" + "Senior FE Dev" → "user--senior-fe-dev.md"
```

***

## 五、记忆指令：告诉 AI 怎么记

AI 不会"凭直觉"记东西。需要通过一段详细的 system prompt 指令来规范行为。

### 5.1 指令结构

```text
你有一个长期记忆系统，位于 ~/.learning-memory/memory/ 目录。

## 记忆类型
  四种类型的详细说明

## 保存原则
  - 只有跨会话仍有价值的稳定信息才值得保存
  - 先查重，再写入
  - 写入时先保存主题文件，再更新 MEMORY.md 索引

## 不该保存的内容
  - 一次性的临时状态
  - 很快会失效的目录结构快照
  - Git 历史、提交哈希、短期调试输出

## 使用义务
  - 记忆说 X 存在 ≠ X 现在存在
  - 在建议用户操作前，必须再次验证
  - 记忆是辅助信息，不可覆盖用户当前明确指令

## 当前 MEMORY.md 索引
  （动态注入当前索引内容）
```

### 5.2 "不该保存的内容"很重要

这个排除列表防止记忆系统变成代码的冗余副本：

- 代码模式、架构 → 可以从代码中读取
- 目录结构 → 很快会变
- Git 历史 → `git log` 是权威来源
- 临时状态 → 只对当前轮有用

Claude Code 的源码中用了一整节来定义这个排除列表，我们也保留了。

### 5.3 "使用义务"：验证优先

这是最关键的设计——**记忆可能过时**。指令明确要求 AI 在使用记忆前必须验证：

```text
记忆中提到某个文件、路径、接口或配置存在，
并不代表它现在仍然存在。
在建议用户操作前，必须结合当前工作区或当前上下文再次验证。
```

对应 Claude Code 源码中的 "Before recommending" 章节。

### 5.4 MEMORY.md 内容动态注入

指令的最后会附上当前 MEMORY.md 的完整内容（截断到 6000 字符）。这样 AI 在每轮对话中都能看到所有记忆的索引，知道"自己知道什么"。

***

## 六、记忆提取：每轮自动分析对话

### 6.1 触发时机

每轮对话结束后（`chat_loop` 返回后），自动触发记忆提取。

### 6.2 互斥机制

如果 AI 在对话中**自己已经写了记忆文件**（通过 `writeFile` 工具写到了 memory/ 目录），就跳过自动提取。避免重复。

实现方式：`state.memory_written_this_turn` 标志位。主 Agent 写记忆时设为 `True`，提取函数检查到 `True` 就跳过。

### 6.3 游标机制

用 `state.last_processed_msg_index` 追踪已处理到哪条消息，只分析新增的消息：

```python
new_messages = messages[state.last_processed_msg_index:]
filtered = [m for m in new_messages if not _should_skip_message(m)]
```

跳过的消息类型：记忆上下文注入、压缩摘要、工作区快照——这些不是用户的真实对话。

### 6.4 调用 LLM 分析

构建一个提取 prompt，让 LLM 从最近 12 条消息中识别值得保存的信息：

```text
你是一个长期记忆提取助手。
请从下面新增对话中提取值得跨会话保存的稳定信息。
可用类型只有 user / feedback / project / reference。
不要保存临时状态、一次性调试信息。
只返回 JSON 数组，每项格式为：
{"type":"user","name":"...","description":"...","body":"..."}。
如果没有值得保存的内容，返回 []。
```

LLM 返回 JSON 数组后，逐条调用 `write_memory()` 写入文件。

### 6.5 为什么不用主 Agent 自己记？

Claude Code 的设计是"主 Agent 自己记 + Extract 兜底"。我们的实现是"Extract 为主 + 主 Agent 可以通过工具辅助"。

原因：把记忆提取逻辑放在主循环之外，更可控、更可预测。主 Agent 的 system prompt 中虽然有记忆指令，但它不一定每轮都会主动写记忆——Extract 确保不会遗漏。

***

## 七、记忆检索：每轮自动召回

### 7.1 为什么需要检索？

不能把所有记忆都塞进上下文——那会浪费 token。需要在每轮对话前，**只注入与当前输入最相关的记忆**。

### 7.2 检索流程

```text
用户输入 "帮我重构登录模块"
  │
  ├─ 扫描 memory/ 目录
  │   └─ 获取所有记忆的 frontmatter（name, type, description）
  │
  ├─ 过滤已展示的记忆
  │   └─ 最近几轮已经注入过的不再重复选择
  │
  ├─ LLM 相关性选择
  │   ├─ 输入: 用户查询 + 记忆清单（最多 40 条）
  │   ├─ 输出: 最多 5 个 memory_id
  │   └─ max_tokens: 256（非常轻量）
  │
  └─ 读取选中的记忆完整内容 → 注入上下文
```

### 7.3 去重：已展示的不重复选

`state.recent_surfaced_memory_ids` 记录了最近展示过的记忆 ID（最多保留 20 个）。检索时排除这些 ID，避免连续几轮都注入同一条记忆。

```python
candidates = [
    item for item in scan_memories()
    if item.memory_id not in state.recent_surfaced_memory_ids
]
```

### 7.4 新鲜度警告

超过 7 天的记忆会被标记为"可能已过时"：

```python
def _is_stale(memory: MemoryItem) -> bool:
    return (time.time() - memory.created_at) > (7 * 24 * 3600)
```

注入时附带警告：

```text
[相关长期记忆]
[注意] 此记忆可能已过时，使用前请再次验证。
类型: project
名称: Merge Freeze
描述: 合并冻结从周四开始
```

### 7.5 注入位置

检索到的记忆作为 `user` 角色的消息注入，标记 `memory_context: True`（用于后续跳过和过滤）：

```python
{
    "role": "user",
    "content": "[相关长期记忆]\n类型: user\n...",
    "memory_context": True,
    "memory_id": "user--senior-fe-dev"
}
```

注入时机：在用户消息**之前**。这样 LLM 在看到用户输入前，就已经有了相关记忆的上下文。

***

## 八、`/remember` 命令：人工审查

自动提取不完美，需要给人一个手动管理的入口。

```text
  你> /remember

  /remember 可选操作:
    1. 列出所有记忆
    2. 查看某条记忆
    3. 删除某条记忆
    4. 查看 MEMORY.md
    5. 清空所有记忆
    6. 手动添加记忆

  选择操作（回车取消）: 1

  当前长期记忆:
    1. [user] Senior FE Dev | user--senior-fe-dev.md | 2026-04-12 14:27
    2. [feedback] No Mock DB | feedback--no-mock-db.md | 2026-04-12 14:30
    3. [project] Merge Freeze | project--merge-freeze.md | 2026-04-12 14:35
```

支持查看详情、删除单条、清空全部、手动添加。手动添加时需要指定类型、名称、描述、正文四个字段。

***

## 九、集成到主循环

### 9.1 每轮前的记忆检索

```python
# main.py 中，用户输入后、发给 LLM 前
state.reset_memory_written_this_turn()

memory_context_messages = await inject_relevant_memories(messages, user_input)
for memory_message in memory_context_messages:
    messages.append(memory_message)
    await session_store.append_message(memory_message, messages)
```

### 9.2 每轮后的记忆提取

```python
# chat_loop 返回后
saved_memories = await extract_memories_from_messages(messages)
if saved_memories:
    print(f"  💾 保存了 {saved_memories} 条长期记忆")
```

### 9.3 system prompt 的三层拼装

```python
def build_system_prompt():
    sections = []
    sections.append(load_claude_md_text())           # 第一层：用户规则
    sections.append(build_memory_system_prompt())     # 第二层：记忆指令 + 索引
    sections.append(SYSTEM_PROMPT)                    # 第三层：基础指令
    return "\n\n".join(sections)
```

用户手写的规则在最前面（最高优先级），记忆指令在中间，基础指令在最后。

### 9.4 增强的 `/stats`

```text
  你> /stats

  💰 累计成本: $0.0012
  📊 输入 token: 5,670
  📊 输出 token: 820
  🧠 当前上下文: 2,340 / 128,000
  🗜  微压缩次数: 1
  📝 自动压缩次数: 0
  💾 记忆数量: 3
  📚 MEMORY.md 大小: 342 bytes
  ⏱  会话时长: 120s
```

***

## 十、完整交互示例

下面展示一个跨会话记忆的完整流程：

```text
  ╔══════════════════════════════════════╗
  ║   learning-memory v0.1               ║
  ║   从零实现 Claude Code 记忆系统         ║
  ╚══════════════════════════════════════╝

  你> 你好，我叫 Leek Emperor，是一名前端工程师

  Claude> 你好 Leek Emperor！很高兴认识你。作为一名前端工程师，你平时主要用哪些技术栈呢？

  💾 保存了 1 条长期记忆

  你> 对了，我们团队有个规矩：集成测试必须连真实数据库，不要用 mock

  Claude> 明白了，这是一个很好的工程实践。使用真实数据库进行集成测试...

  💾 保存了 1 条长期记忆

  你> /remember

  当前长期记忆:
    1. [user] Leek Emperor Identity | user--leek-emperor-identity.md
    2. [feedback] Real DB For Integration Tests | feedback--real-db-for-integration-tests.md

  你> /exit

  ══════════════════════════════════════════

  （重新启动程序）

  ╔══════════════════════════════════════╗
  ║   learning-memory v0.1               ║
  ╚══════════════════════════════════════╝

  你> 帮我写一个用户登录的集成测试

  [相关长期记忆]
  类型: feedback
  名称: Real DB For Integration Tests
  描述: 集成测试必须连真实数据库，不要用 mock

  Claude> 好的，我来帮你写一个连接真实数据库的用户登录集成测试。
  根据之前的约定，我们不使用 mock，而是直接连接...
```

注意最后一轮——AI 自动检索到了"不要用 mock"这条记忆，并在写测试时遵守了这个规则。这就是"越用越聪明"。

***

## 十一、和 Claude Code 思路的对应关系

| 本篇模块 | Claude Code 中的对应思路 | 保留了什么 |
| -------- | ------------------------ | ---------- |
| `claude_md.py` 多层加载 | claudemd.ts | 三层优先级、@include 递归、HTML 过滤 |
| `claude_md.py` /init | init.ts | 交互式生成模板 |
| `memdir.py` 文件存储 | memdir.ts | frontmatter 格式、四种类型、slugify 文件名 |
| `memdir.py` 索引管理 | memdir.ts | MEMORY.md 索引、200 行 / 25KB 截断 |
| `memdir.py` 去重写入 | extractMemories | 同类型+同名更新，不重复创建 |
| `memory_prompt.py` | memdir.ts 的 prompt | 类型定义、排除列表、保存原则、验证义务 |
| `memory_extract.py` | extractMemories.ts | 游标机制、互斥检查、LLM 分析、JSON 输出 |
| `memory_retrieve.py` | findRelevantMemories | 扫描 frontmatter、LLM 选择 ≤5 条、去重、新鲜度警告 |
| `state.py` 新增字段 | Bootstrap State | memory_written_this_turn、recent_surfaced_memory_ids |

***

## 十二、这一篇最值得记住的事

> 长期记忆的本质是"让 AI 从对话中自动提取稳定知识，下次启动时自动召回"。

具体来说就是四件事：

1. **CLAUDE.md**：用户手写的项目规则，注入 system prompt 最高优先级位置
2. **Memdir**：文件即记忆，Markdown + frontmatter，四种类型，索引与内容分离
3. **记忆提取**：每轮结束后 LLM 分析对话，写入 memory/ 目录
4. **记忆检索**：每轮对话前 LLM 选择最相关的 ≤5 条记忆，注入上下文

三层记忆体系至此完整：

```text
Layer 3: 长期记忆（本篇）    → 跨会话，越用越聪明
Layer 2: 会话持久化（第 2 篇） → 跨进程，关掉不丢
Layer 1: 运行时状态（第 1 篇） → 进程内，当前会话
```

***

## 系列导航

| # | 篇名 | 状态 |
| - | ---------------------------------- | ---- |
| 1 | **构建带工具调用的 LLM Loop + Web Search** | ✅ 已完成 |
| 2 | **短期记忆：会话持久化与上下文压缩** | ✅ 已完成 |
| 3 | **长期记忆：项目指令与自动记忆** | ✅ 本篇 |
