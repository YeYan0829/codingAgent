from __future__ import annotations

from codeagent.safety.repository_access import enforce_repository_access

from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.runtime.execution_service import ModelToolExecution
from codeagent.context.builder import ContextManager
from codeagent.context.models import ContextBudgetExceeded, ContextState
from codeagent.model_gateway.base import BaseModelClient, LLMResponse, LLMToolCall, ModelRequest, TokenUsage
from codeagent.runtime.approval import ApprovalGate
from codeagent.runtime.policy import DefaultPolicy
from codeagent.runtime.turn_state import current_turn_state
from codeagent.session.store import SessionStore
from codeagent.tools.base import ToolResult, ToolSpec
from codeagent.tools.registry import ToolRegistry
from codeagent.workspace.workspace import WorkspaceContext
from codeagent.runtime.command import CommandExecutor
from codeagent.runtime.command_service import CommandService
from codeagent.runtime.atomic_edit import AtomicEditService
from codeagent.runtime.sandbox_executor import SandboxedCommandExecutor
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.tools.atomic_edit import build_atomic_edit_tool, build_edit_file_tool
from codeagent.tools.command import build_command_tool


from codeagent.model_gateway.output_recovery import (
    MAX_CONSECUTIVE_OUTPUT_TRUNCATIONS, recovery_reasoning_effort,
)
OUTPUT_TRUNCATION_CONTROL_MESSAGE = (
    "上一次响应达到输出长度上限，该响应的工具未执行，文本也不是已完成交付。"
    "这不是任务终止，也未撤销本请求提供的工具；按当前工具列表继续原任务。"
    "仍有调查、修改或可行验证时，现在发出一个简短完整的必要工具调用，不用长篇重述方案或要求用户提供已有工具。"
    "已实施完成或有实际阻塞证据才汇报事实与限制。不要重复已有调查或把半截参数当成执行结果。"
)


@dataclass
class RunnerOutput:
    final_text: str
    steps: list[dict] = field(default_factory=list)
    status: str = "completed"
    steps_used_in_turn: int = 0


from codeagent.runtime.context_preparation import ContextPreparation


class AgentRunner(ContextPreparation):
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
        execution_allowed: bool = True,
        progress_callback: Callable[[dict], None] | None = None,
        cancellation_callback: Callable[[], bool] | None = None,
        model_factory=None,
        gateway_routes=None,
    ) -> None:
        self.session_store = session_store
        self.model = model
        self.model_client = model
        self.tools = tools
        self.workspace_backend = tools.workspace_backend
        self.approval_gate = approval_gate
        self.policy = policy or DefaultPolicy()
        self.config = config or RuntimeConfig()
        self.model_config = model_config or ModelConfig()
        self.workspace_context = workspace_context or tools.workspace_context
        self.command_service: CommandService | None = None
        self.atomic_edit_service: AtomicEditService | None = None
        self.command_executor = command_executor
        self.command_artifact_store = command_artifact_store
        self.progress_callback = progress_callback
        self.cancellation_callback = cancellation_callback or (lambda: False)
        self.model_factory = model_factory
        self.gateway_routes = gateway_routes
        self.agent_sessions = None
        self.request_executor = None
        self.delegation_service = None
        if self.workspace_context is not None and execution_allowed:
            artifacts = command_artifact_store if isinstance(command_artifact_store, CommandArtifactStore) else CommandArtifactStore(session_store)
            executor = command_executor or (self.workspace_backend.command_executor if self.workspace_backend
                else SandboxedCommandExecutor(cancelled=lambda: self.cancellation_callback()))
            if isinstance(executor, SandboxedCommandExecutor):
                executor.cancelled = lambda: self.cancellation_callback()
            self.command_executor = executor
            self.command_service = CommandService(self.workspace_context, approval_gate, executor, session_store, artifacts)
            self.atomic_edit_service = (self.workspace_backend.edit_service(self.workspace_context, session_store)
                if self.workspace_backend else AtomicEditService(self.workspace_context, session_store))
            for tool in (build_command_tool(self.command_service, self.config.task_tracking),
                         build_edit_file_tool(self.atomic_edit_service), build_atomic_edit_tool(self.atomic_edit_service)):
                if not self._can_register(tool):
                    continue
                try:
                    self.tools.get(tool.name)
                except KeyError:
                    self.tools.register(tool)
        from codeagent.runtime.task_plan import TaskPlanService
        from codeagent.tools.task_plan import build_task_plan_tools
        self.task_plan_service = TaskPlanService(session_store, enabled=self.config.task_tracking)
        if self.config.task_tracking:
            for tool in build_task_plan_tools(self.task_plan_service):
                if self._can_register(tool):
                    self.tools.register(tool)
        from codeagent.tools.observations import build_observation_tool
        if self.tools.allowed_names is None or "read_observation" in self.tools.allowed_names:
            self.tools.register(build_observation_tool(session_store))
        self.context_manager = ContextManager(session_store, self.tools, self._environment_snapshot,
                                              prompt_variant=self.config.prompt_variant, state_provider=self._context_state)

    def _can_register(self, tool: ToolSpec) -> bool:
        from codeagent.tools.base import PermissionLevel
        return ((self.tools.allowed_names is None or tool.name in self.tools.allowed_names)
                and (not self.tools.readonly or tool.permission_level == PermissionLevel.READ))

    def _context_state(self) -> ContextState:
        context = self.workspace_context or self.session_store.workspace_context()
        plan = self.task_plan_service.view() if self.config.task_tracking else None
        environment = self._environment_snapshot()
        return ContextState(environment.get("default_cwd_resolves_to", str(context.active_root)), environment, plan)

    def _environment_snapshot(self) -> dict:
        context = self.workspace_context or self.session_store.workspace_context()
        if self.command_service is None:
            return {
                "contract_revision": "command-environment-v1", "backend": "disabled",
                "backend_availability": "not_enabled", "default_cwd": ".",
                "default_cwd_resolves_to": str(context.active_root),
                "home_kind": "private_runtime_home", "tilde_is_host_home": False,
                "network_mode": "off", "commands_are_fresh_processes": True,
                "shell_state_persists": False,
            }
        return self.command_service.environment_snapshot()

    @enforce_repository_access
    def run_turn(self, message: str) -> RunnerOutput:
        from codeagent.orchestration.lease import session_lease
        with session_lease(self.session_store):
            if self._has_incomplete_turn():
                self._close_pending_tools("new_user_message")
                self.session_store.append_event("turn_terminated", {"reason": "new_user_message",
                    "message": "用户已给出新指令；旧轮次未完成的操作不自动重放。"})
            return self._start_turn(message)

    def _start_turn(self, message: str) -> RunnerOutput:
        self.session_store.append_event("user_message", {
            "message": message,
        })
        return self._run_turn()

    @enforce_repository_access
    def continue_turn(self) -> RunnerOutput:
        from codeagent.orchestration.lease import session_lease
        with session_lease(self.session_store):
            return self._continue_turn()

    def _continue_turn(self) -> RunnerOutput:
        if not self._has_incomplete_turn():
            raise RuntimeError("当前没有可继续的未完成 UserTurn")
        return self._run_turn()

    def _run_turn(self) -> RunnerOutput:
        from codeagent.model_gateway.factory import build_model_client
        from codeagent.runtime.delegation import DelegationService
        from codeagent.runtime.request_executor import RequestExecutor, ExecutionInterrupted
        from codeagent.orchestration.agents import AgentSessions
        from codeagent.orchestration.main import run_main_loop
        from codeagent.orchestration.lease import session_lease
        turn_id = current_turn_state(self.session_store.read_events()).turn_id
        if self.request_executor is None or self.request_executor.turn_id != turn_id:
            self.request_executor = RequestExecutor(self.session_store, self.config, self.model_config,
                self.model_factory or build_model_client, lambda: self.cancellation_callback(),
                client=self.model_client if self.model_factory is None else None)
            self.delegation_service = DelegationService(self.session_store, self.request_executor,
                lambda: self.workspace_context or self.session_store.workspace_context(), parent_policy=self.policy,
                parent_runtime=self)
            self.agent_sessions = AgentSessions(self.session_store, self.request_executor, self.delegation_service, self.gateway_routes)
            if self.config.delegation_enabled:
                tool = self.agent_sessions.task_tool()
                if self._can_register(tool):
                    self.tools.register(tool)
        try:
            with session_lease(self.session_store) as epoch:
                return run_main_loop(self, self.agent_sessions, epoch)
        except KeyboardInterrupt:
            return self._terminate_stopped(getattr(self, "_turn_steps", []))
        except ExecutionInterrupted as exc:
            steps = getattr(self, "_turn_steps", [])
            if exc.reason == "cancelled":
                return self._terminate_stopped(steps)
            self._close_pending_tools(exc.reason)
            text = "执行已停止：" + exc.reason + "；当前修改与记录已保留。"
            self.session_store.append_event("turn_terminated", {"reason": exc.reason, "message": text})
            return RunnerOutput(text, steps=steps, status=exc.reason, steps_used_in_turn=self._steps_used_in_current_turn())
        except Exception as exc:
            self._close_pending_tools("unknown_effect_or_interrupted")
            text = "模型或 Runtime 执行失败；当前修改与记录已保留。"
            self.session_store.append_event("turn_terminated", {"reason": "runtime_error",
                "error_type": type(exc).__name__, "error": str(exc), "message": text})
            return RunnerOutput(text, steps=getattr(self, "_turn_steps", []), status="failed",
                steps_used_in_turn=self._steps_used_in_current_turn())

    def _close_pending_tools(self, reason):
        events = self.session_store.read_events()
        turn_id = current_turn_state(events).turn_id
        resolved = {e.payload.get("call_id") for e in events if e.turn_id == turn_id and e.type in {"tool_result", "tool_denied"}}
        for event in events:
            if event.turn_id != turn_id or event.type != "assistant_tool_calls":
                continue
            for call in event.payload["tool_calls"]:
                if call["call_id"] not in resolved:
                    self.session_store.append_event("tool_denied", {"call_id": call["call_id"], "name": call["name"],
                        "result": ToolResult(ok=False, error_code=reason, error="执行未获可靠结果；保留现场，不自动重放").model_dump()})
                    resolved.add(call["call_id"])


    def _main_request_reasoning_effort(self, *, recovering_output_truncation: bool) -> str | None:
        """截断续写暂时降低推理强度；正常主请求继续使用 client 的配置值。"""
        return recovery_reasoning_effort(self.model_config, recovering=recovering_output_truncation)


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
            "context_budget_reason": exc.reason,
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

    def _terminate_stopped(self, steps: list[dict]) -> RunnerOutput:
        text = "用户已请求停止；Agent 在当前安全边界结束执行。"
        # 同一次模型响应可声明多个调用；未执行的调用也需闭合协议，才能安全开始后续对话。
        events = self.session_store.read_events()
        turn_id = next((event.turn_id for event in reversed(events) if event.type == "user_message"), None)
        terminal = {event.payload.get("call_id") for event in events if event.turn_id == turn_id
                    and event.type in {"tool_result", "tool_denied"}}
        for event in events:
            if event.turn_id == turn_id and event.type == "assistant_tool_calls":
                for call in event.payload.get("tool_calls", []):
                    if call["call_id"] not in terminal:
                        self.session_store.append_event("tool_denied", {
                            "call_id": call["call_id"], "name": call["name"], "reason": "user_stop",
                        })
        self.session_store.append_event("turn_terminated", {
            "reason": "user_stop", "message": text, "boundary": "between_model_or_tool_steps",
        })
        return RunnerOutput(
            final_text=text, steps=steps, status="stopped", steps_used_in_turn=self._steps_used_in_current_turn(),
        )

    def _steps_used_in_current_turn(self) -> int:
        return current_turn_state(self.session_store.read_events()).used

    def _has_incomplete_turn(self) -> bool:
        events = self.session_store.read_events()
        turn_id = next((event.turn_id for event in reversed(events) if event.type == "user_message"), None)
        if turn_id is None:
            return False
        return not any(event.turn_id == turn_id and event.type in {"assistant_message", "turn_terminated"} for event in events)

    def _truncation_continuation_messages(self) -> list[dict]:
        events = self.session_store.read_events()
        turn_id = next((event.turn_id for event in reversed(events) if event.type == "user_message"), None)
        latest_step = next((event for event in reversed(events) if event.turn_id == turn_id and event.type in {
            "assistant_tool_calls", "assistant_message", "model_protocol_error", "model_output_truncated",
        }), None)
        if latest_step is None or latest_step.type != "model_output_truncated":
            return []
        return [{"role": "system", "content": OUTPUT_TRUNCATION_CONTROL_MESSAGE}]

    def _protocol_continuation_messages(self) -> list[dict]:
        events = self.session_store.read_events()
        turn_id = next((event.turn_id for event in reversed(events) if event.type == "user_message"), None)
        latest = next((event for event in reversed(events) if event.turn_id == turn_id and event.type in {
            "assistant_tool_calls", "assistant_message", "model_protocol_error", "model_output_truncated",
        }), None)
        if latest is None or latest.type != "model_protocol_error":
            return []
        payload = latest.payload
        from codeagent.model_gateway.output_recovery import protocol_repair_message
        return [{"role": "system", "content": protocol_repair_message(payload)}]

    def _notify(self, event: dict) -> None:
        if self.progress_callback is not None:
            self.progress_callback(event)

    def _record_model_usage(
        self, usage: TokenUsage | None, provider_request_id: str | None, finish_reason: str | None = None,
        *, reasoning_effort: str | None = None, request_id: str | None = None,
    ) -> None:
        if request_id and any(e.type == "model_usage" and e.payload.get("request_id") == request_id
                              for e in self.session_store.read_events()):
            return
        report = self.context_manager.last_budget_report
        self.session_store.append_event("model_usage", {
            **({"request_id": request_id} if request_id else {}),
            "provider": self.model_config.provider,
            "model": self.model_config.resolved_model,
            "purpose": "main_agent",
            "provider_request_id": provider_request_id,
            "finish_reason": finish_reason,
            "reasoning_effort": reasoning_effort or self.model_config.resolved_reasoning_effort,
            "usage": usage.model_dump() if usage is not None else None,
            "context_reductions": list(report.reductions) if report is not None else [],
            "context": ({
                "estimated_tokens": report.estimated_tokens,
                "usable_tokens": report.usable_tokens,
                "fixed_request_tokens": report.fixed_request_tokens,
                "history_tokens": report.history_tokens,
                "uncovered_context_event_count": report.uncovered_context_event_count,
                "chain_tip_event_id": report.chain_tip_event_id,
                "anchor_source_event_id": report.anchor_source_event_id,
                "retained_raw_source_event_ids": list(report.retained_raw_source_event_ids),
                "mandatory_protocol_atom_id": report.mandatory_protocol_atom_id,
            } if report is not None else None),
        })

    def _record_output_truncation(
        self, steps: list[dict], *, text: str | None, reasoning_content: str | None,
        finish_reason: str | None, detection: str,
    ) -> RunnerOutput | None:
        prior_count = self._consecutive_output_truncations()
        count = prior_count + 1
        payload = {
            "message": text or "",
            "finish_reason": finish_reason,
            "detection": detection,
            "consecutive_count": count,
            "max_consecutive_output_truncations": MAX_CONSECUTIVE_OUTPUT_TRUNCATIONS,
        }
        if reasoning_content is not None:
            payload["reasoning_content"] = reasoning_content
        self.session_store.append_event("model_output_truncated", payload)
        used = self._steps_used_in_current_turn()
        if count < MAX_CONSECUTIVE_OUTPUT_TRUNCATIONS:
            return None
        message = (
            f"模型连续 {count} 次达到输出长度上限，未形成完整工具调用或最终回答；"
            "当前任务未完成，Session 与 workspace 已保留。"
        )
        self.session_store.append_event("turn_terminated", {
            "reason": "output_truncated", "message": message,
            "consecutive_output_truncations": count,
            "steps_used_in_turn": used,
        })
        return RunnerOutput(
            final_text=message, steps=steps, status="output_truncated", steps_used_in_turn=used,
        )

    def _consecutive_output_truncations(self) -> int:
        events = self.session_store.read_events()
        turn_id = next((event.turn_id for event in reversed(events) if event.type == "user_message"), None)
        count = 0
        for event in reversed(events):
            if event.turn_id != turn_id:
                continue
            if event.type not in {
                "assistant_tool_calls", "assistant_message", "model_protocol_error", "model_output_truncated",
            }:
                continue
            if event.type != "model_output_truncated":
                break
            count += 1
        return count

    def _handle_tool_call(self, call: LLMToolCall) -> dict:
        return ModelToolExecution.main(self, call)

    def _step(self, call: LLMToolCall, decision: str, result: ToolResult) -> dict:
        return {
            "tool": call.name,
            "arguments": call.arguments,
            "decision": decision,
            "result": result.model_dump(),
        }
