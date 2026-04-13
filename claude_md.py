"""CLAUDE.md 多层加载与模板生成。"""
import os
import re
from typing import List, Set

from config import DATA_ROOT
from state import state

HTML_COMMENT_PATTERN = re.compile(r"<!--[\s\S]*?-->")
INCLUDE_PATTERN = re.compile(r"^\s*@(?P<path>\./[^\s]+)\s*$")


def _strip_html_comments(content: str) -> str:
    """移除 HTML 注释，避免无效内容进入 system prompt。"""
    return HTML_COMMENT_PATTERN.sub("", content)


def _load_markdown_with_includes(file_path: str, visited: Set[str]) -> str:
    """递归解析 @include，路径相对于当前文件所在目录。"""
    real_path = os.path.realpath(file_path)
    if real_path in visited:
        return f"\n[检测到循环 include，已跳过: {file_path}]\n"

    if not os.path.isfile(real_path):
        return ""

    visited.add(real_path)
    with open(real_path, "r", encoding="utf-8") as file:
        raw_content = file.read()

    base_dir = os.path.dirname(real_path)
    rendered_lines: List[str] = []
    for line in raw_content.splitlines():
        match = INCLUDE_PATTERN.match(line)
        if not match:
            rendered_lines.append(line)
            continue

        include_relative_path = match.group("path")
        include_path = os.path.join(base_dir, include_relative_path[2:])
        included = _load_markdown_with_includes(include_path, visited)
        if included:
            rendered_lines.append(included.strip())

    visited.remove(real_path)
    return "\n".join(rendered_lines)


def _candidate_paths() -> List[str]:
    """按约定顺序返回 CLAUDE.md 候选文件。"""
    return [
        os.path.join(DATA_ROOT, "CLAUDE.md"),
        os.path.join(state.cwd, "CLAUDE.md"),
        os.path.join(state.cwd, "CLAUDE.local.md"),
    ]


def load_claude_md_text() -> str:
    """加载多层 CLAUDE.md，并返回最终注入文本。"""
    sections: List[str] = []
    for path in _candidate_paths():
        content = _load_markdown_with_includes(path, visited=set())
        content = _strip_html_comments(content).strip()
        if content:
            sections.append(content)

    return "\n\n".join(section for section in sections if section).strip()


def has_project_claude_md() -> bool:
    """检查项目层是否已经存在 CLAUDE.md。"""
    return os.path.isfile(os.path.join(state.cwd, "CLAUDE.md"))


def build_init_template(project_name: str, tech_stack: str, coding_rules: str) -> str:
    """生成项目级 CLAUDE.md 模板。"""
    project_name = project_name.strip() or "我的项目"
    tech_stack = tech_stack.strip() or "Python, OpenAI API"
    coding_rules = coding_rules.strip() or "优先小步修改，避免无关重构，必要时补充简洁注释。"

    return f"""# {project_name}

## 项目背景

- 项目名称：{project_name}
- 技术栈：{tech_stack}

## 你在这个项目中的工作方式

- 优先理解当前代码结构，再开始修改
- 保持改动聚焦，不做与当前任务无关的重构
- 修改后优先做最小验证，确保行为正确

## 编码规范

- {coding_rules}

## 额外说明

- 文件工具默认只允许操作 `workspace/`
- 使用记忆中的信息前，需要先验证它是否仍然有效
"""
