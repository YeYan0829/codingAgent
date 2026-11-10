from __future__ import annotations

import sys
from typing import Protocol

from rich.prompt import Confirm

from codeagent.model_gateway.base import LLMToolCall
from codeagent.runtime.command import CommandRequest
from codeagent.runtime.sandbox_policy import PermissionEvaluation
from codeagent.tools.base import ToolSpec
from codeagent.workspace.workspace import WorkspaceContext


class ApprovalGate(Protocol):
    def request(self, tool: ToolSpec, call: LLMToolCall | None = None) -> bool: ...
    def request_permissions(self, command: CommandRequest, evaluations: tuple[PermissionEvaluation, ...]) -> bool: ...


class ConsoleApprovalGate:
    def __init__(self, workspace_context: WorkspaceContext | None = None) -> None:
        self.workspace_context = workspace_context

    def request(self, tool: ToolSpec, call: LLMToolCall | None = None) -> bool:
        if not sys.stdin.isatty():
            return False
        try:
            return Confirm.ask(f"允许执行 {tool.name} ({tool.permission_level})?", default=False)
        except (EOFError, KeyboardInterrupt):
            return False

    def request_permissions(self, command: CommandRequest, evaluations: tuple[PermissionEvaluation, ...]) -> bool:
        if not sys.stdin.isatty():
            return False
        details = "\n".join(
            f"- {item.request.capability.value}: requested={item.request.requested_resource} "
            f"resolved={item.request.resolved_resource} scope={item.request.scope.value} reason={item.request.reason}"
            for item in evaluations
        )
        try:
            return Confirm.ask(
                f"允许命令的增量资源权限？\ncommand={command.command}\ncwd={command.cwd}\n{details}", default=False,
            )
        except (EOFError, KeyboardInterrupt):
            return False


class AutoApprovalGate:
    def __init__(self, allow: bool = True) -> None:
        self.allow = allow

    def request(self, tool: ToolSpec, call: LLMToolCall | None = None) -> bool:
        return self.allow

    def request_permissions(self, command: CommandRequest, evaluations: tuple[PermissionEvaluation, ...]) -> bool:
        return self.allow


class FakeApprovalGate(AutoApprovalGate):
    def __init__(self, allow: bool = False) -> None:
        super().__init__(allow=allow)
        self.permission_requests: list[tuple[CommandRequest, tuple[PermissionEvaluation, ...]]] = []

    def request_permissions(self, command: CommandRequest, evaluations: tuple[PermissionEvaluation, ...]) -> bool:
        self.permission_requests.append((command, evaluations))
        return self.allow
