from __future__ import annotations

from contextvars import ContextVar
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from codeagent.runtime.permissions import EffectiveSandboxPolicy, PermissionRequest
from codeagent.workspace.workspace import WorkspaceContext


class CommandExecutionStatus(StrEnum):
    COMPLETED = "completed"
    TIMED_OUT = "execution_timed_out"
    SPAWN_FAILED = "spawn_failed"
    EXECUTOR_REJECTED = "executor_rejected"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"
    SANDBOX_SETUP_FAILED = "sandbox_setup_failed"
    CANCELLED = "execution_cancelled"


@dataclass(frozen=True)
class CommandRequest:
    execution_id: str
    command: str
    cwd: str
    timeout_seconds: int
    purpose: str
    permissions: tuple[PermissionRequest, ...] = ()
    command_sha256: str = ""

    def to_dict(self) -> dict:
        return {
            "execution_id": self.execution_id, "command": self.command, "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds, "purpose": self.purpose,
            "permissions": [item.to_dict() for item in self.permissions],
            "command_sha256": self.command_sha256,
        }


@dataclass(frozen=True)
class SandboxExecutionRequest:
    command: CommandRequest
    policy: EffectiveSandboxPolicy


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
    stdout_preview_truncated: bool = False
    stderr_preview_truncated: bool = False
    stdout_artifact: str | None = None
    stderr_artifact: str | None = None
    environment_names: tuple[str, ...] = ()
    payload_started: bool = False
    process_tree_stopped: bool = True
    effective_policy: dict | None = None
    environment_contract_revision: str | None = None
    backend: str | None = None
    backend_availability: str | None = None
    launch_cwd: str | None = None
    effective_network_policy: str | None = None
    effective_filesystem_policy: dict | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        data["environment_names"] = list(self.environment_names)
        return data


class CommandExecutor(Protocol):
    def execute(self, request: SandboxExecutionRequest, context: WorkspaceContext,
                artifacts: CommandArtifactPaths | None) -> CommandResult: ...


_COMMAND_CANCELLATIONS = ContextVar("codeagent_command_cancellations", default=())


def command_cancellation(default):
    """绑定本次调用的取消信号；不复制容器等执行器状态，也不覆盖父方callback。"""
    callbacks = _COMMAND_CANCELLATIONS.get()
    return lambda: default() or any(callback() for callback in callbacks)


class ScopedCommandExecutor:
    def __init__(self, executor, cancelled):
        self.executor, self.cancelled = executor, cancelled

    def __getattr__(self, name):
        return getattr(self.executor, name)

    def execute(self, request, context, artifacts):
        token = _COMMAND_CANCELLATIONS.set((*_COMMAND_CANCELLATIONS.get(), self.cancelled))
        try:
            return self.executor.execute(request, context, artifacts)
        finally:
            _COMMAND_CANCELLATIONS.reset(token)
