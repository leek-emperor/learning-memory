"""Web Search 工具 —— 支持 SearXNG（免费自建）和 Tavily（免费额度）两种后端

对应 Claude Code 的 WebSearchTool。
设计思路：统一接口，两种后端实现同一个 search_web() 函数。
"""
import json
from typing import Optional
from config import SEARCH_BACKEND, TAVILY_API_KEY, SEARXNG_URL


async def search_searxng(query: str, max_results: int = 5) -> str:
    """SearXNG 后端 —— 免费、无限次、需自建

    部署方式: docker run -d -p 8888:8080 searxng/searxng
    """
    import httpx

    url = f"{SEARXNG_URL}/search"
    params = {
        "q": query,
        "format": "json",
        "categories": "general",
        "language": "zh-CN",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        results = []
        for item in data.get("results", [])[:max_results]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
            })

        if not results:
            return f"未找到与 '{query}' 相关的结果"

        # 格式化输出
        output = []
        for i, r in enumerate(results, 1):
            output.append(f"{i}. {r['title']}")
            output.append(f"   {r['url']}")
            if r["snippet"]:
                output.append(f"   {r['snippet']}")
            output.append("")

        return "\n".join(output)

    except httpx.ConnectError:
        return (
            f"无法连接 SearXNG ({SEARXNG_URL})。\n"
            "请先启动: docker run -d -p 8888:8080 searxng/searxng\n"
            "或切换到 Tavily: export SEARCH_BACKEND=tavily"
        )
    except Exception as e:
        return f"SearXNG 搜索错误: {e}"


async def search_tavily(query: str, max_results: int = 5) -> str:
    """Tavily 后端 —— 免费 1000 次/月，需 API Key

    注册: https://tavily.com → 获取 API Key
    """
    import httpx

    if not TAVILY_API_KEY:
        return (
            "Tavily API Key 未设置。\n"
            "请: export TAVILY_API_KEY=tvly-xxxxxxxx\n"
            "注册: https://tavily.com"
        )

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "query": query,
                    "max_results": max_results,
                    "include_answer": True,
                },
                headers={"Authorization": f"Bearer {TAVILY_API_KEY}"},
            )
            resp.raise_for_status()
            data = resp.json()

        # Tavily 的 answer 字段是 AI 生成的摘要
        answer = data.get("answer", "")
        results = data.get("results", [])

        output = []
        if answer:
            output.append(f"摘要: {answer}")
            output.append("")

        for i, r in enumerate(results, 1):
            output.append(f"{i}. {r.get('title', '')}")
            output.append(f"   {r.get('url', '')}")
            if r.get("content"):
                output.append(f"   {r['content'][:200]}")
            output.append("")

        if not output:
            return f"未找到与 '{query}' 相关的结果"

        return "\n".join(output)

    except Exception as e:
        return f"Tavily 搜索错误: {e}"


async def search_web(query: str, max_results: int = 5) -> str:
    """统一搜索接口 —— 根据 SEARCH_BACKEND 配置选择后端"""
    if SEARCH_BACKEND == "searxng":
        return await search_searxng(query, max_results)
    else:
        return await search_tavily(query, max_results)


async def web_search_handler(args: dict) -> str:
    """工具 handler：供 ToolRegistry 调用"""
    query = args.get("query", "")
    if not query:
        return "错误: 缺少 query 参数"
    return await search_web(query)


def register_search_tool(registry):
    """将 Web Search 工具注册到工具注册表"""
    backend_name = "SearXNG" if SEARCH_BACKEND == "searxng" else "Tavily"
    registry.register(
        name="webSearch",
        description=f"搜索互联网获取最新信息。当前使用 {backend_name} 后端。",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词",
                },
            },
            "required": ["query"],
        },
        handler=web_search_handler,
        compactable=True,  # 搜索结果可被微压缩清除
    )
