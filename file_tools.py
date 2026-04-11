"""文件操作工具 —— readFile / writeFile / listFiles

对应 Claude Code 的 FileReadTool / FileWriteTool / GlobTool。
这三个是 LLM 编程助手的基础能力。
"""
import os
from state import state


async def read_file(args: dict) -> str:
    """读取文件内容"""
    path = args.get("path", "")
    if not path:
        return "错误: 缺少 path 参数"

    # 支持相对路径（相对于 cwd）
    if not os.path.isabs(path):
        path = os.path.join(state.cwd, path)

    # 安全检查：不允许读取 cwd 之外的文件
    real_path = os.path.realpath(path)
    real_cwd = os.path.realpath(state.cwd)
    if not real_path.startswith(real_cwd):
        return f"错误: 不允许读取工作目录之外的文件: {path}"

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

    if not os.path.isabs(path):
        path = os.path.join(state.cwd, path)

    real_path = os.path.realpath(path)
    real_cwd = os.path.realpath(state.cwd)
    if not real_path.startswith(real_cwd):
        return f"错误: 不允许写入工作目录之外的文件: {path}"

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

    if not os.path.isabs(path):
        path = os.path.join(state.cwd, path)

    real_path = os.path.realpath(path)
    real_cwd = os.path.realpath(state.cwd)
    if not real_path.startswith(real_cwd):
        return f"错误: 不允许访问工作目录之外的目录: {path}"

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
                    "description": "文件路径（相对于工作目录）",
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
                    "description": "文件路径（相对于工作目录）",
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
                    "description": "目录路径（默认当前目录）",
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
