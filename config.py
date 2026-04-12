"""全局配置。

外部配置统一从项目根目录 `.env` 读取。
派生路径仍在代码中计算，避免把本地目录硬编码进环境变量。
"""
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"

# 启动时优先加载项目内的 `.env`，这样 `uv run main.py` 可以直接生效。
load_dotenv(ENV_FILE)


def _require_env(name: str) -> str:
    """读取必填环境变量，缺失时直接报错。"""
    value = os.getenv(name, "").strip()
    if value:
        return value
    raise RuntimeError(f"缺少必填配置: {name}。请检查 {ENV_FILE}")


def _get_int_env(name: str) -> int:
    """读取整数配置，并在格式错误时给出清晰提示。"""
    raw_value = _require_env(name)
    try:
        return int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"配置 {name} 必须是整数，当前值为: {raw_value}") from exc


# ── LLM ──
OPENAI_API_KEY = _require_env("OPENAI_API_KEY")
OPENAI_API_BASE = _require_env("OPENAI_API_BASE")  
OPENAI_API_MODEL = _require_env("OPENAI_API_MODEL") 
MAX_CONTEXT_TOKENS = _get_int_env("MAX_CONTEXT_TOKENS")

# ── Web Search ──
SEARCH_BACKEND = _require_env("SEARCH_BACKEND").lower()
if SEARCH_BACKEND not in {"tavily", "searxng"}:
    raise RuntimeError(
        f"配置 SEARCH_BACKEND 只支持 'tavily' 或 'searxng'，当前值为: {SEARCH_BACKEND}"
    )

TAVILY_API_KEY = _require_env("TAVILY_API_KEY") if SEARCH_BACKEND == "tavily" else ""
SEARXNG_URL = _require_env("SEARXNG_URL") if SEARCH_BACKEND == "searxng" else ""

# ── 会话存储 ──
def _resolve_data_root() -> str:
    """解析可写的数据目录。

    优先级：
    1. 用户主目录下的 `~/.learning-memory`
    2. 当前项目目录下的 `.learning-memory`
    """
    home_dir = os.path.expanduser("~")
    if os.access(home_dir, os.W_OK):
        return os.path.join(home_dir, ".learning-memory")

    # 某些受限环境不能写入用户目录，这里回退到项目内目录。
    return str(PROJECT_ROOT / ".learning-memory")


DATA_ROOT = _resolve_data_root()
SESSION_DIR = os.path.join(DATA_ROOT, "sessions")
MEMORY_DIR = os.path.join(DATA_ROOT, "memory")
