from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.context.builder import ContextBuilder
from codeagent.model_gateway.base import BaseModelClient, LLMToolCall, MalformedToolArgumentsError, ModelRequest
from codeagent.runtime.approval import ApprovalGate
from codeagent.runtime.policy import DefaultPolicy, PolicyDecision
from codeagent.session.store import SessionStore
from codeagent.tools.base import ToolResult, ToolSpec
from codeagent.tools.registry import ToolRegistry
from codeagent.workspace.workspace import WorkspaceContext
from codeagent.runtime.command import CommandExecutor
from codeagent.runtime.command_service import CommandService
from codeagent.runtime.candidate import CandidateService
from codeagent.runtime.atomic_edit import AtomicEditService
from codeagent.runtime.sandbox_executor import SandboxedCommandExecutor
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.tools.fs_read import build_fs_tools
from codeagent.tools.git_read import build_git_tools
from codeagent.tools.atomic_edit import build_atomic_edit_tool
from codeagent.tools.command import build_command_tool, command_tool_schema
from codeagent.workspace.git_worktree import GitWorktreeError, GitWorktreeManager
from codeagent.workspace.workspace import SessionWorkspaceState


@dataclass
class RunnerOutput:
    final_text: str
    steps: list[dict] = field(default_factory=list)


class AgentRunner:
    def __init__(
        self,
        session_store: SessionStore,
        model: BaseModelClient,
        tools: ToolRegistry,
        approval_gate: ApprovalGate,
        policy: DefaultPolicy | None = None,
        config: RuntimeConfig | None = None,
        model_config: ModelConfig | None = None,
        workspace_context: WorkspaceContext | None = None,
        command_executor: CommandExecutor | None = None,
        command_artifact_store: CommandArtifactStore | None = None,
        dynamic_workspace: bool = False,
        progress_callback: Callable[[dict], None] | None = None,
    ) -> None:
        self.session_store = session_store
        self.model = model
        self.tools = tools
        self.approval_gate = approval_gate
        self.policy = policy or DefaultPolicy()
        self.config = config or RuntimeConfig()
        self.model_config = model_config or ModelConfig()
        self.workspace_context = workspace_context or tools.workspace_context
        self.command_service: CommandService | None = None
        self.atomic_edit_service: AtomicEditService | None = None
        self.candidate_service: CandidateService | None = None
        self.dynamic_workspace = dynamic_workspace
        self.command_executor = command_executor
        self.command_artifact_store = command_artifact_store
        self.progress_callback = progress_callback
        self._workspace_upgrade_denied_this_turn = False
        if command_executor is not None:
            if self.workspace_context is None:
                raise ValueError("command executor requires WorkspaceContext")
            artifacts = command_artifact_store if isinstance(command_artifact_store, CommandArtifactStore) else CommandArtifactStore(session_store)
            self.command_service = CommandService(self.workspace_context, approval_gate, command_executor, session_store, artifacts)
            self.tools.register(build_command_tool(self.command_service))
            self.atomic_edit_service = AtomicEditService(self.workspace_context, session_store)
            self.candidate_service = CandidateService(self.workspace_context, session_store)
            self.tools.register(build_atomic_edit_tool(self.atomic_edit_service))
        elif dynamic_workspace:
            self._register_upgrade_tools()

    def run_turn(self, message: str) -> RunnerOutput:
        self.session_store.append_event("user_message", {"message": message})
        steps: list[dict] = []
        self._workspace_upgrade_denied_this_turn = False
        repair_messages: list[dict] = []
        protocol_failures = 0
        previous_failure: tuple[str, str] | None = None

        for _ in range(self.config.max_steps_per_turn):
            request = ModelRequest(
                messages=ContextBuilder(self.session_store).build() + repair_messages,
                tools=self.tools.as_model_tools(),
                model=self.model_config.resolved_model,
                temperature=self.model_config.temperature,
                max_tokens=self.model_config.max_tokens,
            )
            try:
                response = self.model.complete(request)
            except MalformedToolArgumentsError as exc:
                protocol_failures += 1
                fingerprint = (exc.tool_name, exc.raw_arguments)
                self.session_store.append_event("model_protocol_error", {
                    "tool": exc.tool_name, "message": exc.message, "line": exc.line,
                    "column": exc.column, "attempt": protocol_failures,
                })
                if protocol_failures > 3 or fingerprint == previous_failure:
                    final = (
                        f"模型连续返回无法解析的 {exc.tool_name} 参数；工具未执行，workspace 未修改。"
                    )
                    self.session_store.append_event("assistant_message", {"message": final})
                    return RunnerOutput(final_text=final, steps=steps)
                previous_failure = fingerprint
                repair_messages.append({"role": "system", "content": (
                    f"上一响应中 {exc.tool_name} 的 arguments 不是合法 JSON：{exc.message} "
                    f"(line {exc.line}, column {exc.column})。工具尚未执行，workspace 未发生变化。"
                    "请依据工具 schema 重新生成完整工具调用，并确保所有字符串使用合法 JSON 转义。"
                )})
                continue
            if not response.tool_calls:
                final = response.text or ""
                self.session_store.append_event("assistant_message", {"message": final})
                return RunnerOutput(final_text=final, steps=steps)

            tool_payload = {
                "message": response.text or "",
                "tool_calls": [call.model_dump() for call in response.tool_calls],
            }
            self.session_store.append_event("assistant_tool_calls", tool_payload)
            if response.text:
                self._notify({"type": "assistant_progress", "message": response.text})
            for call in response.tool_calls:
                step = self._handle_tool_call(call)
                steps.append(step)
                self._notify({"type": "tool_step", "step": step})

        final = "本轮达到最大模型步骤数，建议缩小任务范围或继续下一轮。"
        self.session_store.append_event("assistant_message", {"message": final})
        return RunnerOutput(final_text=final, steps=steps)

    def _notify(self, event: dict) -> None:
        if self.progress_callback is not None:
            self.progress_callback(event)

    def _handle_tool_call(self, call: LLMToolCall) -> dict:
        self.session_store.append_event("tool_requested", call.model_dump())
        try:
            tool = self.tools.get(call.name)
        except KeyError as exc:
            result = ToolResult(ok=False, error=str(exc), error_code="invalid_arguments")
            payload = {"call_id": call.call_id, "name": call.name, "result": result.model_dump()}
            self.session_store.append_event("tool_denied", payload)
            return self._step(call, "deny", result)

        policy_result = self.policy.evaluate(tool)
        if policy_result.decision == PolicyDecision.DENY:
            result = ToolResult(ok=False, error=policy_result.reason, error_code="policy_denied")
            self.session_store.append_event("tool_denied", {"call_id": call.call_id, "name": call.name, "reason": policy_result.reason})
            return self._step(call, "deny", result)

        if policy_result.decision == PolicyDecision.ASK:
            self.session_store.append_event("approval_requested", {"call_id": call.call_id, "name": call.name, "reason": policy_result.reason})
            allowed = self.approval_gate.request(tool, call)
            self.session_store.append_event("approval_decision", {"call_id": call.call_id, "name": call.name, "allowed": allowed})
            if not allowed:
                result = ToolResult(ok=False, error="user denied approval", error_code="approval_denied")
                self.session_store.append_event("tool_denied", {"call_id": call.call_id, "name": call.name, "reason": "user denied approval"})
                return self._step(call, "deny", result)

        result = tool.handler(call.arguments)
        result_payload = result.model_dump()
        self.session_store.append_event(
            "tool_result",
            {"call_id": call.call_id, "name": call.name, "arguments": call.arguments, "content": json.dumps(result_payload, ensure_ascii=False), "result": result_payload},
        )
        return self._step(call, policy_result.decision.value, result)

    def _step(self, call: LLMToolCall, decision: str, result: ToolResult) -> dict:
        return {
            "tool": call.name,
            "arguments": call.arguments,
            "decision": decision,
            "result": result.model_dump(),
        }

    def _register_upgrade_tools(self) -> None:
        atomic = build_atomic_edit_tool(AtomicEditService(self.workspace_context, self.session_store))
        command = ToolSpec("run_command", "在Candidate的Linux沙盒中运行任意shell命令；额外资源必须由Agent显式申请。",
                           atomic.permission_level, command_tool_schema(), lambda _: ToolResult(ok=False))
        self.tools.register(ToolSpec(atomic.name, atomic.description, atomic.permission_level, atomic.schema,
                                     lambda args: self._upgrade_and_dispatch(atomic.name, args)))
        self.tools.register(ToolSpec(command.name, command.description, command.permission_level, command.schema,
                                     lambda args: self._upgrade_and_dispatch(command.name, args)))

    def _upgrade_and_dispatch(self, tool_name: str, arguments: dict) -> ToolResult:
        if self.session_store.workspace_state() == SessionWorkspaceState.RECOVERY_REQUIRED:
            return ToolResult(ok=False, error_code="recovery_required", error="Session 需要人工检查，受保护能力保持关闭")
        if self._workspace_upgrade_denied_this_turn:
            return ToolResult(
                ok=False,
                error_code="approval_denied",
                error="本轮创建隔离 Agent worktree 的请求已被拒绝；不再重复询问",
            )
        self.session_store.append_event("approval_requested", {"name": tool_name, "reason": "创建隔离 Agent worktree"})
        allowed = self.approval_gate.request_workspace_upgrade(tool_name, arguments)
        self.session_store.append_event("approval_decision", {"name": tool_name, "allowed": allowed, "reason": "创建隔离 Agent worktree"})
        if not allowed:
            self._workspace_upgrade_denied_this_turn = True
            return ToolResult(ok=False, error_code="approval_denied", error="用户拒绝创建隔离 Agent worktree；Session 保持只读")

        meta = self.session_store.read_meta()
        revision = int(meta.get("workspace_revision", 0)) + 1
        manager = GitWorktreeManager(self.session_store.session_root)
        context = None
        previous_approval_context = getattr(self.approval_gate, "workspace_context", None)
        try:
            self.session_store.begin_workspace_upgrade(revision)
            context = manager.create(self.session_store.workspace_root, self.session_store.session_id, revision)
            if hasattr(self.approval_gate, "workspace_context"):
                self.approval_gate.workspace_context = context
            registry, command_service, edit_service, candidate_service = self._build_active_bundle(context)
            self.session_store.activate_workspace(context)
            self.workspace_context = context
            self.tools = registry
            self.command_service = command_service
            self.atomic_edit_service = edit_service
            self.candidate_service = candidate_service
        except Exception as exc:
            if hasattr(self.approval_gate, "workspace_context"):
                self.approval_gate.workspace_context = previous_approval_context
            if context is not None:
                try:
                    manager.discard(context)
                except GitWorktreeError:
                    self.session_store.mark_recovery_required(f"workspace upgrade 回滚失败: {exc}")
                    return ToolResult(ok=False, error_code="recovery_required", error="创建 Agent worktree 失败且无法确认清理")
            self.session_store.cancel_workspace_upgrade(str(exc))
            code = "approval_denied" if isinstance(exc, _UpgradeApprovalDenied) else "workspace_upgrade_failed"
            return ToolResult(ok=False, error_code=code, error=str(exc) if code == "approval_denied" else f"无法创建 Agent worktree: {exc}")
        return self.tools.get(tool_name).handler(arguments)

    def _build_active_bundle(self, context: WorkspaceContext) -> tuple[ToolRegistry, CommandService, AtomicEditService, CandidateService]:
        registry = ToolRegistry(workspace_context=context)
        for tool in build_fs_tools(context) + build_git_tools(context):
            registry.register(tool)
        executor = self.command_executor or SandboxedCommandExecutor()
        artifacts = self.command_artifact_store or CommandArtifactStore(self.session_store)
        command = CommandService(context, self.approval_gate, executor, self.session_store, artifacts)
        edit = AtomicEditService(context, self.session_store)
        candidate = CandidateService(context, self.session_store)
        registry.register(build_command_tool(command))
        registry.register(build_atomic_edit_tool(edit))
        return registry, command, edit, candidate


class _UpgradeApprovalDenied(RuntimeError):
    pass
