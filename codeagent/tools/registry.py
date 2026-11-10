from __future__ import annotations

from codeagent.model_gateway.base import ModelTool
from codeagent.tools.base import ToolSpec, PermissionLevel
from codeagent.workspace.workspace import WorkspaceContext


class ToolRegistry:
    def __init__(self, workspace_context: WorkspaceContext | None = None, *,
                 allowed_names: frozenset[str] | None = None, readonly: bool = False,
                 workspace_backend=None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self.workspace_context = workspace_context
        self.allowed_names, self.readonly = allowed_names, readonly
        self.workspace_backend = workspace_backend

    def register(self, tool: ToolSpec) -> None:
        if self.allowed_names is not None and tool.name not in self.allowed_names:
            raise ValueError("tool is outside registry allowlist: " + tool.name)
        if self.readonly and tool.permission_level != PermissionLevel.READ:
            raise ValueError("readonly registry rejects tool: " + tool.name)
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
