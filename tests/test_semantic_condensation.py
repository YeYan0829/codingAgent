import hashlib
import json

import pytest

from codeagent.context.builder import (
    ConservativeTokenEstimator,
    ContextManager,
    canonical_event_ids_digest,
    select_rolling_summary,
)
from codeagent.context.models import (
    CondensationRequired,
    ContextBudgetExceeded,
    ModelCapabilities,
    ReadyContext,
)
from codeagent.context.projector import CCES_EVENT_TYPES, project_context_events
from codeagent.config import ModelConfig
from codeagent.model_gateway.base import BaseModelClient, LLMResponse, LLMToolCall, ModelRequest, TokenUsage
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.runner import AgentRunner
from codeagent.session.store import SessionStore, SessionStoreError
from codeagent.tools.registry import ToolRegistry
from codeagent.workspace.workspace import Workspace


def _store(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    return SessionStore(workspace, session_root=tmp_path / "sessions").create()


def _append_tool_step(store, index, *, reasoning=None, size=20):
    call_id = f"call-{index}"
    payload = {
        "message": f"step {index}",
        "tool_calls": [{"call_id": call_id, "name": "read_file", "arguments": {"path": f"f{index}.py"}}],
    }
    if reasoning is not None:
        payload["reasoning_content"] = reasoning
    store.append_event("assistant_tool_calls", payload)
    store.append_event("tool_result", {
        "call_id": call_id,
        "name": "read_file",
        "result": {"ok": True, "content": str(index) * size, "metadata": {"path": f"f{index}.py"}},
    })


class CondensingModel(BaseModelClient):
    def __init__(self, invalid=None):
        self.requests = []
        self.invalid = list(invalid or [])

    def complete(self, request: ModelRequest) -> LLMResponse:
        self.requests.append(request)
        if request.purpose == "context_condenser":
            if self.invalid:
                return self.invalid.pop(0)
            return LLMResponse(
                text="USER_REQUIREMENTS:\n保留真实请求。\nIMPORTANT_EVIDENCE:\n已读取目标文件。\nPENDING:\n继续任务。",
                finish_reason="stop",
                provider_request_id=f"cond-{len(self.requests)}",
                usage=TokenUsage(input_tokens=100, output_tokens=30, total_tokens=130),
            )
        return LLMResponse(
            text="done", finish_reason="stop", provider_request_id="main",
            usage=TokenUsage(input_tokens=50, output_tokens=2, total_tokens=52),
        )


def test_cces_allowlist_source_identity_reasoning_strip_and_atoms(tmp_path):
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "inspect"})
    store.append_event("model_usage", {"usage": {"total_tokens": 1}})
    store.append_event("approval_decision", {"allowed": True})
    store.append_event("assistant_tool_calls", {
        "reasoning_content": "private",
        "tool_calls": [
            {"call_id": "a", "name": "read_file", "arguments": {"path": "a.py"}},
            {"call_id": "b", "name": "read_file", "arguments": {"path": "b.py"}},
        ],
    })
    store.append_event("tool_result", {"call_id": "a", "result": {"ok": True, "content": "a"}})
    projection = project_context_events(store.read_events())
    tool_atom = projection.atoms[-1]
    assert CCES_EVENT_TYPES == {
        "user_message", "assistant_tool_calls", "tool_result", "tool_denied", "assistant_message", "turn_terminated",
    }
    assert [item.source_event_id for item in projection.items] == ["seq:2", "seq:5", "seq:6"]
    assert all("reasoning_content" not in item.payload for item in projection.items)
    assert tool_atom.closed is False and len(tool_atom.items) == 2
    store.append_event("tool_denied", {"call_id": "b", "reason": "denied"})
    tool_atom = project_context_events(store.read_events()).atoms[-1]
    assert tool_atom.closed is True and len(tool_atom.items) == 3


def test_anchor_exact_once_and_mandatory_protocol_deduplicated(tmp_path):
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "CURRENT TASK"})
    _append_tool_step(store, 1, reasoning="private reasoning")
    result = ContextManager(store).build()
    assert isinstance(result, ReadyContext)
    rendered = json.dumps(result.messages, ensure_ascii=False)
    assert rendered.count("CURRENT TASK") == 1
    assert rendered.count('"id": "call-1"') == 1
    assert rendered.count('"tool_call_id": "call-1"') == 1
    assert "private reasoning" in rendered


def test_anchor_source_has_zero_removable_raw_contribution(tmp_path):
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "x" * 1000})
    manager = ContextManager(store)
    result = manager.build(model_capabilities=ModelCapabilities(max_events=0, target_events=0))
    assert isinstance(result, CondensationRequired)
    assert result.plan.source_event_ids == ("seq:2",)
    # Anchor 仍在 fixed budget，覆盖 source 不会让 request-before 估算凭空减掉它。
    assert result.budget_report.fixed_request_tokens > manager.estimator.estimate("x" * 1000)


def test_session_derived_append_is_atomic_and_preserves_identity(tmp_path):
    store = _store(tmp_path)
    user = store.append_event("user_message", {"message": "task"})
    step = store.append_event("assistant_message", {"message": "done"})
    before = store.read_meta()
    appended = store.append_derived_events([
        ("context_condensation_attempt", {"status": "valid_completion"}),
        ("context_condensed", {"summary": "x"}),
    ], turn_id=user.turn_id, expected_event_seq=step.seq)
    after = store.read_meta()
    assert [event.model_step_id for event in appended] == [None, None]
    assert after["current_turn_id"] == before["current_turn_id"]
    assert after["current_model_step_id"] == before["current_model_step_id"]


def _summary_payload(previous, new_ids, prior_count, summary="PENDING:\ncontinue"):
    return {
        "previous_summary_event_id": previous,
        "newly_covered_from_seq": int(new_ids[0].split(":")[1]),
        "newly_covered_to_seq": int(new_ids[-1].split(":")[1]),
        "newly_covered_event_ids": list(new_ids),
        "newly_covered_event_ids_sha256": canonical_event_ids_digest(new_ids),
        "logical_covered_event_count": prior_count + len(new_ids),
        "logical_frontier_source_event_id": new_ids[-1],
        "summary": summary,
        "algorithm_version": 1,
    }


def test_chain_root_successor_invalid_descendant_and_seq_tiebreak(tmp_path):
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "first"})
    store.append_event("assistant_message", {"message": "answer"})
    store.append_event("user_message", {"message": "second"})
    projection = project_context_events(store.read_events())
    ids = [item.source_event_id for item in projection.items]
    first = store.append_derived_events([
        ("context_condensed", _summary_payload(None, ids[:2], 0)),
    ], turn_id=projection.items[-1].turn_id)[0]
    first_id = f"seq:{first.seq}"
    valid_two = store.append_derived_events([
        ("context_condensed", _summary_payload(first_id, ids[2:], 2, "PENDING:\nsecond")),
    ], turn_id=projection.items[-1].turn_id)[0]
    store.append_derived_events([
        ("context_condensed", {**_summary_payload("seq:999", ids[2:], 2), "summary": "invalid"}),
    ], turn_id=projection.items[-1].turn_id)
    selected = select_rolling_summary(
        store.read_events(), projection.atoms, ConservativeTokenEstimator(), 2048,
    )
    assert selected and selected.event_id == f"seq:{valid_two.seq}"
    assert selected.covered_event_ids == tuple(ids)


def test_runner_soft_condensation_full_path_and_accounting(tmp_path):
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "old"})
    for index in range(41):
        _append_tool_step(store, index)
    store.append_event("assistant_message", {"message": "old done"})
    model = CondensingModel()
    runner = AgentRunner(store, model, ToolRegistry(), AutoApprovalGate(False))
    output = runner.run_turn("CURRENT TASK")
    assert output.final_text == "done"
    assert [request.purpose for request in model.requests] == ["context_condenser", "main_agent"]
    condenser = model.requests[0]
    assert condenser.tools == [] and condenser.max_tokens == 2048
    main = model.requests[-1]
    rendered = json.dumps(main.messages, ensure_ascii=False)
    assert "<historical_memory>" in rendered
    assert rendered.count("CURRENT TASK") == 1
    events = store.read_events()
    attempt = next(event for event in events if event.type == "context_condensation_attempt")
    summary = next(event for event in events if event.type == "context_condensed")
    usage = [event for event in events if event.type == "model_usage"]
    assert attempt.model_step_id is None and summary.model_step_id is None
    assert [event.payload["purpose"] for event in usage] == ["context_condenser", "main_agent"]


def test_invalid_length_soft_skips_without_summary(tmp_path):
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "old"})
    for index in range(41):
        _append_tool_step(store, index)
    store.append_event("assistant_message", {"message": "old done"})
    model = CondensingModel([LLMResponse(text="partial", finish_reason="length")])
    output = AgentRunner(store, model, ToolRegistry(), AutoApprovalGate(False)).run_turn("task")
    assert output.final_text == "done"
    events = store.read_events()
    assert not any(event.type == "context_condensed" for event in events)
    attempt = next(event for event in events if event.type == "context_condensation_attempt")
    assert attempt.payload["status"] == "invalid_completion"
    assert attempt.payload["failure_code"] == "finish_reason_length"


def test_event_token_and_combined_triggers_choose_legal_prefix(tmp_path):
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "task"})
    for index in range(5):
        _append_tool_step(store, index, size=900)
    manager = ContextManager(store)
    event_only = manager.build(model_capabilities=ModelCapabilities(
        context_limit=100_000, max_events=5, target_events=3,
    ))
    assert isinstance(event_only, CondensationRequired)
    assert event_only.trigger == ("events",)
    assert all(atom.closed for atom in event_only.plan.source_atoms)

    token_only = manager.build(model_capabilities=ModelCapabilities(
        context_limit=8_000, generation_reserve=500, continuation_reserve=500,
        safety_margin=500, max_events=1000, soft_token_ratio=0.2, target_token_ratio=0.1,
        summary_max_tokens=256,
    ))
    assert isinstance(token_only, CondensationRequired)
    assert "tokens" in token_only.trigger and "events" not in token_only.trigger

    both = manager.build(model_capabilities=ModelCapabilities(
        context_limit=8_000, generation_reserve=500, continuation_reserve=500,
        safety_margin=500, max_events=5, target_events=3, soft_token_ratio=0.2,
        target_token_ratio=0.1, summary_max_tokens=256,
    ))
    assert isinstance(both, CondensationRequired)
    assert {"events", "tokens"} <= set(both.trigger)
    assert len(both.plan.source_event_ids) >= len(event_only.plan.source_event_ids)


def test_fixed_overflow_and_oversized_condenser_atom_fail_closed(tmp_path):
    fixed = _store(tmp_path / "fixed")
    fixed.append_event("user_message", {"message": "x" * 20_000})
    result = ContextManager(fixed).build(model_capabilities=ModelCapabilities(
        context_limit=2_000, generation_reserve=200, continuation_reserve=200, safety_margin=200,
        summary_max_tokens=128, condenser_safety_margin=128,
    ))
    assert isinstance(result, ContextBudgetExceeded) and result.reason == "fixed_set"

    oversized = _store(tmp_path / "atom")
    oversized.append_event("user_message", {"message": "task"})
    _append_tool_step(oversized, 1, size=50_000)
    caps = ModelCapabilities(
        context_limit=4_000, generation_reserve=500, continuation_reserve=500, safety_margin=500,
        summary_max_tokens=256, condenser_safety_margin=500, soft_token_ratio=0.1,
        target_token_ratio=0.05,
    )
    runner = AgentRunner(oversized, CondensingModel(), ToolRegistry(), AutoApprovalGate(False))
    result = runner._prepare_context(caps, ())
    assert isinstance(result, ContextBudgetExceeded)
    assert result.reason == "condenser_atom_too_large"


def test_recursive_split_and_valid_but_insufficient_rebuild(tmp_path):
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "task"})
    for index in range(12):
        _append_tool_step(store, index, size=3_000)
    model = CondensingModel()
    runner = AgentRunner(store, model, ToolRegistry(), AutoApprovalGate(False))
    caps = ModelCapabilities(
        context_limit=8_000, generation_reserve=500, continuation_reserve=500, safety_margin=500,
        summary_max_tokens=256, condenser_safety_margin=500, soft_token_ratio=0.2,
        target_token_ratio=0.15,
    )
    prepared = runner._prepare_context(caps, ())
    assert isinstance(prepared, ReadyContext)
    condenser_requests = [request for request in model.requests if request.purpose == "context_condenser"]
    assert 2 <= len(condenser_requests) <= 4
    assert all(runner.context_manager.estimator.estimate(request.messages) <= caps.condenser_usable_input_budget
               for request in condenser_requests)
    summaries = [event for event in store.read_events() if event.type == "context_condensed"]
    assert len(summaries) == len(condenser_requests)
    assert summaries[1].payload["previous_summary_event_id"] == f"seq:{summaries[0].seq}"


class FailingCondenser(BaseModelClient):
    def __init__(self, failure):
        self.failure = failure
        self.calls = 0

    def complete(self, request):
        assert request.purpose == "context_condenser"
        self.calls += 1
        raise self.failure


@pytest.mark.parametrize("failure", [RuntimeError("provider"), TimeoutError("timeout")])
def test_hard_request_failure_retries_same_plan_once_then_fails(tmp_path, failure):
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "task"})
    for index in range(6):
        _append_tool_step(store, index, size=3_000)
    model = FailingCondenser(failure)
    runner = AgentRunner(store, model, ToolRegistry(), AutoApprovalGate(False))
    result = runner._prepare_context(ModelCapabilities(
        context_limit=6_000, generation_reserve=500, continuation_reserve=500, safety_margin=500,
        summary_max_tokens=256, condenser_safety_margin=500, soft_token_ratio=0.2,
        target_token_ratio=0.1,
    ), ())
    assert isinstance(result, ContextBudgetExceeded)
    assert result.reason == "condensation_request_failed"
    attempts = [event.payload for event in store.read_events() if event.type == "context_condensation_attempt"]
    assert model.calls == 2
    assert {attempt["plan_id"] for attempt in attempts} == {attempts[0]["plan_id"]}
    assert all(attempt["status"] == "request_failed" for attempt in attempts)


@pytest.mark.parametrize("response,code", [
    (LLMResponse(text="partial", finish_reason="length"), "finish_reason_length"),
    (LLMResponse(text="summary", finish_reason=None), "missing_finish_reason"),
    (LLMResponse(text="", finish_reason="stop"), "empty_summary"),
    (LLMResponse(text="x", finish_reason="stop", tool_calls=[
        LLMToolCall(call_id="bad", name="read_file", arguments={}),
    ]), "tool_calls"),
    (LLMResponse(text="x", finish_reason="content_filter"), "finish_reason_content_filter"),
    (LLMResponse(text="x", finish_reason="future"), "finish_reason_future"),
])
def test_invalid_completion_classes_never_enter_chain(tmp_path, response, code):
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "old"})
    for index in range(41):
        _append_tool_step(store, index)
    store.append_event("assistant_message", {"message": "done"})
    model = CondensingModel([response])
    output = AgentRunner(store, model, ToolRegistry(), AutoApprovalGate(False)).run_turn("task")
    assert output.status == "completed"
    events = store.read_events()
    assert not any(event.type == "context_condensed" for event in events)
    attempt = next(event for event in events if event.type == "context_condensation_attempt")
    assert attempt.payload["failure_code"] == code


def test_metadata_race_records_invalid_and_replans_soft_without_chain(tmp_path):
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "old"})
    for index in range(41):
        _append_tool_step(store, index)
    store.append_event("assistant_message", {"message": "done"})

    class RacingModel(CondensingModel):
        def complete(self, request):
            if request.purpose == "context_condenser":
                store.append_event("execution_slice_exhausted", {"race": True})
            return super().complete(request)

    model = RacingModel()
    output = AgentRunner(store, model, ToolRegistry(), AutoApprovalGate(False)).run_turn("task")
    assert output.status == "completed"
    attempts = [event for event in store.read_events() if event.type == "context_condensation_attempt"]
    assert attempts[0].payload["status"] == "invalid_completion"
    assert attempts[0].payload["failure_code"] == "stale_frontier"
    assert not any(event.type == "context_condensed" for event in store.read_events())


def test_hard_invalid_completion_shrinks_then_stops_at_two(tmp_path):
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "task"})
    for index in range(8):
        _append_tool_step(store, index, size=2_000)
    model = CondensingModel([
        LLMResponse(text="partial one", finish_reason="length"),
        LLMResponse(text="partial two", finish_reason="length"),
    ])
    runner = AgentRunner(store, model, ToolRegistry(), AutoApprovalGate(False))
    result = runner._prepare_context(ModelCapabilities(
        context_limit=6_000, generation_reserve=500, continuation_reserve=500, safety_margin=500,
        summary_max_tokens=256, condenser_safety_margin=500, soft_token_ratio=0.2,
        target_token_ratio=0.1,
    ), ())
    assert isinstance(result, ContextBudgetExceeded)
    assert result.reason == "condensation_invalid_completion"
    attempts = [event.payload for event in store.read_events() if event.type == "context_condensation_attempt"]
    assert len(attempts) == 2
    assert attempts[0]["candidate_source_event_ids_sha256"] != attempts[1]["candidate_source_event_ids_sha256"]


def test_chain_rejects_wrong_digest_gap_order_and_invalid_descendant(tmp_path):
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "a"})
    store.append_event("assistant_message", {"message": "b"})
    store.append_event("user_message", {"message": "c"})
    projection = project_context_events(store.read_events())
    ids = [item.source_event_id for item in projection.items]
    bad_values = [
        {**_summary_payload(None, ids[:2], 0), "newly_covered_event_ids_sha256": "bad"},
        _summary_payload(None, [ids[0], ids[2]], 0),
        _summary_payload(None, list(reversed(ids[:2])), 0),
    ]
    for payload in bad_values:
        store.append_derived_events([("context_condensed", payload)], turn_id=projection.items[-1].turn_id)
    invalid_parent = store.append_derived_events([
        ("context_condensed", {**_summary_payload(None, ids[:2], 0), "algorithm_version": 99}),
    ], turn_id=projection.items[-1].turn_id)[0]
    store.append_derived_events([
        ("context_condensed", _summary_payload(f"seq:{invalid_parent.seq}", ids[2:], 2)),
    ], turn_id=projection.items[-1].turn_id)
    assert select_rolling_summary(
        store.read_events(), projection.atoms, ConservativeTokenEstimator(), 2048,
    ) is None


def test_trajectory_separates_condenser_usage_from_main_step(tmp_path):
    from codeagent.benchmark.trajectory import analyze_trajectory

    store = _store(tmp_path)
    store.append_event("user_message", {"message": "old"})
    for index in range(41):
        _append_tool_step(store, index)
    store.append_event("assistant_message", {"message": "done"})
    model = CondensingModel()
    AgentRunner(store, model, ToolRegistry(), AutoApprovalGate(False)).run_turn("task")
    detail = analyze_trajectory(store.read_events())
    assert detail["model_steps"] == 43
    assert detail["condensation"]["calls"] == 1
    assert detail["condensation"]["usage"]["total_tokens"] == 130


def test_soft_minimum_progress_skips_too_small_cut(tmp_path):
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "task"})
    for index in range(5):
        _append_tool_step(store, index)
    result = ContextManager(store).build(model_capabilities=ModelCapabilities(
        context_limit=100_000, max_events=10, target_events=10, minimum_progress=0.10,
    ))
    assert isinstance(result, ReadyContext)
    assert result.budget_report.uncovered_context_event_count == 11


def test_condenser_uses_lowest_glm_effort_and_persists_its_reasoning_only_in_audit(tmp_path):
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "old"})
    for index in range(41):
        _append_tool_step(store, index)
    store.append_event("assistant_message", {"message": "old done"})

    class ReasoningCondenser(CondensingModel):
        def complete(self, request):
            response = super().complete(request)
            if request.purpose == "context_condenser":
                response.reasoning_content = "private condenser reasoning"
            return response

    model = ReasoningCondenser()
    runner = AgentRunner(
        store, model, ToolRegistry(), AutoApprovalGate(False),
        model_config=ModelConfig(
            provider="glm", model="glm-5.3", reasoning_enabled=True, reasoning_effort="high",
        ),
    )
    assert runner.run_turn("CURRENT").status == "completed"
    condenser, main = model.requests
    assert condenser.reasoning_effort == "low" and condenser.tools == []
    assert main.reasoning_effort is None
    attempt = next(event for event in store.read_events() if event.type == "context_condensation_attempt")
    assert attempt.payload["reasoning_content"] == "private condenser reasoning"
    assert "private condenser reasoning" not in json.dumps(main.messages, ensure_ascii=False)


def test_total_condenser_call_limit_fails_closed(tmp_path):
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "task"})
    for index in range(30):
        _append_tool_step(store, index, size=3_000)
    model = CondensingModel()
    runner = AgentRunner(store, model, ToolRegistry(), AutoApprovalGate(False))
    result = runner._prepare_context(ModelCapabilities(
        context_limit=8_000, generation_reserve=500, continuation_reserve=500, safety_margin=500,
        summary_max_tokens=256, condenser_safety_margin=500, soft_token_ratio=0.2,
        target_token_ratio=0.1,
    ), ())
    assert isinstance(result, ContextBudgetExceeded)
    assert result.reason == "condensation_cycles_exhausted"
    assert sum(request.purpose == "context_condenser" for request in model.requests) == 4


def test_resume_equal_coverage_uses_later_valid_summary_event(tmp_path):
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "a"})
    store.append_event("assistant_message", {"message": "b"})
    projection = project_context_events(store.read_events())
    ids = [item.source_event_id for item in projection.items]
    first = store.append_derived_events([
        ("context_condensed", _summary_payload(None, ids, 0, "PENDING:\nfirst")),
    ], turn_id=projection.items[-1].turn_id)[0]
    second = store.append_derived_events([
        ("context_condensed", _summary_payload(None, ids, 0, "PENDING:\nsecond")),
    ], turn_id=projection.items[-1].turn_id)[0]
    selected = select_rolling_summary(
        store.read_events(), projection.atoms, ConservativeTokenEstimator(), 2048,
    )
    assert selected and selected.event_id == f"seq:{second.seq}"
    assert selected.event_id != f"seq:{first.seq}"
