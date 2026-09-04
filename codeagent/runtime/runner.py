from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.context.builder import ContextManager
from codeagent.context.models import ContextBudgetExceeded, ModelCapabilities
from codeagent.model_gateway.base import (
    BaseModelClient,
    LLMToolCall,
    MalformedToolArgumentsError,
    ModelRequest,
    TokenUsage,
)
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
    status: str = "completed"
    steps_used_in_turn: int = 0


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
        self.context_manager = ContextManager(session_store, self.tools)

    def run_turn(self, message: str) -> RunnerOutput:
        self.session_store.append_event("user_message", {"message": message})
        return self._run_slice()

    def continue_turn(self) -> RunnerOutput:
        if not self._has_incomplete_turn():
            raise RuntimeError("当前没有可继续的未完成 UserTurn")
        return self._run_slice()

    def _run_slice(self) -> RunnerOutput:
        steps: list[dict] = []
        self._workspace_upgrade_denied_this_turn = False
        repair_messages: list[dict] = []
        control_messages = self._continuation_messages()
        protocol_failures = 0
        previous_failure: tuple[str, str] | None = None
        steps_before = self._steps_used_in_current_turn()
        remaining = self.config.max_model_steps_per_user_turn - steps_before
        if remaining <= 0:
            return self._terminate_budget(steps, steps_before)

        slice_limit = min(self.config.max_steps_per_turn, remaining)
        for _ in range(slice_limit):
            try:
                messages = self.context_manager.build(
                    model_capabilities=ModelCapabilities(
                        context_limit=self.model_config.context_limit,
                        generation_reserve=self.model_config.max_tokens or 4_000,
                    ),
                    current_turn_transient_messages=tuple(control_messages + repair_messages),
                )
            except ContextBudgetExceeded as exc:
                return self._terminate_context_budget(steps, exc)
            request = ModelRequest(
                messages=messages,
                tools=self.tools.as_model_tools(),
                model=self.model_config.resolved_model,
                temperature=self.model_config.temperature,
                max_tokens=self.model_config.max_tokens,
            )
            try:
                response = self.model.complete(request)
            except MalformedToolArgumentsError as exc:
                self._record_model_usage(exc.usage, exc.provider_request_id)
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
                    return RunnerOutput(final_text=final, steps=steps, steps_used_in_turn=self._steps_used_in_current_turn())
                previous_failure = fingerprint
                repair_messages.append({"role": "system", "content": (
                    f"上一响应中 {exc.tool_name} 的 arguments 不是合法 JSON：{exc.message} "
                    f"(line {exc.line}, column {exc.column})。工具尚未执行，workspace 未发生变化。"
                    "请依据工具 schema 重新生成完整工具调用，并确保所有字符串使用合法 JSON 转义。"
                )})
                continue
            self._record_model_usage(response.usage, response.provider_request_id)
            if not response.tool_calls:
                final = response.text or ""
                self.session_store.append_event("assistant_message", {"message": final})
                return RunnerOutput(final_text=final, steps=steps, steps_used_in_turn=self._steps_used_in_current_turn())

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

        used = self._steps_used_in_current_turn()
        if used >= self.config.max_model_steps_per_user_turn:
            return self._terminate_budget(steps, used)
        meta = self.session_store.read_meta()
        self.session_store.append_event("execution_slice_exhausted", {
            "slice_steps": slice_limit,
            "steps_used_in_turn": used,
            "max_model_steps_per_user_turn": self.config.max_model_steps_per_user_turn,
            "candidate_revision": int(meta.get("candidate_revision", 0)),
            "workspace_state": meta.get("workspace_state"),
        })
        text = f"Agent 已执行 {slice_limit} 个模型步骤但尚未完成；可继续同一 UserTurn（已用 {used}/{self.config.max_model_steps_per_user_turn}）。"
        return RunnerOutput(final_text=text, steps=steps, status="slice_exhausted", steps_used_in_turn=used)

    def _terminate_budget(self, steps: list[dict], used: int) -> RunnerOutput:
        text = f"Agent 已耗尽当前 UserTurn 的模型步骤预算（{used}/{self.config.max_model_steps_per_user_turn}），任务未确认完成。"
        self.session_store.append_event("turn_terminated", {
            "reason": "model_step_budget_exhausted", "message": text,
            "steps_used_in_turn": used,
            "max_model_steps_per_user_turn": self.config.max_model_steps_per_user_turn,
        })
        return RunnerOutput(final_text=text, steps=steps, status="model_step_budget_exhausted", steps_used_in_turn=used)

    def _terminate_context_budget(self, steps: list[dict], exc: ContextBudgetExceeded) -> RunnerOutput:
        report = exc.report
        used = self._steps_used_in_current_turn()
        text = (
            "Agent 的下一次模型请求无法在当前上下文输入预算内安全构建；"
            f"最低集合估算为 {report.estimated_tokens} token，预算为 {report.usable_tokens} token。"
            "当前任务未完成，Session 与 workspace 已保留。"
        )
        self.session_store.append_event("turn_terminated", {
            "reason": "context_budget_exceeded",
            "message": text,
            "estimated_tokens": report.estimated_tokens,
            "usable_tokens": report.usable_tokens,
            "dropped_turn_ids": list(report.dropped_turn_ids),
            "reductions": list(report.reductions),
            "minimum_set_components": [asdict(item) for item in report.minimum_set_components],
            "largest_observations": [asdict(item) for item in report.largest_observations],
            "component_estimated_tokens": report.component_estimated_tokens,
            "estimation_residual": report.estimation_residual,
            "steps_used_in_turn": used,
        })
        return RunnerOutput(final_text=text, steps=steps, status="context_budget_exceeded", steps_used_in_turn=used)

    def _steps_used_in_current_turn(self) -> int:
        events = self.session_store.read_events()
        turn_id = next((event.turn_id for event in reversed(events) if event.type == "user_message"), None)
        return sum(event.turn_id == turn_id and event.type in {
            "assistant_tool_calls", "assistant_message", "model_protocol_error",
        } for event in events)

    def _has_incomplete_turn(self) -> bool:
        events = self.session_store.read_events()
        turn_id = next((event.turn_id for event in reversed(events) if event.type == "user_message"), None)
        if turn_id is None:
            return False
        return not any(event.turn_id == turn_id and event.type in {"assistant_message", "turn_terminated"} for event in events)

    def _continuation_messages(self) -> list[dict]:
        events = self.session_store.read_events()
        turn_id = next((event.turn_id for event in reversed(events) if event.type == "user_message"), None)
        latest = next((event for event in reversed(events)
                       if event.turn_id == turn_id and event.type == "execution_slice_exhausted"), None)
        if latest is None:
            return []
        payload = latest.payload
        return [{"role": "system", "content": (
            "这是同一 UserTurn 的后续执行切片；原始用户请求和约束仍然有效，不要要求用户重复，也不要从头探索。"
            f"上一切片结束时已使用 {payload.get('steps_used_in_turn')} 个模型步骤，"
            f"candidate_revision={payload.get('candidate_revision')}，workspace_state={payload.get('workspace_state')}。"
            "请根据现有工具记录、Runtime Snapshot 和 Active Code 继续；已有足够证据时优先编辑和运行用户要求的验证。"
        )}]

    def _notify(self, event: dict) -> None:
        if self.progress_callback is not None:
            self.progress_callback(event)

    def _record_model_usage(self, usage: TokenUsage | None, provider_request_id: str | None) -> None:
        self.session_store.append_event("model_usage", {
            "provider": self.model_config.provider,
            "model": self.model_config.resolved_model,
            "provider_request_id": provider_request_id,
            "usage": usage.model_dump() if usage is not None else None,
        })

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
            self.session_store.append_event("tool_denied", {"call_id": call.call_id, "name": call.name,
                                                            "reason": policy_result.reason, "result": result.model_dump()})
            return self._step(call, "deny", result)

        if policy_result.decision == PolicyDecision.ASK:
            self.session_store.append_event("approval_requested", {"call_id": call.call_id, "name": call.name, "reason": policy_result.reason})
            allowed = self.approval_gate.request(tool, call)
            self.session_store.append_event("approval_decision", {"call_id": call.call_id, "name": call.name, "allowed": allowed})
            if not allowed:
                result = ToolResult(ok=False, error="user denied approval", error_code="approval_denied")
                self.session_store.append_event("tool_denied", {"call_id": call.call_id, "name": call.name,
                                                                "reason": "user denied approval", "result": result.model_dump()})
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
        command = ToolSpec("run_command", "在当前 active_workspace 的 Linux 沙盒中运行命令；默认 cwd 是 Candidate 根目录，额外资源必须显式申请。",
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
            self.context_manager.tool_registry = registry
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
