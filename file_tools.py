"""文件操作工具 —— readFile / writeFile / listFiles

对应 Claude Code 的 FileReadTool / FileWriteTool / GlobTool。
这三个是 LLM 编程助手的基础能力。
"""
import os
from typing import Optional, Tuple
from state import state


def _get_workspace_root() -> str:
    """获取文件工具的工作区根目录。"""
    return os.path.realpath(state.workspace_root or state.cwd)


def _resolve_workspace_path(path: str) -> str:
    """将相对路径解析到工作区内，绝对路径保持原样。"""
    if os.path.isabs(path):
        return path
    return os.path.join(_get_workspace_root(), path)


def _ensure_within_workspace(path: str, display_path: str) -> Tuple[str, Optional[str]]:
    """校验路径是否落在工作区内，返回真实路径和错误信息。"""
    real_path = os.path.realpath(path)
    workspace_root = _get_workspace_root()
    if os.path.commonpath([real_path, workspace_root]) != workspace_root:
        return real_path, f"错误: 不允许访问工作区之外的路径: {display_path}"
    return real_path, None


async def read_file(args: dict) -> str:
    """读取文件内容"""
    path = args.get("path", "")
    if not path:
        return "错误: 缺少 path 参数"

    # 相对路径默认从工作区根目录开始解析。
    real_path, error = _ensure_within_workspace(_resolve_workspace_path(path), path)
    if error:
        return error

    if not os.path.isfile(real_path):
        return f"错误: 文件不存在: {path}"

    try:
        with open(real_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 限制返回大小（防止超大文件占满上下文）
        MAX_CHARS = 30000
        if len(content) > MAX_CHARS:
            content = content[:MAX_CHARS] + f"\n\n... [文件过大，已截断，共 {len(content)} 字符]"
        return content
    except Exception as e:
        return f"读取文件错误: {e}"


async def write_file(args: dict) -> str:
    """写入文件内容"""
    path = args.get("path", "")
    content = args.get("content", "")

    if not path:
        return "错误: 缺少 path 参数"

    real_path, error = _ensure_within_workspace(_resolve_workspace_path(path), path)
    if error:
        return error

    try:
        os.makedirs(os.path.dirname(real_path), exist_ok=True)
        with open(real_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"已写入 {path} ({len(content)} 字符)"
    except Exception as e:
        return f"写入文件错误: {e}"


async def list_files(args: dict) -> str:
    """列出目录下的文件和子目录"""
    path = args.get("path", ".")
    pattern = args.get("pattern", "*")

    real_path, error = _ensure_within_workspace(_resolve_workspace_path(path), path)
    if error:
        return error

    if not os.path.isdir(real_path):
        return f"错误: 目录不存在: {path}"

    try:
        import glob as glob_mod
        matches = glob_mod.glob(os.path.join(real_path, pattern), recursive=True)

        # 相对路径显示，限制数量
        results = []
        for m in sorted(matches)[:100]:
            rel = os.path.relpath(m, real_path)
            if os.path.isdir(m):
                rel += "/"
            results.append(rel)

        if not results:
            return f"目录 {path} 下没有匹配 '{pattern}' 的文件"
        return "\n".join(results)
    except Exception as e:
        return f"列出文件错误: {e}"


def register_file_tools(registry):
    """将文件工具注册到工具注册表"""
    registry.register(
        name="readFile",
        description="读取文件内容。返回文件的完整文本。",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径（相对于工作区）",
                },
            },
            "required": ["path"],
        },
        handler=read_file,
        compactable=True,  # 文件读取结果可被微压缩清除
    )

    registry.register(
        name="writeFile",
        description="将内容写入文件。如果文件已存在则覆盖。",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "文件路径（相对于工作区）",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的内容",
                },
            },
            "required": ["path", "content"],
        },
        handler=write_file,
        compactable=True,
    )

    registry.register(
        name="listFiles",
        description="列出目录下的文件和子目录。支持 glob 模式匹配。",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "目录路径（默认工作区根目录）",
                },
                "pattern": {
                    "type": "string",
                    "description": "glob 模式（默认 '*'，支持 '**/*.py' 递归）",
                },
            },
        },
        handler=list_files,
        compactable=True,
    )
