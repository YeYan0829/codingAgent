"""Runtime 公开接口；惰性导出避免底层模块之间形成导入环。"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "AgentRunner": ("codeagent.runtime.runner", "AgentRunner"),
    "RunnerOutput": ("codeagent.runtime.runner", "RunnerOutput"),
    "AutoApprovalGate": ("codeagent.runtime.approval", "AutoApprovalGate"),
    "ConsoleApprovalGate": ("codeagent.runtime.approval", "ConsoleApprovalGate"),
    "CommandArtifactStore": ("codeagent.runtime.artifacts", "CommandArtifactStore"),
    "CommandExecutionStatus": ("codeagent.runtime.command", "CommandExecutionStatus"),
    "CommandExecutor": ("codeagent.runtime.command", "CommandExecutor"),
    "CommandRequest": ("codeagent.runtime.command", "CommandRequest"),
    "CommandResult": ("codeagent.runtime.command", "CommandResult"),
    "CommandService": ("codeagent.runtime.command_service", "CommandService"),
    "DefaultPolicy": ("codeagent.runtime.policy", "DefaultPolicy"),
    "PolicyDecision": ("codeagent.runtime.policy", "PolicyDecision"),
    "PolicyResult": ("codeagent.runtime.policy", "PolicyResult"),
    "SandboxedCommandExecutor": ("codeagent.runtime.sandbox_executor", "SandboxedCommandExecutor"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
