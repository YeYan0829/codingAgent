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
    def request_workspace_upgrade(self, tool_name: str, arguments: dict) -> bool: ...
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

    def request_workspace_upgrade(self, tool_name: str, arguments: dict) -> bool:
        if not sys.stdin.isatty():
            return False
        if tool_name == "apply_workspace_edit":
            operations = arguments.get("operations", [])
            targets = []
            for item in operations:
                operation = item.get("op", "unknown")
                if operation == "move_file":
                    target = f"{item.get('source', '<unknown>')} -> {item.get('destination', '<unknown>')}"
                else:
                    target = str(item.get("path", "<unknown>"))
                targets.append(f"{operation}: {target}")
            detail = "\n".join(f"- {target}" for target in targets[:10]) or "- 未提供有效操作"
            action = f"准备修改 {len(operations)} 项文件：\n{detail}"
        else:
            action = (
                "准备在隔离工作区运行命令：\n"
                f"command={arguments.get('command', '')}\n"
                f"cwd={arguments.get('cwd', '.')}\n"
                f"purpose={arguments.get('purpose', 'utility')}"
            )
        try:
            return Confirm.ask(
                "当前 Session 仍为只读。\n"
                f"{action}\n"
                "允许创建 Session 专属隔离工作区，并在其中执行以上操作？",
                default=False,
            )
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

    def request_workspace_upgrade(self, tool_name: str, arguments: dict) -> bool:
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
