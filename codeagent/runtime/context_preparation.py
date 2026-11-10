"""主子共用的 Context 装配与压缩推进；模型发送和历史投影分别由依赖提供。"""
from __future__ import annotations
import hashlib
from dataclasses import replace
import time
import uuid
from codeagent.context.builder import SUMMARY_ALGORITHM_VERSION, select_rolling_summary
from codeagent.context.projector import project_context_events
from codeagent.context.models import CondensationPlan, ContextBudgetExceeded, ModelCapabilities, ReadyContext
from codeagent.model_gateway.base import LLMResponse, ModelRequest, TokenUsage
from codeagent.runtime.execution_service import ModelToolExecution, UnsafeRecovery
from codeagent.session.store import SessionStoreError, _store_lock


class ContextPreparation:
    def _prepare_context(
        self, caps: ModelCapabilities, transient_messages: tuple[dict, ...],
    ) -> ReadyContext | ContextBudgetExceeded:
        """摘要后按实际文本重建；提前摘要只尝试一次，容量不足时按进展推进。"""
        calls = 0
        suppress_soft = False
        previous_size: int | None = None
        summary_caps = replace(caps, max_output_tokens=(
            self.model_config.summary_max_tokens or caps.max_output_tokens
        ))
        while True:
            try:
                result = self.context_manager.build(
                    model_capabilities=caps,
                    summary_capabilities=summary_caps,
                    current_turn_transient_messages=transient_messages,
                    suppress_soft_trigger=suppress_soft,
                )
            except ContextBudgetExceeded as exc:  # 兼容旧 renderer seam。
                return exc
            if isinstance(result, list):
                report = self.context_manager.last_budget_report or self._compat_budget_report(caps)
                return ReadyContext(result, report)
            if isinstance(result, (ReadyContext, ContextBudgetExceeded)):
                return result
            if previous_size is not None and result.budget_report.estimated_tokens >= previous_size:
                return ContextBudgetExceeded(result.budget_report, "condensation_no_progress")
            calls += 1
            status, _ = self._call_condenser(result.plan, summary_caps, calls)
            suppress_soft = True
            if status != "valid_completion":
                if result.plan.hard:
                    return ContextBudgetExceeded(result.budget_report, "condensation_" + status)
                # 保留原始历史，跳过本次提前摘要；实际容量仍由下一次装配检查。
                continue
            previous_size = result.budget_report.estimated_tokens


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
            max_tokens=caps.max_output_tokens,
            purpose="context_condenser",
            reasoning_effort=self._condenser_reasoning_effort(),
        )
        started = time.monotonic()
        try:
            response = ModelToolExecution.complete(getattr(self, "condenser", self.model), request)
        except Exception as exc:
            from codeagent.runtime.request_executor import ExecutionInterrupted
            if isinstance(exc, (ExecutionInterrupted, UnsafeRecovery)):
                raise
            latency = time.monotonic() - started
            self._append_condensation_failure(
                plan, attempt_id, call_index, "request_failed", type(exc).__name__, latency,
                usage=None, provider_request_id=None, finish_reason=None, output_text=None,
                reasoning_content=None,
            )
            return "request_failed", type(exc).__name__
        latency = time.monotonic() - started
        failure_code = self._invalid_summary_reason(response)
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
            self._append_condensation_events(plan, [
                ("model_usage", usage_payload),
                ("context_condensation_attempt", attempt_payload),
                ("context_condensed", success_payload),
            ])
        except SessionStoreError:
            # response 合法但 plan frontier 已失效；不得让它进入 rolling chain。
            self._append_condensation_failure(
                plan,
                attempt_id, call_index, "invalid_completion", "stale_frontier", latency,
                usage=response.usage, provider_request_id=response.provider_request_id,
                finish_reason=response.finish_reason, output_text=summary,
                reasoning_content=response.reasoning_content,
            )
            return "invalid_completion", "stale_frontier"
        return "valid_completion", None


    def _append_condensation_events(self, plan, items, *, compare=True):
        # 只比较被摘要的实际历史与前序摘要，不把费用、后台通知或图节点混成上下文一致性。
        with _store_lock(self.session_store.meta_path):
            if compare:
                events = self.session_store.read_events()
                projection = project_context_events(events)
                anchor = next((item for item in reversed(projection.items) if item.source_type == "user_message"), None)
                protected = self.context_manager._mandatory_atom(projection.atoms, anchor)
                summary = select_rolling_summary(events, projection.atoms, before_atom=protected)
                previous = summary.event_id if summary else None
                covered = summary.logical_covered_event_count if summary else 0
                actual = tuple(item.source_event_id for item in projection.items[covered:covered + len(plan.source_event_ids)])
                if previous != plan.previous_summary_event_id or actual != plan.source_event_ids:
                    raise SessionStoreError("context summary source prefix changed")
            client = getattr(self, "condenser", self.model)
            if hasattr(client, "commit"):
                return client.commit(plan, items, history_store=self.session_store)
            return self.session_store.append_derived_events(items, turn_id=plan.turn_id)

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
        self._append_condensation_events(plan, [
            ("model_usage", usage_payload),
            ("context_condensation_attempt", attempt_payload),
        ], compare=False)

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


    def _invalid_summary_reason(self, response: LLMResponse) -> str | None:
        if response.finish_reason != "stop":
            return "missing_finish_reason" if response.finish_reason is None else f"finish_reason_{response.finish_reason}"
        if response.tool_calls:
            return "tool_calls"
        if not (response.text or "").strip():
            return "empty_summary"
        return None


    def _condenser_reasoning_effort(self) -> str | None:
        if not self.model_config.reasoning_enabled:
            return None
        if self.model_config.provider == "glm" and self.model_config.resolved_model == "glm-5.3":
            return "low"
        if self.model_config.provider == "deepseek":
            return "low"
        return "high"


    @staticmethod
    def _seq_from_event_id(value: str) -> int:
        return int(value.split(":", 1)[1])


    @staticmethod
    def _compat_budget_report(caps: ModelCapabilities):
        from codeagent.context.models import BudgetReport
        return BudgetReport(0, caps.usable_input_budget)
