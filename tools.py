"""工具注册表 —— 声明式定义工具

对应 Claude Code 的 Tool.ts + tools.ts。
每个工具只需要提供 name / description / parameters / handler，注册表自动生成 OpenAI 格式的 tool 定义。
"""
from typing import Callable, Any
from state import state


class ToolRegistry:
    """工具注册表

    设计思路：
    - 工具以声明式注册（name + schema + handler）
    - 自动生成 OpenAI function calling 格式
    - 为后续微压缩提供可压缩工具白名单
    """

    def __init__(self):
        self._tools: dict[str, dict] = {}       # name → {description, parameters, handler, compactable}
        self._order: list[str] = []              # 注册顺序

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        handler: Callable,
        compactable: bool = False,
    ):
        """注册一个工具

        Args:
            name: 工具名称（如 "readFile"）
            description: 工具描述（LLM 看到的）
            parameters: JSON Schema 格式的参数定义
            handler: async (args: dict) -> str
            compactable: 是否可被微压缩清除（对应 Claude Code 的 COMPACTABLE_TOOLS）
        """
        self._tools[name] = {
            "description": description,
            "parameters": parameters,
            "handler": handler,
            "compactable": compactable,
        }
        if name not in self._order:
            self._order.append(name)

    def get_openai_tools(self) -> list[dict]:
        """生成 OpenAI API 格式的 tools 列表"""
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

    def get_handlers(self) -> dict[str, Callable]:
        """获取 {name: handler} 映射，供 chat_loop 使用"""
        return {name: self._tools[name]["handler"] for name in self._order}

    def get_compactable_tools(self) -> list[str]:
        """获取可压缩工具名称列表（供微压缩使用）"""
        return [name for name in self._order if self._tools[name]["compactable"]]

    def list_tools(self) -> list[str]:
        """列出所有已注册的工具名称"""
        return list(self._order)


# 全局注册表
registry = ToolRegistry()
