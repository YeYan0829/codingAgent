from __future__ import annotations

from codeagent.model_gateway.base import ModelTool
from codeagent.tools.base import ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, tool: ToolSpec) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")
        return self._tools[name]

    def list_tools(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def as_model_tools(self) -> list[ModelTool]:
        return [tool.to_model_tool() for tool in self.list_tools()]

    def call(self, name: str, arguments: dict) -> object:
        return self.get(name).handler(arguments)
