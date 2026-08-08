from __future__ import annotations

import sys
from typing import Protocol

from rich.prompt import Confirm

from codeagent.model_gateway.base import LLMToolCall
from codeagent.runtime.command import ApprovalDecision, CommandSpec
from codeagent.tools.base import ToolSpec
from codeagent.workspace.workspace import WorkspaceContext


class ApprovalGate(Protocol):
    def request(self, tool: ToolSpec, call: LLMToolCall | None = None) -> bool: ...
    def request_command(self, spec: CommandSpec) -> ApprovalDecision: ...


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

    def request_command(self, spec: CommandSpec) -> ApprovalDecision:
        if not sys.stdin.isatty():
            return ApprovalDecision.DENY
        workspace = self.workspace_context.active_root if self.workspace_context else "<unknown>"
        prompt = (
            f"允许执行一次命令？\nkind={spec.command_kind}\nargv={' '.join(spec.argv)}\n"
            f"cwd={spec.cwd}\ntimeout={spec.timeout_seconds}\nactive_workspace={workspace}\n"
            "警告：当前只有 Git worktree 隔离，没有主机级 sandbox 或网络隔离"
        )
        try:
            allowed = Confirm.ask(prompt, default=False)
        except (EOFError, KeyboardInterrupt):
            allowed = False
        return ApprovalDecision.APPROVE_ONCE if allowed else ApprovalDecision.DENY


class AutoApprovalGate:
    def __init__(self, allow: bool = True) -> None:
        self.allow = allow

    def request(self, tool: ToolSpec, call: LLMToolCall | None = None) -> bool:
        return self.allow

    def request_command(self, spec: CommandSpec) -> ApprovalDecision:
        return ApprovalDecision.APPROVE_ONCE if self.allow else ApprovalDecision.DENY


class FakeApprovalGate(AutoApprovalGate):
    def __init__(self, decision: ApprovalDecision = ApprovalDecision.DENY) -> None:
        super().__init__(allow=decision == ApprovalDecision.APPROVE_ONCE)
        self.decision = decision
        self.command_requests: list[CommandSpec] = []

    def request_command(self, spec: CommandSpec) -> ApprovalDecision:
        self.command_requests.append(spec)
        return self.decision
