from __future__ import annotations

import os
import sys
import uuid
from typing import Any

from codeagent.runtime.approval import ApprovalGate
from codeagent.runtime.command import (
    ApprovalDecision,
    CommandArtifactStoreProtocol,
    CommandExecutionStatus,
    CommandExecutor,
    CommandReceipt,
    CommandSpec,
)
from codeagent.runtime.policy import CommandPolicy, PolicyDecision
from codeagent.session.store import SessionStore
from codeagent.tools.base import ToolResult
from codeagent.workspace.workspace import WorkspaceContext


class CommandService:
    def __init__(
        self,
        context: WorkspaceContext,
        policy: CommandPolicy,
        approval_gate: ApprovalGate,
        executor: CommandExecutor,
        session_store: SessionStore,
        artifact_store: CommandArtifactStoreProtocol | None = None,
    ) -> None:
        self.context = context
        self.policy = policy
        self.approval_gate = approval_gate
        self.executor = executor
        self.session_store = session_store
        self.artifact_store = artifact_store

    def run_check(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            spec = self._build_spec(arguments)
        except (TypeError, ValueError) as exc:
            return ToolResult(ok=False, error=f"无效 run_check 参数: {exc}")

        self.session_store.append_event("command_requested", {"command_spec": spec.to_dict()})
        policy_result = self.policy.evaluate(spec, self.context)
        self.session_store.append_event(
            "command_policy_decision",
            {"command_id": spec.command_id, "decision": policy_result.decision.value, "reason": policy_result.reason},
        )
        if policy_result.decision == PolicyDecision.DENY:
            receipt = CommandReceipt(
                spec, CommandExecutionStatus.POLICY_DENIED, policy_result.decision.value, policy_result.reason, None, None
            )
            self._save_receipt(receipt)
            return ToolResult(
                ok=False,
                error=policy_result.reason,
                metadata={"command_id": spec.command_id, "status": receipt.status.value},
            )

        approval = self.approval_gate.request_command(spec)
        self.session_store.append_event(
            "command_approval_decision",
            {"command_id": spec.command_id, "decision": approval.value, "command_spec": spec.to_dict()},
        )
        if approval != ApprovalDecision.APPROVE_ONCE:
            receipt = CommandReceipt(
                spec,
                CommandExecutionStatus.APPROVAL_DENIED,
                policy_result.decision.value,
                policy_result.reason,
                approval,
                None,
            )
            self._save_receipt(receipt)
            return ToolResult(
                ok=False,
                error="用户拒绝执行命令",
                metadata={"command_id": spec.command_id, "status": receipt.status.value},
            )

        artifacts = self.artifact_store.prepare(spec) if self.artifact_store else None
        result = self.executor.execute(spec, self.context, artifacts)
        receipt = CommandReceipt(spec, result.status, policy_result.decision.value, policy_result.reason, approval, result)
        self._save_receipt(receipt)
        stdout, stdout_trimmed = _summarize_text(result.stdout)
        stderr, stderr_trimmed = _summarize_text(result.stderr)
        parts = [f"status: {result.status.value}", f"exit_code: {result.exit_code}"]
        if stdout:
            parts.append(f"stdout:\n{stdout}")
        if stderr:
            parts.append(f"stderr:\n{stderr}")
        if result.spawn_error:
            parts.append(f"spawn_error: {result.spawn_error}")
        successful = result.status == CommandExecutionStatus.COMPLETED and result.exit_code == 0
        return ToolResult(
            ok=successful,
            content="\n".join(parts),
            error=None if successful else "命令未成功完成",
            truncated=result.stdout_truncated or result.stderr_truncated or stdout_trimmed or stderr_trimmed,
            metadata={
                "command_id": spec.command_id,
                "status": result.status.value,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "stdout_artifact": result.stdout_artifact,
                "stderr_artifact": result.stderr_artifact,
                "workspace_changed": result.workspace_changed,
                "changed_files": list(result.changed_files[:20]),
                "preexisting_changed_files": list(result.preexisting_changed_files[:20]),
                "command_introduced_changes": list(result.command_introduced_changes[:20]),
                "workspace_change_artifact": result.workspace_change_artifact,
            },
        )

    def _build_spec(self, arguments: dict[str, Any]) -> CommandSpec:
        kind = str(arguments.get("kind", ""))
        raw_targets = arguments.get("targets", [])
        if not isinstance(raw_targets, list) or not all(isinstance(item, str) for item in raw_targets):
            raise TypeError("targets 必须是字符串列表")
        cwd = _normalize_relative(str(arguments.get("cwd", ".")))
        targets = tuple(_normalize_relative(item) for item in raw_targets)
        timeout = int(arguments.get("timeout_seconds", 120))
        argv = (sys.executable, "-m", "pytest", *targets) if kind == "pytest" else ()
        return CommandSpec(uuid.uuid4().hex, kind, argv, cwd, timeout)

    def _save_receipt(self, receipt: CommandReceipt) -> None:
        self.session_store.append_event("command_receipt", receipt.to_dict())
        if self.artifact_store is not None:
            self.artifact_store.finalize(receipt)


def _normalize_relative(value: str) -> str:
    if not value or "\x00" in value:
        raise ValueError("路径不能为空或包含 NUL")
    return os.path.normpath(value.replace("\\", "/")).replace("\\", "/")


def _summarize_text(value: str, head: int = 700, tail: int = 1300) -> tuple[str, bool]:
    if len(value) <= head + tail:
        return value, False
    return value[:head] + "\n...[middle truncated]...\n" + value[-tail:], True
