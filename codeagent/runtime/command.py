from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from codeagent.workspace.workspace import WorkspaceContext


class ApprovalDecision(StrEnum):
    APPROVE_ONCE = "approve_once"
    DENY = "deny"


class CommandExecutionStatus(StrEnum):
    POLICY_DENIED = "policy_denied"
    APPROVAL_DENIED = "approval_denied"
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    SPAWN_FAILED = "spawn_failed"
    EXECUTOR_REJECTED = "executor_rejected"


@dataclass(frozen=True)
class CommandSpec:
    command_id: str
    command_kind: str
    argv: tuple[str, ...]
    cwd: str
    timeout_seconds: int

    def to_dict(self) -> dict:
        data = asdict(self)
        data["argv"] = list(self.argv)
        return data


@dataclass(frozen=True)
class CommandArtifactPaths:
    command_dir: Path
    request_path: Path
    result_path: Path
    stdout_path: Path
    stderr_path: Path
    workspace_change_path: Path
    runtime_home: Path
    runtime_temp: Path


@dataclass(frozen=True)
class CommandResult:
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    status: CommandExecutionStatus = CommandExecutionStatus.COMPLETED
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = None
    timed_out: bool = False
    spawn_error: str | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_artifact: str | None = None
    stderr_artifact: str | None = None
    environment_names: tuple[str, ...] = ()
    workspace_changed: bool | None = None
    changed_files: tuple[str, ...] = ()
    workspace_change_artifact: str | None = None
    workspace_audit_error: str | None = None
    git_status_before: str | None = None
    git_status_after: str | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        data["environment_names"] = list(self.environment_names)
        return data


@dataclass(frozen=True)
class CommandReceipt:
    command_spec: CommandSpec
    status: CommandExecutionStatus
    policy_decision: str
    policy_reason: str
    approval_decision: ApprovalDecision | None
    result: CommandResult | None

    def to_dict(self) -> dict:
        return {
            "command_spec": self.command_spec.to_dict(),
            "status": self.status.value,
            "policy_decision": self.policy_decision,
            "policy_reason": self.policy_reason,
            "approval_decision": self.approval_decision.value if self.approval_decision else None,
            "result": self.result.to_dict() if self.result else None,
        }


class CommandArtifactStoreProtocol(Protocol):
    def prepare(self, spec: CommandSpec) -> CommandArtifactPaths:
        ...

    def finalize(self, receipt: CommandReceipt) -> None:
        ...


class CommandExecutor(Protocol):
    def execute(
        self,
        spec: CommandSpec,
        workspace_context: WorkspaceContext,
        artifacts: CommandArtifactPaths | None,
    ) -> CommandResult:
        ...


class FakeCommandExecutor:
    """只返回预配置结果，绝不启动进程。"""

    def __init__(self, result: CommandResult | None = None) -> None:
        self.result = result or CommandResult(exit_code=0, stdout="1 passed")
        self.calls: list[CommandSpec] = []
        self.contexts: list[WorkspaceContext] = []

    def execute(
        self,
        spec: CommandSpec,
        workspace_context: WorkspaceContext,
        artifacts: CommandArtifactPaths | None,
    ) -> CommandResult:
        self.calls.append(spec)
        self.contexts.append(workspace_context)
        return self.result
