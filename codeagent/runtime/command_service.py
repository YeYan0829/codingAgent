from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from codeagent.runtime.approval import ApprovalGate
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.command import CommandExecutionStatus, CommandRequest, SandboxExecutionRequest
from codeagent.runtime.permissions import PermissionDecision, PermissionError, PermissionScope, parse_permission_request, recheck_request
from codeagent.runtime.sandbox_executor import SandboxedCommandExecutor
from codeagent.runtime.sandbox_policy import SandboxPolicy
from codeagent.runtime.workspace_audit import WorkspaceAuditError, compare_workspace, take_workspace_snapshot
from codeagent.safety.path_guard import PathGuard, PathGuardError
from codeagent.session.store import SessionStore
from codeagent.tools.base import ToolResult
from codeagent.workspace.workspace import SessionWorkspaceState, WorkspaceContext


class CommandLimits:
    DEFAULT_TIMEOUT_SECONDS = 120
    MAX_TIMEOUT_SECONDS = 1800
    MAX_COMMAND_CHARS = 32_000
    MAX_PERMISSIONS = 20


class CommandService:
    def __init__(self, context: WorkspaceContext, approval_gate: ApprovalGate, executor: SandboxedCommandExecutor,
                 session_store: SessionStore, artifact_store: CommandArtifactStore) -> None:
        self.context, self.approval_gate, self.executor = context, approval_gate, executor
        self.store, self.artifacts = session_store, artifact_store

    def run_command(self, arguments: dict[str, Any], *, preapproved_workspace: bool = False) -> ToolResult:
        state = self.store.workspace_state()
        if state == SessionWorkspaceState.RECOVERY_REQUIRED:
            return ToolResult(ok=False, error_code="recovery_required", error="Session需要人工检查，命令能力保持关闭")
        if state == SessionWorkspaceState.WORKSPACE_TAINTED:
            return ToolResult(ok=False, error_code="workspace_tainted", error="workspace audit边界不可信；只允许只读检查和discard")
        try:
            return self.run_prepared(self.prepare(arguments))
        except (PermissionError, PathGuardError, WorkspaceAuditError, TypeError, ValueError) as exc:
            return ToolResult(ok=False, error_code=getattr(exc, "code", "invalid_arguments"), error=str(exc))

    def prepare(self, arguments: dict[str, Any]) -> CommandRequest:
        if self.context.workspace_kind != "git_worktree" or not self.context.base_commit:
            raise ValueError("命令只能在Session Candidate worktree中执行")
        if not isinstance(arguments, dict) or set(arguments) - {"command", "cwd", "timeout_seconds", "purpose", "permissions"} or "command" not in arguments:
            raise ValueError("run_command字段不完整或包含未知字段")
        command = arguments["command"]
        if not isinstance(command, str) or not command.strip() or len(command) > CommandLimits.MAX_COMMAND_CHARS or "\x00" in command:
            raise ValueError("command必须是有界非空字符串")
        cwd_raw = arguments.get("cwd", ".")
        if not isinstance(cwd_raw, str) or Path(cwd_raw).is_absolute() or "\x00" in cwd_raw:
            raise ValueError("cwd必须是Candidate内相对路径")
        cwd = PathGuard(self.context.active_root).resolve(cwd_raw, reject_sensitive=False)
        if not cwd.is_dir():
            raise ValueError("cwd不存在或不是目录")
        timeout = arguments.get("timeout_seconds", CommandLimits.DEFAULT_TIMEOUT_SECONDS)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= CommandLimits.MAX_TIMEOUT_SECONDS:
            raise ValueError(f"timeout_seconds必须在1..{CommandLimits.MAX_TIMEOUT_SECONDS}")
        purpose = arguments.get("purpose", "utility")
        if purpose not in {"utility", "validation"}:
            raise ValueError("purpose只支持utility/validation")
        raw_permissions = arguments.get("permissions", [])
        if not isinstance(raw_permissions, list) or len(raw_permissions) > CommandLimits.MAX_PERMISSIONS:
            raise ValueError("permissions必须是有界数组")
        policy = SandboxPolicy(self.context, self.store, self.artifacts.runtime_root)
        permissions = tuple(parse_permission_request(item, self.context.active_root, policy.hard_deny_paths) for item in raw_permissions)
        before = take_workspace_snapshot(self.context.active_root, self.context.base_commit)
        revision = int(self.store.read_meta().get("candidate_revision", 0))
        execution_id = uuid.uuid4().hex
        return CommandRequest(execution_id, command, cwd.relative_to(self.context.active_root).as_posix() or ".", timeout,
                              purpose, permissions, self.context.workspace_revision, self.context.base_commit, revision,
                              before.subject_tree, hashlib.sha256(command.encode()).hexdigest())

    def run_prepared(self, request: CommandRequest) -> ToolResult:
        runtime_cache = self.artifacts.runtime_root
        runtime_cache.mkdir(parents=True, exist_ok=True)
        policy = SandboxPolicy(self.context, self.store, runtime_cache)
        session_grants = self.store.permission_grants()
        evaluations = tuple(policy.evaluate(item, session_grants) for item in request.permissions)
        self.store.append_event("command_requested", _request_summary(request, evaluations))
        denied = next((item for item in evaluations if item.decision == PermissionDecision.DENY), None)
        if denied:
            return ToolResult(ok=False, error_code="permission_denied", error=denied.reason)
        asks = tuple(item for item in evaluations if item.decision == PermissionDecision.ASK)
        if asks and not self.approval_gate.request_permissions(request, asks):
            self.store.append_event("permission_denied", {"execution_id": request.execution_id, "kind": "approval"})
            return ToolResult(ok=False, error_code="approval_denied", error="用户拒绝增量资源权限")
        grants = tuple(policy.grant(item.request, request.execution_id) for item in evaluations if item.decision in {PermissionDecision.ALLOW, PermissionDecision.ASK})
        session_new = tuple(item for item in grants if item.scope == PermissionScope.SESSION and item not in session_grants)
        if session_new:
            self.store.add_permission_grants(session_new)
            session_grants = self.store.permission_grants()
        once = tuple(item for item in grants if item.scope == PermissionScope.ONCE)
        try:
            for item in request.permissions:
                recheck_request(item, self.context.active_root)
            before = take_workspace_snapshot(self.context.active_root, self.context.base_commit or "HEAD")
            if before.subject_tree != request.before_subject_tree:
                return ToolResult(ok=False, error_code="command_request_stale", error="审批前Candidate已变化，请提交新命令")
        except (PermissionError, WorkspaceAuditError) as exc:
            return ToolResult(ok=False, error_code=getattr(exc, "code", "workspace_audit_unavailable"), error=str(exc))
        effective = policy.effective(session_grants, once)
        paths = self.artifacts.prepare(request)
        result = self.executor.execute(SandboxExecutionRequest(request, effective), self.context, paths)
        after_revision, after_tree, audit_changes = request.before_candidate_revision, request.before_subject_tree, ()
        if result.payload_started:
            if not result.process_tree_stopped:
                self.store.mark_recovery_required("无法确认sandbox command process tree已停止")
            else:
                try:
                    after = take_workspace_snapshot(self.context.active_root, self.context.base_commit or "HEAD")
                    audit = compare_workspace(before, after)
                    after_revision = self.store.next_candidate_revision()
                    after_tree, audit_changes = audit.after_subject_tree, audit.changes
                except WorkspaceAuditError as exc:
                    self.store.mark_workspace_tainted(f"command after audit失败: {exc}", [])
        result = replace(result, before_candidate_revision=request.before_candidate_revision,
                         after_candidate_revision=after_revision, before_subject_tree=request.before_subject_tree,
                         after_subject_tree=after_tree, command_induced_changes=audit_changes)
        workspace_state = self.store.workspace_state().value
        self.store.append_event("command_completed", _completion_summary(request, result, workspace_state))
        if request.purpose == "validation" and _valid_evidence(result, workspace_state):
            self.store.append_event("validation_completed", {
                "validation_id": uuid.uuid4().hex, "execution_id": request.execution_id,
                "command": request.command[:CommandLimits.MAX_COMMAND_CHARS], "command_sha256": request.command_sha256,
                "candidate_revision": after_revision, "subject_tree": after_tree,
                "workspace_revision": request.workspace_revision, "base_commit": request.base_commit,
                "policy_revision": effective.policy_revision, "status": "passed", "exit_code": 0,
            })
        self._finalize_diagnostics(paths, request, result, workspace_state)
        success = result.status == CommandExecutionStatus.COMPLETED and result.exit_code == 0 and workspace_state == SessionWorkspaceState.CHANGES_ACTIVE.value
        return ToolResult(ok=success, content=_result_content(result), error=None if success else "命令未成功完成",
                          error_code=None if success else result.status.value,
                          truncated=result.stdout_truncated or result.stderr_truncated,
                          metadata={"execution_id": request.execution_id, "status": result.status.value,
                                    "exit_code": result.exit_code, "before_candidate_revision": request.before_candidate_revision,
                                    "after_candidate_revision": after_revision, "command_induced_changes": list(audit_changes),
                                    "effective_policy": effective.summary(), "workspace_state": workspace_state})

    def _finalize_diagnostics(self, paths, request, result, workspace_state: str) -> None:
        keep = result.status != CommandExecutionStatus.COMPLETED or result.exit_code != 0 or workspace_state != SessionWorkspaceState.CHANGES_ACTIVE.value
        if keep:
            paths.result_path.write_text(json.dumps({"request": request.to_dict(), "result": result.to_dict()}, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            shutil.rmtree(paths.command_dir, ignore_errors=True)
        shutil.rmtree(paths.runtime_home.parent, ignore_errors=True)


def _request_summary(request, evaluations):
    return {"execution_id": request.execution_id, "command": request.command[:CommandLimits.MAX_COMMAND_CHARS],
            "command_sha256": request.command_sha256, "cwd": request.cwd, "purpose": request.purpose,
            "timeout_seconds": request.timeout_seconds, "before_candidate_revision": request.before_candidate_revision,
            "before_subject_tree": request.before_subject_tree,
            "permissions": [{**item.request.to_dict(), "decision": item.decision.value, "policy_reason": item.reason} for item in evaluations]}


def _completion_summary(request, result, workspace_state):
    return {"execution_id": request.execution_id, "command": request.command[:CommandLimits.MAX_COMMAND_CHARS],
            "command_sha256": request.command_sha256, "cwd": request.cwd, "purpose": request.purpose,
            "status": result.status.value, "exit_code": result.exit_code, "timed_out": result.timed_out,
            "duration_ms": result.duration_ms, "before_candidate_revision": result.before_candidate_revision,
            "after_candidate_revision": result.after_candidate_revision, "before_subject_tree": result.before_subject_tree,
            "after_subject_tree": result.after_subject_tree, "changed_paths": list(result.command_induced_changes)[:100],
            "audit_complete": workspace_state == SessionWorkspaceState.CHANGES_ACTIVE.value,
            "workspace_state": workspace_state, "effective_policy": result.effective_policy}


def _valid_evidence(result, workspace_state):
    return result.status == CommandExecutionStatus.COMPLETED and result.exit_code == 0 and result.payload_started and result.process_tree_stopped and workspace_state == SessionWorkspaceState.CHANGES_ACTIVE.value


def _result_content(result):
    parts = [f"status: {result.status.value}", f"exit_code: {result.exit_code}"]
    if result.stdout:
        parts.append(f"stdout:\n{result.stdout}")
    if result.stderr:
        parts.append(f"stderr:\n{result.stderr}")
    if result.spawn_error:
        parts.append(f"diagnostic: {result.spawn_error}")
    parts.append(f"effective_policy: {json.dumps(result.effective_policy, ensure_ascii=False)}")
    return "\n".join(parts)
