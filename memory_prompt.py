"""记忆系统 Prompt 构建。"""
from memdir import read_memory_index

MEMORY_INDEX_INJECTION_LIMIT = 6000


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[已截断，共 {len(text)} 字符]"


def build_memory_system_prompt() -> str:
    """构建长期记忆系统说明，供 system prompt 注入。"""
    memory_index = read_memory_index().strip()
    memory_index_section = ""
    if memory_index:
        memory_index_section = (
            "\n\n## 当前 MEMORY.md 索引\n\n"
            + _truncate_text(memory_index, MEMORY_INDEX_INJECTION_LIMIT)
        )

    return (
        "你有一个长期记忆系统，位于 ~/.learning-memory/memory/ 目录。\n\n"
        "## 记忆类型\n"
        "- user: 关于用户身份、背景、偏好、长期习惯的稳定事实\n"
        "- feedback: 用户对输出风格、工作方式、协作偏好的反馈\n"
        "- project: 当前项目的结构、规则、架构约定、关键背景\n"
        "- reference: 长期有参考价值的链接、命令、操作说明、外部资料\n\n"
        "## 保存原则\n"
        "- 只有跨会话仍有价值的稳定信息才值得保存\n"
        "- 先查重，再写入；存在相同记忆时应更新而不是重复创建\n"
        "- 写入时先保存主题文件，再更新 MEMORY.md 索引\n\n"
        "## 不该保存的内容\n"
        "- 一次性的临时状态\n"
        "- 很快会失效的目录结构快照\n"
        "- Git 历史、提交哈希、短期调试输出\n"
        "- 仅对当前单轮回答有用的上下文噪音\n\n"
        "## 使用义务\n"
        "- 记忆中提到某个文件、路径、接口或配置存在，并不代表它现在仍然存在\n"
        "- 在建议用户操作前，必须结合当前工作区或当前上下文再次验证\n"
        "- 记忆是辅助信息，不可覆盖用户当前明确指令"
        + memory_index_section
    )
