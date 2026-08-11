from __future__ import annotations

from codeagent.model_gateway.base import ModelTool
from codeagent.tools.base import ToolSpec
from codeagent.workspace.workspace import WorkspaceContext


class ToolRegistry:
    def __init__(self, workspace_context: WorkspaceContext | None = None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self.workspace_context = workspace_context

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

    def register_run_check(self, service: "CommandService") -> None:
        from codeagent.tools.run_check import build_run_check_tool

        self.register(build_run_check_tool(service))

    def register_candidate_tools(self, edit_service: "TextPatchService", candidate_service: "CandidateService") -> None:
        from codeagent.tools.candidate import build_freeze_candidate_tool
        from codeagent.tools.text_patch import build_text_patch_tool

        self.register(build_text_patch_tool(edit_service))
        self.register(build_freeze_candidate_tool(candidate_service))
