from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from codeagent.runtime.approval import ApprovalGate
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.command import CommandExecutionStatus, CommandExecutor, CommandRequest, SandboxExecutionRequest
from codeagent.runtime.permissions import PermissionDecision, PermissionError, PermissionScope, parse_permission_request, recheck_request
from codeagent.runtime.sandbox_policy import SandboxPolicy
from codeagent.safety.path_guard import PathGuardError
from codeagent.session.store import SessionStore
from codeagent.tools.base import ToolResult
from codeagent.workspace.workspace import SessionWorkspaceState, WorkspaceContext


class CommandLimits:
    DEFAULT_TIMEOUT_SECONDS = 120
    MAX_TIMEOUT_SECONDS = 1800
    MAX_COMMAND_CHARS = 32_000
    MAX_PERMISSIONS = 20


class CommandService:
    def __init__(self, context: WorkspaceContext, approval_gate: ApprovalGate, executor: CommandExecutor,
                 session_store: SessionStore, artifact_store: CommandArtifactStore) -> None:
        self.context, self.approval_gate, self.executor = context, approval_gate, executor
        self.store, self.artifacts = session_store, artifact_store

    def environment_snapshot(self) -> dict[str, Any]:
        """返回默认下一条命令的环境；单命令 Approval 后的 effective policy 以结果为准。"""
        policy = self._sandbox_policy()
        effective = policy.effective(self.store.permission_grants(), ())
        provider = getattr(self.executor, "environment_snapshot", None)
        if provider is None:
            return {
                "contract_revision": "command-environment-v1", "backend": type(self.executor).__name__,
                "backend_availability": "unknown", "default_cwd": ".",
                "default_cwd_resolves_to": str(self.context.active_root), "network_mode": effective.network_mode.value,
            }
        return provider(self.context, self.artifacts, effective)

    def run_command(self, arguments: dict[str, Any], *, preapproved_workspace: bool = False) -> ToolResult:
        state = self.store.workspace_state()
        if state == SessionWorkspaceState.RECOVERY_REQUIRED:
            return ToolResult(ok=False, error_code="recovery_required", error="Session需要人工检查，命令能力保持关闭")
        try:
            return self.run_prepared(self.prepare(arguments))
        except (PermissionError, PathGuardError, TypeError, ValueError) as exc:
            return ToolResult(ok=False, error_code=getattr(exc, "code", "invalid_arguments"), error=str(exc))

    def prepare(self, arguments: dict[str, Any]) -> CommandRequest:
        if not isinstance(arguments, dict) or set(arguments) - {"command", "cwd", "timeout_seconds", "purpose", "permissions"} or "command" not in arguments:
            raise ValueError("run_command字段不完整或包含未知字段")
        command = arguments["command"]
        if not isinstance(command, str) or not command.strip() or len(command) > CommandLimits.MAX_COMMAND_CHARS or "\x00" in command:
            raise ValueError("command必须是有界非空字符串")
        cwd_raw = arguments.get("cwd", ".")
        if not isinstance(cwd_raw, str) or Path(cwd_raw).is_absolute() or "\x00" in cwd_raw:
            raise ValueError("cwd必须是当前项目内相对路径")
        cwd = self._resolve_cwd(cwd_raw)
        timeout = arguments.get("timeout_seconds", CommandLimits.DEFAULT_TIMEOUT_SECONDS)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= CommandLimits.MAX_TIMEOUT_SECONDS:
            raise ValueError(f"timeout_seconds必须在1..{CommandLimits.MAX_TIMEOUT_SECONDS}")
        purpose = arguments.get("purpose", "utility")
        if purpose not in {"utility", "validation"}:
            raise ValueError("purpose只支持utility/validation")
        raw_permissions = arguments.get("permissions", [])
        if not isinstance(raw_permissions, list) or len(raw_permissions) > CommandLimits.MAX_PERMISSIONS:
            raise ValueError("permissions必须是有界数组")
        policy = self._sandbox_policy()
        permissions = tuple(parse_permission_request(item, self.context.active_root, policy.hard_deny_paths) for item in raw_permissions)
        return CommandRequest(uuid.uuid4().hex, command, cwd, timeout,
                              purpose, permissions, command_sha256=hashlib.sha256(command.encode()).hexdigest())

    def _resolve_cwd(self, value):
        resolver = getattr(self.executor, "resolve_cwd", None)
        if resolver is not None:
            return resolver(self.context, value)
        path = self.context.path_guard().resolve(value)
        if not path.is_dir():
            raise ValueError("cwd不存在或不是目录")
        return path.relative_to(self.context.active_root).as_posix() or "."

    def _sandbox_policy(self):
        return SandboxPolicy(self.context, self.store, self.artifacts.runtime_root,
                             inspect_workspace=not getattr(self.executor, 'manages_workspace_paths', False))

    def run_prepared(self, request: CommandRequest) -> ToolResult:
        if self.context.denied_paths:
            from codeagent.runtime.sandbox_executor import SandboxedCommandExecutor
            if not isinstance(self.executor, SandboxedCommandExecutor) and not getattr(self.executor, 'supports_denied_paths', False):
                return ToolResult(ok=False, error_code="path_policy_unsupported", error="此命令后端未实现仓库禁止路径隔离，命令未启动")
        runtime_cache = self.artifacts.runtime_root
        runtime_cache.mkdir(parents=True, exist_ok=True)
        policy = self._sandbox_policy()
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
            self._resolve_cwd(request.cwd)
        except PermissionError as exc:
            return ToolResult(ok=False, error_code=exc.code, error=str(exc))
        effective = policy.effective(session_grants, once)
        paths = self.artifacts.prepare(request)
        from codeagent.runtime.access_mounts import denied_mountpoints
        try:
            mounts = (nullcontext() if getattr(self.executor, "manages_workspace_paths", False)
                      else denied_mountpoints(self.context, self.store))
            with mounts:
                result = self.executor.execute(SandboxExecutionRequest(request, effective), self.context, paths)
        except (OSError, ValueError) as exc:
            return ToolResult(ok=False, error_code="path_policy_setup_failed", error=str(exc))
        if result.payload_started and not result.process_tree_stopped:
            self.store.mark_recovery_required("无法确认sandbox command process tree已停止")
        workspace_state = self.store.workspace_state().value
        from codeagent.runtime.validation_output import empty_test_summary, failed_test_summary
        empty_summary = empty_test_summary(result.stdout, result.stderr) if request.purpose == 'validation' else None
        failed_summary = (failed_test_summary(result.stdout, result.stderr)
                          if request.purpose == 'validation' and not empty_summary and result.exit_code == 0 else None)
        validation_issue = 'no_tests' if empty_summary else 'reported_failures' if failed_summary else None
        self.store.append_event("command_completed", _completion_summary(request, result, workspace_state, validation_issue))
        self._finalize_diagnostics(paths, request, result, workspace_state, validation_issue)
        success = (result.status == CommandExecutionStatus.COMPLETED and result.exit_code == 0
                   and workspace_state != SessionWorkspaceState.RECOVERY_REQUIRED.value )
        model_root = self.environment_snapshot()['default_cwd_resolves_to']
        workspace_warning = _workspace_path_warning(request.command, self.context, model_root)
        error_code = None if success else (
            "command_exit_nonzero"
            if result.status == CommandExecutionStatus.COMPLETED and result.exit_code not in {None, 0}
            else result.status.value
        )
        validation_note = ("命令输出报告未运行测试。" if empty_summary else
                           "命令退出码为0，但输出汇总报告了失败。" if failed_summary else None)
        content = _result_content(result, workspace_warning)
        if validation_note:
            content += "\nvalidation: " + validation_note
        return ToolResult(ok=success, content=content,
                          error=None if success else "命令未成功完成", error_code=error_code,
                          truncated=result.stdout_truncated or result.stderr_truncated or result.stdout_preview_truncated or result.stderr_preview_truncated,
                          metadata={"execution_id": request.execution_id, "source_ref": "command:" + request.execution_id,
                                    "stdout_preview_truncated": result.stdout_preview_truncated,
                                    "stderr_preview_truncated": result.stderr_preview_truncated,
                                    "stdout_capture_truncated": result.stdout_truncated,
                                    "stderr_capture_truncated": result.stderr_truncated, "status": result.status.value,
                                    "exit_code": result.exit_code,
                                    "validation_note": validation_note,
                                    "validation_issue": validation_issue,
                                    "effective_policy": effective.summary(), "workspace_state": workspace_state,
                                    "active_workspace": model_root,
                                    "baseline_workspace": str(self.context.source_root),
                                    "initial_cwd": result.launch_cwd or str(Path(model_root) / request.cwd),
                                    "workspace_warning": workspace_warning,
                                    "stdout_tail": _bounded_tail(result.stdout),
                                    "stderr_tail": _bounded_tail(result.stderr),
                                    "diagnostic": result.spawn_error,
                                    **_environment_provenance(result)})

    def _finalize_diagnostics(self, paths, request, result, workspace_state: str, validation_issue=None) -> None:
        # 成功输出同样是后续调查来源；只清理运行期 HOME/TMP，保留已采集日志。
        paths.result_path.write_text(json.dumps({"request": request.to_dict(), "result": result.to_dict()}, ensure_ascii=False, indent=2), encoding="utf-8")
        for target, text, artifact in ((paths.stdout_path, result.stdout, result.stdout_artifact),
                                       (paths.stderr_path, result.stderr, result.stderr_artifact)):
            if artifact is None and target.stat().st_size == 0 and text:
                target.write_text(text, encoding="utf-8")
        if result.process_tree_stopped:
            shutil.rmtree(paths.runtime_home.parent, ignore_errors=True)


def _request_summary(request, evaluations):
    return {"execution_id": request.execution_id, "command": request.command[:CommandLimits.MAX_COMMAND_CHARS],
            "command_sha256": request.command_sha256, "cwd": request.cwd, "purpose": request.purpose,
            "timeout_seconds": request.timeout_seconds,
            "permissions": [{**item.request.to_dict(), "decision": item.decision.value, "policy_reason": item.reason} for item in evaluations]}


def _completion_summary(request, result, workspace_state, validation_issue=None):
    return {"execution_id": request.execution_id, "command": request.command[:CommandLimits.MAX_COMMAND_CHARS],
            "command_sha256": request.command_sha256, "cwd": request.cwd, "purpose": request.purpose,
            "status": result.status.value, "exit_code": result.exit_code, "timed_out": result.timed_out,
            "duration_ms": result.duration_ms, "process_tree_stopped": result.process_tree_stopped,
            "workspace_state": workspace_state, "effective_policy": result.effective_policy,
            "validation_issue": validation_issue,
            "stdout_capture_truncated": result.stdout_truncated, "stderr_capture_truncated": result.stderr_truncated,
            "stdout_tail": _bounded_tail(result.stdout), "stderr_tail": _bounded_tail(result.stderr),
            **_environment_provenance(result)}


def _environment_provenance(result) -> dict:
    return {
        "environment_contract_revision": result.environment_contract_revision,
        "backend": result.backend,
        "backend_availability": result.backend_availability,
        "launch_cwd": result.launch_cwd,
        "environment_names": list(result.environment_names),
        "effective_network_policy": result.effective_network_policy,
        "effective_filesystem_policy": result.effective_filesystem_policy,
    }


def _result_content(result, workspace_warning: str | None = None):
    parts = [f"status: {result.status.value}", f"exit_code: {result.exit_code}"]
    if workspace_warning:
        parts.append(f"workspace_warning: {workspace_warning}")
    if result.stdout:
        parts.append(f"stdout:\n{result.stdout}")
    if result.stderr:
        parts.append(f"stderr:\n{result.stderr}")
    if result.spawn_error:
        parts.append(f"diagnostic: {result.spawn_error}")
    parts.append(f"effective_policy: {json.dumps(result.effective_policy, ensure_ascii=False)}")
    return "\n".join(parts)


def _workspace_path_warning(command: str, context: WorkspaceContext, model_root: str | None = None) -> str | None:
    """提示模型不要在动态升级后回到看不到 Candidate 修改的基线目录。"""
    if context.active_root == context.source_root or str(context.source_root) not in command:
        return None
    if model_root and model_root != str(context.active_root):
        return f"命令引用了基线的宿主路径；当前修改位于 {model_root}，请使用默认 cwd 和工作区内相对路径。"
    return (
        f"命令文本引用了 baseline_workspace {context.source_root}；该目录不包含当前 Candidate 修改。"
        f" 后续读取、编辑和验证应使用默认 cwd 或 active_workspace {context.active_root}。"
    )


def _bounded_tail(value: str, *, max_lines: int = 20, max_chars: int = 1200) -> str:
    """保存确定性的有限输出尾部，供历史 Context 保留实际结果或失败根因。"""
    if not value:
        return ""
    tail = "\n".join(value.splitlines()[-max_lines:])
    if len(tail) <= max_chars:
        return tail
    return "...[tail truncated]\n" + tail[-max_chars:]
