from __future__ import annotations

import json
import hashlib
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace

from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.context.builder import ContextManager, SUMMARY_ALGORITHM_VERSION
from codeagent.context.models import (
    CondensationPlan, CondensationRequired, ContextBudgetExceeded, ModelCapabilities, ReadyContext,
)
from codeagent.model_gateway.base import (
    BaseModelClient,
    LLMResponse,
    LLMToolCall,
    MalformedToolArgumentsError,
    ModelRequest,
    TokenUsage,
)
from codeagent.runtime.approval import ApprovalGate
from codeagent.runtime.policy import DefaultPolicy, PolicyDecision
from codeagent.runtime.turn_budget import current_turn_budget
from codeagent.session.store import SessionStore, SessionStoreError
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


MAX_CONSECUTIVE_OUTPUT_TRUNCATIONS = 3
MAX_CONDENSER_CALLS_PER_MODEL_STEP = 4
MAX_SOFT_CONDENSER_CALLS_PER_MODEL_STEP = 1
OUTPUT_TRUNCATION_CONTROL_MESSAGE = (
    "上一次响应达到输出长度上限，任务尚未完成。不要扩展或重新推演已有分析；"
    "请依据现有证据立即执行一个具体的下一步工具调用，不要重复已经完成的探索。"
)


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
        cancellation_callback: Callable[[], bool] | None = None,
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
        self.cancellation_callback = cancellation_callback or (lambda: False)
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
        self.context_manager = ContextManager(session_store, self.tools, self._environment_snapshot)

    def _environment_snapshot(self) -> dict:
        context = self.workspace_context or self.session_store.workspace_context()
        if self.command_service is None:
            return {
                "contract_revision": "command-environment-v1", "backend": "pending_workspace_upgrade",
                "backend_availability": "not_active", "default_cwd": ".",
                "default_cwd_resolves_to": str(context.active_root),
                "home_kind": "private_runtime_home", "tilde_is_host_home": False,
                "network_mode": "off", "commands_are_fresh_processes": True,
                "shell_state_persists": False,
            }
        return self.command_service.environment_snapshot()

    def run_turn(self, message: str) -> RunnerOutput:
        self.session_store.append_event("user_message", {
            "message": message,
            "max_model_steps_per_user_turn": self.config.max_model_steps_per_user_turn,
        })
        return self._run_slice()

    def continue_turn(self) -> RunnerOutput:
        if not self._has_incomplete_turn():
            raise RuntimeError("当前没有可继续的未完成 UserTurn")
        return self._run_slice()

    def _run_slice(self) -> RunnerOutput:
        steps: list[dict] = []
        if self.cancellation_callback():
            return self._terminate_stopped(steps)
        events = self.session_store.read_events()
        turn_id = next((event.turn_id for event in reversed(events) if event.type == "user_message"), None)
        self._workspace_upgrade_denied_this_turn = any(
            event.turn_id == turn_id and event.type == "approval_decision" and event.payload.get("allowed") is False
            and (event.payload.get("kind") == "workspace_upgrade" or event.payload.get("reason") == "创建隔离 Agent worktree")
            for event in events
        )
        repair_messages = self._protocol_continuation_messages()
        control_messages = self._continuation_messages()
        truncation_messages = self._truncation_continuation_messages()
        protocol_failures = 0
        previous_failure: tuple[str, str] | None = None
        budget = current_turn_budget(self.session_store.read_events(), self.config.max_model_steps_per_user_turn)
        steps_before = budget.used
        remaining = budget.limit - steps_before
        if remaining <= 0:
            return self._wait_for_budget(steps, steps_before)

        slice_limit = min(self.config.max_steps_per_turn, remaining)
        for _ in range(slice_limit):
            if self.cancellation_callback():
                return self._terminate_stopped(steps)
            caps = ModelCapabilities(
                context_limit=self.model_config.context_limit,
                generation_reserve=self.model_config.max_tokens or 4_000,
            )
            prepared = self._prepare_context(
                caps,
                tuple(control_messages + repair_messages + truncation_messages),
            )
            if isinstance(prepared, list):  # 测试/旧嵌入方的临时 renderer seam。
                messages = prepared
            elif isinstance(prepared, ContextBudgetExceeded):
                return self._terminate_context_budget(steps, prepared)
            else:
                messages = prepared.messages
            if self.cancellation_callback():
                return self._terminate_stopped(steps)
            request = ModelRequest(
                messages=messages,
                tools=self.tools.as_model_tools(),
                model=self.model_config.resolved_model,
                temperature=self.model_config.temperature,
                max_tokens=self.model_config.max_tokens,
                reasoning_effort=self._main_request_reasoning_effort(
                    recovering_output_truncation=bool(truncation_messages)
                ),
            )
            try:
                response = self.model.complete(request)
            except MalformedToolArgumentsError as exc:
                self._record_model_usage(
                    exc.usage, exc.provider_request_id, exc.finish_reason,
                    reasoning_effort=request.reasoning_effort,
                )
                if exc.finish_reason == "length":
                    terminated = self._record_output_truncation(
                        steps, text=exc.text, reasoning_content=exc.reasoning_content,
                        finish_reason=exc.finish_reason, detection="provider_finish_reason",
                        budget_limit=budget.limit,
                    )
                    if terminated is not None:
                        return terminated
                    truncation_messages[:] = [{"role": "system", "content": OUTPUT_TRUNCATION_CONTROL_MESSAGE}]
                    repair_messages.clear()
                    protocol_failures = 0
                    previous_failure = None
                    continue
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
            self._record_model_usage(
                response.usage, response.provider_request_id, response.finish_reason,
                reasoning_effort=request.reasoning_effort,
            )
            if self.cancellation_callback():
                return self._terminate_stopped(steps)
            truncation_detection = self._output_truncation_detection(response, request)
            if truncation_detection is not None and not response.tool_calls:
                terminated = self._record_output_truncation(
                    steps, text=response.text, reasoning_content=response.reasoning_content,
                    finish_reason=response.finish_reason, detection=truncation_detection,
                    budget_limit=budget.limit,
                )
                if terminated is not None:
                    return terminated
                truncation_messages[:] = [{"role": "system", "content": OUTPUT_TRUNCATION_CONTROL_MESSAGE}]
                repair_messages.clear()
                protocol_failures = 0
                previous_failure = None
                continue
            repair_messages.clear()
            if not response.tool_calls:
                final = response.text or ""
                payload = {"message": final}
                if response.reasoning_content is not None:
                    payload["reasoning_content"] = response.reasoning_content
                self.session_store.append_event("assistant_message", payload)
                return RunnerOutput(final_text=final, steps=steps, steps_used_in_turn=self._steps_used_in_current_turn())

            tool_payload = {
                "message": response.text or "",
                "tool_calls": [call.model_dump() for call in response.tool_calls],
            }
            if response.reasoning_content is not None:
                tool_payload["reasoning_content"] = response.reasoning_content
            self.session_store.append_event("assistant_tool_calls", tool_payload)
            truncation_messages.clear()
            if response.text:
                self._notify({"type": "assistant_progress", "message": response.text})
            for call in response.tool_calls:
                if self.cancellation_callback():
                    return self._terminate_stopped(steps)
                step = self._handle_tool_call(call)
                steps.append(step)
                self._notify({"type": "tool_step", "step": step})
                if self.cancellation_callback():
                    return self._terminate_stopped(steps)

        used = self._steps_used_in_current_turn()
        if used >= budget.limit:
            return self._wait_for_budget(steps, used)
        meta = self.session_store.read_meta()
        self.session_store.append_event("execution_slice_exhausted", {
            "slice_steps": slice_limit,
            "steps_used_in_turn": used,
            "max_model_steps_per_user_turn": budget.limit,
            "candidate_revision": int(meta.get("candidate_revision", 0)),
            "workspace_state": meta.get("workspace_state"),
        })
        text = f"内部执行切片结束；继续同一 UserTurn（已用 {used}/{budget.limit}）。"
        return RunnerOutput(final_text=text, steps=steps, status="slice_exhausted", steps_used_in_turn=used)

    def _prepare_context(
        self, caps: ModelCapabilities, transient_messages: tuple[dict, ...],
    ) -> ReadyContext | ContextBudgetExceeded:
        """在一次主 ModelStep 前有界编排 semantic condensation。"""
        calls = 0
        soft_calls = 0
        suppress_soft = False
        plan_max_atoms: int | None = None
        consecutive_invalid = 0
        request_failures: dict[str, int] = {}
        retry_plan: CondensationPlan | None = None
        valid_before_estimate: int | None = None

        while True:
            try:
                result = self.context_manager.build(
                    model_capabilities=caps,
                    current_turn_transient_messages=transient_messages,
                    suppress_soft_trigger=suppress_soft,
                    plan_max_atoms=plan_max_atoms,
                )
            except ContextBudgetExceeded as exc:  # 兼容旧 renderer seam。
                return exc
            if isinstance(result, list):
                report = self.context_manager.last_budget_report or self._compat_budget_report(caps)
                return ReadyContext(result, report)
            if isinstance(result, ReadyContext):
                return result
            if isinstance(result, ContextBudgetExceeded):
                return result
            if valid_before_estimate is not None and result.budget_report.estimated_tokens >= valid_before_estimate:
                return ContextBudgetExceeded(result.budget_report, "condensation_no_progress")
            valid_before_estimate = None
            if calls >= MAX_CONDENSER_CALLS_PER_MODEL_STEP:
                return ContextBudgetExceeded(result.budget_report, "condensation_cycles_exhausted")
            if not result.plan.hard and soft_calls >= MAX_SOFT_CONDENSER_CALLS_PER_MODEL_STEP:
                suppress_soft = True
                continue

            plan = retry_plan or result.plan
            retry_plan = None
            if plan.expected_event_seq != int(self.session_store.read_meta().get("event_seq", 0)):
                plan = replace(plan, expected_event_seq=int(self.session_store.read_meta().get("event_seq", 0)))
            calls += 1
            if not plan.hard:
                soft_calls += 1
            status, failure_code = self._call_condenser(plan, caps, calls)
            if status == "valid_completion":
                consecutive_invalid = 0
                plan_max_atoms = None
                valid_before_estimate = result.budget_report.estimated_tokens
                continue
            if status == "request_failed":
                count = request_failures.get(plan.plan_id, 0) + 1
                request_failures[plan.plan_id] = count
                if not plan.hard:
                    suppress_soft = True
                    continue
                if count < 2 and calls < MAX_CONDENSER_CALLS_PER_MODEL_STEP:
                    retry_plan = replace(plan, expected_event_seq=int(self.session_store.read_meta().get("event_seq", 0)))
                    continue
                return ContextBudgetExceeded(result.budget_report, "condensation_request_failed")

            consecutive_invalid += 1
            if not plan.hard:
                suppress_soft = True
                continue
            if consecutive_invalid >= 2:
                return ContextBudgetExceeded(result.budget_report, "condensation_invalid_completion")
            if failure_code == "stale_frontier":
                plan_max_atoms = None
                continue
            smaller = len(plan.source_atoms) - 1
            if smaller <= 0:
                return ContextBudgetExceeded(result.budget_report, "condensation_no_smaller_atom")
            plan_max_atoms = smaller

    def _call_condenser(
        self, plan: CondensationPlan, caps: ModelCapabilities, call_index: int,
    ) -> tuple[str, str | None]:
        attempt_id = "cond-attempt-" + uuid.uuid4().hex[:16]
        request = ModelRequest(
            messages=list(plan.source_messages),
            tools=[],
            tool_choice="none",
            model=self.model_config.resolved_model,
            temperature=0,
            max_tokens=caps.summary_max_tokens,
            purpose="context_condenser",
            reasoning_effort=self._condenser_reasoning_effort(),
        )
        started = time.monotonic()
        try:
            response = self.model.complete(request)
        except Exception as exc:
            latency = time.monotonic() - started
            self._append_condensation_failure(
                plan, attempt_id, call_index, "request_failed", type(exc).__name__, latency,
                usage=None, provider_request_id=None, finish_reason=None, output_text=None,
                reasoning_content=None,
            )
            return "request_failed", type(exc).__name__
        latency = time.monotonic() - started
        failure_code = self._invalid_summary_reason(response, caps)
        if failure_code is not None:
            self._append_condensation_failure(
                plan, attempt_id, call_index, "invalid_completion", failure_code, latency,
                usage=response.usage, provider_request_id=response.provider_request_id,
                finish_reason=response.finish_reason, output_text=response.text,
                reasoning_content=response.reasoning_content,
            )
            return "invalid_completion", failure_code

        summary = (response.text or "").strip()
        usage_payload = self._usage_event_payload(
            response.usage, response.provider_request_id, response.finish_reason, latency,
            purpose="context_condenser",
        )
        attempt_payload = self._condensation_attempt_payload(
            plan, attempt_id, call_index, "valid_completion", None, latency,
            response.usage, response.provider_request_id, response.finish_reason, summary,
            response.reasoning_content,
        )
        success_payload = {
            "attempt_id": attempt_id,
            "previous_summary_event_id": plan.previous_summary_event_id,
            "newly_covered_from_seq": self._seq_from_event_id(plan.source_event_ids[0]),
            "newly_covered_to_seq": self._seq_from_event_id(plan.source_event_ids[-1]),
            "newly_covered_event_ids": list(plan.source_event_ids),
            "newly_covered_event_ids_sha256": plan.candidate_source_event_ids_sha256,
            "logical_covered_event_count": (
                plan.previous_logical_covered_event_count + len(plan.source_event_ids)
            ),
            "logical_frontier_source_event_id": plan.source_event_ids[-1],
            "summary": summary,
            "reason": list(plan.trigger),
            "algorithm_version": SUMMARY_ALGORITHM_VERSION,
            "input_estimated_tokens": plan.input_estimated_tokens,
            "summary_estimated_tokens": self.context_manager.estimator.estimate(summary),
        }
        try:
            self.session_store.append_derived_events([
                ("model_usage", usage_payload),
                ("context_condensation_attempt", attempt_payload),
                ("context_condensed", success_payload),
            ], turn_id=plan.turn_id, expected_event_seq=plan.expected_event_seq)
        except SessionStoreError:
            # response 合法但 plan frontier 已失效；不得让它进入 rolling chain。
            self._append_condensation_failure(
                replace(plan, expected_event_seq=int(self.session_store.read_meta().get("event_seq", 0))),
                attempt_id, call_index, "invalid_completion", "stale_frontier", latency,
                usage=response.usage, provider_request_id=response.provider_request_id,
                finish_reason=response.finish_reason, output_text=summary,
                reasoning_content=response.reasoning_content,
            )
            return "invalid_completion", "stale_frontier"
        return "valid_completion", None

    def _append_condensation_failure(
        self, plan: CondensationPlan, attempt_id: str, call_index: int, status: str,
        failure_code: str, latency: float, *, usage: TokenUsage | None,
        provider_request_id: str | None, finish_reason: str | None, output_text: str | None,
        reasoning_content: str | None,
    ) -> None:
        usage_payload = self._usage_event_payload(
            usage, provider_request_id, finish_reason, latency, purpose="context_condenser",
        )
        attempt_payload = self._condensation_attempt_payload(
            plan, attempt_id, call_index, status, failure_code, latency, usage,
            provider_request_id, finish_reason, output_text,
            reasoning_content,
        )
        try:
            self.session_store.append_derived_events([
                ("model_usage", usage_payload),
                ("context_condensation_attempt", attempt_payload),
            ], turn_id=plan.turn_id, expected_event_seq=plan.expected_event_seq)
        except SessionStoreError:
            self.session_store.append_derived_events([
                ("model_usage", usage_payload),
                ("context_condensation_attempt", {**attempt_payload, "failure_code": "stale_frontier"}),
            ], turn_id=plan.turn_id)

    def _condensation_attempt_payload(
        self, plan: CondensationPlan, attempt_id: str, call_index: int, status: str,
        failure_code: str | None, latency: float, usage: TokenUsage | None,
        provider_request_id: str | None, finish_reason: str | None, output_text: str | None,
        reasoning_content: str | None,
    ) -> dict:
        text = output_text or ""
        payload = {
            "attempt_id": attempt_id,
            "plan_id": plan.plan_id,
            "call_index_for_next_model_step": call_index,
            "previous_summary_event_id": plan.previous_summary_event_id,
            "candidate_source_event_ids_sha256": plan.candidate_source_event_ids_sha256,
            "candidate_source_event_ids": list(plan.source_event_ids),
            "trigger": list(plan.trigger),
            "hard": plan.hard,
            "request_estimated_tokens_before": plan.request_estimated_tokens_before,
            "condenser_input_estimated_tokens": plan.input_estimated_tokens,
            "status": status,
            "failure_code": failure_code,
            "provider": self.model_config.provider,
            "model": self.model_config.resolved_model,
            "provider_request_id": provider_request_id,
            "finish_reason": finish_reason,
            "usage": usage.model_dump() if usage is not None else None,
            "latency_seconds": latency,
            "output_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None,
            "output_chars": len(text),
        }
        if reasoning_content is not None:
            payload["reasoning_content"] = reasoning_content
        return payload

    def _usage_event_payload(
        self, usage: TokenUsage | None, provider_request_id: str | None,
        finish_reason: str | None, latency: float, *, purpose: str,
    ) -> dict:
        return {
            "provider": self.model_config.provider,
            "model": self.model_config.resolved_model,
            "purpose": purpose,
            "provider_request_id": provider_request_id,
            "finish_reason": finish_reason,
            "usage": usage.model_dump() if usage is not None else None,
            "latency_seconds": latency,
            "context_reductions": ["semantic_condensation_applied"] if purpose == "context_condenser" else [],
        }

    def _invalid_summary_reason(self, response: LLMResponse, caps: ModelCapabilities) -> str | None:
        if response.finish_reason != "stop":
            return "missing_finish_reason" if response.finish_reason is None else f"finish_reason_{response.finish_reason}"
        if response.tool_calls:
            return "tool_calls"
        if not (response.text or "").strip():
            return "empty_summary"
        if self.context_manager.estimator.estimate((response.text or "").strip()) > caps.summary_max_tokens:
            return "summary_too_large"
        return None

    def _condenser_reasoning_effort(self) -> str | None:
        if not self.model_config.reasoning_enabled:
            return None
        if self.model_config.provider == "glm" and self.model_config.resolved_model == "glm-5.3":
            return "low"
        if self.model_config.provider == "deepseek":
            return "low"
        return "high"

    def _main_request_reasoning_effort(self, *, recovering_output_truncation: bool) -> str | None:
        """截断续写暂时降低推理强度；正常主请求继续使用 client 的配置值。"""
        if not recovering_output_truncation or not self.model_config.reasoning_enabled:
            return None
        if (self.model_config.provider == "glm"
                and self.model_config.resolved_model == "glm-5.3"):
            return "low"
        if self.model_config.provider == "deepseek":
            return "low"
        return None

    @staticmethod
    def _seq_from_event_id(value: str) -> int:
        return int(value.split(":", 1)[1])

    @staticmethod
    def _compat_budget_report(caps: ModelCapabilities):
        from codeagent.context.models import BudgetReport
        return BudgetReport(0, caps.usable_input_budget)

    def _wait_for_budget(self, steps: list[dict], used: int) -> RunnerOutput:
        budget = current_turn_budget(self.session_store.read_events(), self.config.max_model_steps_per_user_turn)
        text = f"本轮已使用 {used}/{budget.limit} 个模型步骤。可提高本轮预算后继续；当前修改与原始请求已保留。"
        events = self.session_store.read_events()
        # 达到预算是等待用户决策，不是 UserTurn 终止；重复 Continue 不产生重复等待事件。
        if not any(event.type == "turn_budget_exhausted" and event.turn_id == budget.turn_id
                   and event.payload.get("max_model_steps_per_user_turn") == budget.limit for event in events):
            self.session_store.append_event("turn_budget_exhausted", {
                "message": text, "steps_used_in_turn": used,
                "max_model_steps_per_user_turn": budget.limit,
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
        events = self.session_store.read_events()
        turn_id = next((event.turn_id for event in reversed(events) if event.type == "user_message"), None)
        return sum(event.turn_id == turn_id and event.type in {
            "assistant_tool_calls", "assistant_message", "model_protocol_error", "model_output_truncated",
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
        budget = current_turn_budget(events, self.config.max_model_steps_per_user_turn)
        return [{"role": "system", "content": (
            f"当前本轮总上限为 {budget.limit} 个模型步骤，已使用 {budget.used}。"
            "这是同一 UserTurn 的后续执行切片；原始用户请求和约束仍然有效，不要要求用户重复，也不要从头探索。"
            f"上一切片结束时已使用 {payload.get('steps_used_in_turn')} 个模型步骤，"
            f"candidate_revision={payload.get('candidate_revision')}，workspace_state={payload.get('workspace_state')}。"
            "请根据现有工具记录和 Runtime Snapshot 继续；已有足够证据时优先编辑和运行用户要求的验证。"
        )}]

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
        return [{"role": "system", "content": (
            f"上一响应中 {payload.get('tool', '')} 的 arguments 不是合法 JSON：{payload.get('message', '')} "
            f"(line {payload.get('line')}, column {payload.get('column')})。工具尚未执行，workspace 未发生变化。"
            "请依据工具 schema 重新生成完整工具调用，并确保所有字符串使用合法 JSON 转义。"
        )}]

    def _notify(self, event: dict) -> None:
        if self.progress_callback is not None:
            self.progress_callback(event)

    def _record_model_usage(
        self, usage: TokenUsage | None, provider_request_id: str | None, finish_reason: str | None = None,
        *, reasoning_effort: str | None = None,
    ) -> None:
        report = self.context_manager.last_budget_report
        self.session_store.append_event("model_usage", {
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

    @staticmethod
    def _output_truncation_detection(response: LLMResponse, request: ModelRequest) -> str | None:
        if response.finish_reason == "length":
            return "provider_finish_reason"
        if response.finish_reason is not None:
            return None
        output_tokens = response.usage.output_tokens if response.usage is not None else None
        if (not response.text and not response.tool_calls and request.max_tokens is not None
                and output_tokens is not None and output_tokens >= request.max_tokens):
            return "usage_fallback"
        return None

    def _record_output_truncation(
        self, steps: list[dict], *, text: str | None, reasoning_content: str | None,
        finish_reason: str | None, detection: str, budget_limit: int,
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
        if used >= budget_limit:
            return self._wait_for_budget(steps, used)
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
            managed = bool(getattr(self.approval_gate, "manages_events", False))
            if not managed:
                self.session_store.append_event("approval_requested", {"call_id": call.call_id, "name": call.name, "reason": policy_result.reason})
            allowed = self.approval_gate.request(tool, call)
            if not managed:
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
        managed = bool(getattr(self.approval_gate, "manages_events", False))
        if not managed:
            self.session_store.append_event("approval_requested", {"name": tool_name, "reason": "创建隔离 Agent worktree"})
        allowed = self.approval_gate.request_workspace_upgrade(tool_name, arguments)
        if not managed:
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
