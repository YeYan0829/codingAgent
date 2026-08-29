from __future__ import annotations

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
    WORKSPACE_AUDIT_UNAVAILABLE = "workspace_audit_unavailable"


@dataclass(frozen=True)
class CommandRequest:
    execution_id: str
    command: str
    cwd: str
    timeout_seconds: int
    purpose: str
    permissions: tuple[PermissionRequest, ...] = ()
    workspace_revision: int = 0
    base_commit: str | None = None
    before_candidate_revision: int = 0
    before_subject_tree: str | None = None
    command_sha256: str = ""

    def to_dict(self) -> dict:
        return {
            "execution_id": self.execution_id, "command": self.command, "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds, "purpose": self.purpose,
            "permissions": [item.to_dict() for item in self.permissions],
            "workspace_revision": self.workspace_revision, "base_commit": self.base_commit,
            "before_candidate_revision": self.before_candidate_revision,
            "before_subject_tree": self.before_subject_tree, "command_sha256": self.command_sha256,
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
    stdout_artifact: str | None = None
    stderr_artifact: str | None = None
    environment_names: tuple[str, ...] = ()
    payload_started: bool = False
    process_tree_stopped: bool = True
    before_candidate_revision: int | None = None
    after_candidate_revision: int | None = None
    before_subject_tree: str | None = None
    after_subject_tree: str | None = None
    command_induced_changes: tuple[dict[str, str], ...] = ()
    effective_policy: dict | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        data["environment_names"] = list(self.environment_names)
        data["command_induced_changes"] = list(self.command_induced_changes)
        return data


class CommandExecutor(Protocol):
    def execute(self, request: SandboxExecutionRequest, context: WorkspaceContext,
                artifacts: CommandArtifactPaths | None) -> CommandResult: ...
