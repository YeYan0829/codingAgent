from __future__ import annotations

from typing import Protocol

from rich.prompt import Confirm

from codeagent.model_gateway.base import LLMToolCall
from codeagent.tools.base import ToolSpec


class ApprovalGate(Protocol):
    def request(self, tool: ToolSpec, call: LLMToolCall | None = None) -> bool:
        ...


class ConsoleApprovalGate:
    def request(self, tool: ToolSpec, call: LLMToolCall | None = None) -> bool:
        return Confirm.ask(f"允许执行 {tool.name} ({tool.permission_level})?", default=False)


class AutoApprovalGate:
    def __init__(self, allow: bool = True) -> None:
        self.allow = allow

    def request(self, tool: ToolSpec, call: LLMToolCall | None = None) -> bool:
        return self.allow
