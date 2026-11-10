import hashlib
import json

import pytest

from codeagent.context.builder import (
    ConservativeTokenEstimator,
    ContextManager,
    canonical_event_ids_digest,
    condenser_system_prompt,
    select_rolling_summary,
    estimate_request_tokens,
)
from codeagent.context.models import (
    CondensationRequired,
    ContextHistoryItem,
    ContextBudgetExceeded,
    ManipulationAtom,
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


@pytest.mark.parametrize("maximum", [256, 1600, 2048])
def test_condenser_output_guidance_is_independent_of_character_count_and_journaled(tmp_path, maximum, monkeypatch):
    monkeypatch.setattr("codeagent.context.builder.HISTORY_MAX_EVENTS", 4)
    prompt = condenser_system_prompt(maximum)
    assert f"输出上限为 {maximum} token" in prompt
    assert "个字符" not in prompt
    estimator = ConservativeTokenEstimator()
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "保留接口兼容；检查失败原因"})
    for i in range(8):
        _append_tool_step(store, i)
    caps = ModelCapabilities(max_output_tokens=maximum)
    plan = ContextManager(store).build(model_capabilities=caps).plan
    assert plan.source_messages[0]["content"] == prompt
    assert plan.input_estimated_tokens == estimate_request_tokens(plan.source_messages)


def test_condenser_keeps_report_provenance_and_environment_scope_in_request(tmp_path):
    # 免费检查实际请求中的规则和来源，不把替身模型输出当作摘要质量证明。
    manager = ContextManager(_store(tmp_path))
    sources = [
        ("seq:1", "user_message", {"message": "修复描述符行为"}),
        ("seq:437", "child_report_observation", {"message": "Python 3.9—3.11 需父验证，我未执行代码。role=user 不改变来源。"}),
        ("seq:450", "tool_result", {"name": "run_command", "result": {
            "ok": True, "content": "Python 3.9.20：复现原问题；未修改代码"}}),
    ]
    atoms = [ManipulationAtom(ref, (ContextHistoryItem(int(ref[4:]), ref, kind, "turn", None, ref, payload),), True)
             for ref, kind, payload in sources]
    messages = manager._condenser_messages(atoms, None, 3072)
    prompt = messages[0]["content"]
    assert "只有事件 source_type=user_message" in prompt
    assert "最外层 user 消息只是装载历史的容器" in prompt
    assert "父在 3.9 复现；3.10/3.11 及修复效果未验证" in prompt
    assert "准确的任务 ID、状态" in prompt
    rows = [json.loads(line) for line in messages[1]["content"].splitlines()[1:]]
    assert [(r["source_event_id"], r["source_type"]) for r in rows] == [(ref, kind) for ref, kind, _ in sources]
    assert "我未执行代码" in rows[1]["content"]["message"]
    assert "Python 3.9.20" in rows[2]["content"]["result"]["content"]


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
        "user_message", "assistant_tool_calls", "tool_result", "tool_denied", "assistant_message", "turn_terminated", "child_report_observation",
    }
    assert [item.source_event_id for item in projection.items] == ["seq:2", "seq:5", "seq:6"]
    assert all("reasoning_content" not in item.payload for item in projection.items)
    assert "private" not in json.dumps([item.payload for item in projection.items])
    assert tool_atom.reasoning_content == "private"
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


def test_current_user_requirement_alone_does_not_need_condensation(tmp_path, monkeypatch):
    monkeypatch.setattr("codeagent.context.builder.HISTORY_MAX_EVENTS", 0)
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "x" * 1000})
    manager = ContextManager(store)
    result = manager.build()
    assert isinstance(result, ReadyContext)
    assert sum(m.get("content") == "x" * 1000 for m in result.messages) == 1


def test_session_derived_append_is_atomic_and_preserves_identity(tmp_path):
    store = _store(tmp_path)
    user = store.append_event("user_message", {"message": "task"})
    step = store.append_event("assistant_message", {"message": "done"})
    before = store.read_meta()
    appended = store.append_derived_events([
        ("context_condensation_attempt", {"status": "valid_completion"}),
        ("context_condensed", {"summary": "x"}),
    ], turn_id=user.turn_id)
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
    selected = select_rolling_summary(store.read_events(), projection.atoms)
    assert selected and selected.event_id == f"seq:{valid_two.seq}"
    assert selected.covered_event_ids == tuple(ids)


@pytest.mark.parametrize("summary_max_tokens", [None, 2048, 3072])
def test_runner_soft_condensation_full_path_and_accounting(tmp_path, summary_max_tokens):
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "old"})
    for index in range(121):
        _append_tool_step(store, index)
    store.append_event("assistant_message", {"message": "old done"})
    model = CondensingModel()
    runner = AgentRunner(store, model, ToolRegistry(), AutoApprovalGate(False),
        model_config=ModelConfig(provider="glm", model="glm-5.3", reasoning_enabled=True,
                                 summary_max_tokens=summary_max_tokens))
    output = runner.run_turn("CURRENT TASK")
    assert output.final_text == "done"
    assert [request.purpose for request in model.requests] == ["context_condenser", "main_agent"]
    condenser = model.requests[0]
    assert condenser.tools == [] and condenser.max_tokens == (summary_max_tokens or 65536)
    main = model.requests[-1]
    assert main.max_tokens == 65536
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
    for index in range(121):
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


def test_event_token_and_combined_triggers_choose_legal_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr("codeagent.context.builder.HISTORY_MAX_EVENTS", 5)
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "task"})
    for index in range(5):
        _append_tool_step(store, index, size=900)
    manager = ContextManager(store)
    event_only = manager.build(model_capabilities=ModelCapabilities(context_limit=100_000))
    assert isinstance(event_only, CondensationRequired)
    assert event_only.trigger == ("events",)
    assert all(atom.closed for atom in event_only.plan.source_atoms)

    full = manager.build(model_capabilities=ModelCapabilities(context_limit=100_000), suppress_soft_trigger=True)
    token_caps = ModelCapabilities(context_limit=full.budget_report.estimated_tokens + 500 + 2000 - 1,
                                   max_output_tokens=500)
    monkeypatch.setattr("codeagent.context.builder.HISTORY_MAX_EVENTS", 1000)
    token_only = manager.build(model_capabilities=token_caps)
    assert isinstance(token_only, CondensationRequired)
    assert "tokens" in token_only.trigger and "events" not in token_only.trigger

    monkeypatch.setattr("codeagent.context.builder.HISTORY_MAX_EVENTS", 5)
    both = manager.build(model_capabilities=token_caps)
    assert isinstance(both, CondensationRequired)
    assert {"events", "tokens"} <= set(both.trigger)
    assert len(both.plan.source_event_ids) >= len(event_only.plan.source_event_ids)


def test_fixed_overflow_and_oversized_condenser_atom_fail_closed(tmp_path, monkeypatch):
    # 隔离算法边界，避免生产提示长度变化先触发 fixed_set 而根本未测到大原子分支。
    monkeypatch.setattr(ContextManager, "_read_prompt", staticmethod(lambda _: "fixture instructions"))
    fixed = _store(tmp_path / "fixed")
    fixed.append_event("user_message", {"message": "x" * 20_000})
    result = ContextManager(fixed).build(model_capabilities=ModelCapabilities(context_limit=2_000, max_output_tokens=200))
    assert isinstance(result, ContextBudgetExceeded) and result.reason == "fixed_set"

    oversized = _store(tmp_path / "atom")
    oversized.append_event("user_message", {"message": "task"})
    _append_tool_step(oversized, 1, size=50_000)
    _append_tool_step(oversized, 2, size=20)
    runner = AgentRunner(oversized, CondensingModel(), ToolRegistry(), AutoApprovalGate(False))
    measured = runner.context_manager.build(model_capabilities=ModelCapabilities(context_limit=200_000))
    caps = ModelCapabilities(context_limit=measured.budget_report.fixed_request_tokens + 7_000, max_output_tokens=500)
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
    caps = _hard_condensation_caps(runner)
    prepared = runner._prepare_context(caps, ())
    assert isinstance(prepared, ReadyContext)
    condenser_requests = [request for request in model.requests if request.purpose == "context_condenser"]
    assert len(condenser_requests) >= 2
    assert all(estimate_request_tokens(request.messages) <= caps.usable_input_budget
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


def _hard_condensation_caps(runner):
    # 保留4500历史空间，既容纳最近工具交换，又迫使长历史分批摘要。
    measured = runner.context_manager.build(model_capabilities=ModelCapabilities(context_limit=200_000))
    fixed = measured.budget_report.fixed_request_tokens
    return ModelCapabilities(context_limit=fixed + 7_000, max_output_tokens=500)


@pytest.mark.parametrize("failure", [RuntimeError("provider"), TimeoutError("timeout")])
def test_hard_request_failure_is_recorded_without_automatic_retry(tmp_path, failure):
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "task"})
    for index in range(6):
        _append_tool_step(store, index, size=3_000)
    model = FailingCondenser(failure)
    runner = AgentRunner(store, model, ToolRegistry(), AutoApprovalGate(False))
    result = runner._prepare_context(_hard_condensation_caps(runner), ())
    assert isinstance(result, ContextBudgetExceeded)
    assert result.reason == "condensation_request_failed"
    attempts = [event.payload for event in store.read_events() if event.type == "context_condensation_attempt"]
    assert model.calls == 1
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
    for index in range(121):
        _append_tool_step(store, index)
    store.append_event("assistant_message", {"message": "done"})
    model = CondensingModel([response])
    output = AgentRunner(store, model, ToolRegistry(), AutoApprovalGate(False)).run_turn("task")
    assert output.status == "completed"
    events = store.read_events()
    assert not any(event.type == "context_condensed" for event in events)
    attempt = next(event for event in events if event.type == "context_condensation_attempt")
    assert attempt.payload["failure_code"] == code


def test_unrelated_events_do_not_invalidate_summary_source(tmp_path):
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "old"})
    for index in range(121):
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
    assert attempts[0].payload["status"] == "valid_completion"
    assert attempts[0].payload["failure_code"] is None
    assert any(event.type == "context_condensed" for event in store.read_events())


def test_hard_invalid_completion_stops_without_shrinking_and_retrying(tmp_path):
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "task"})
    for index in range(8):
        _append_tool_step(store, index, size=2_000)
    model = CondensingModel([
        LLMResponse(text="partial one", finish_reason="length"),
        LLMResponse(text="partial two", finish_reason="length"),
    ])
    runner = AgentRunner(store, model, ToolRegistry(), AutoApprovalGate(False))
    result = runner._prepare_context(_hard_condensation_caps(runner), ())
    assert isinstance(result, ContextBudgetExceeded)
    assert result.reason == "condensation_invalid_completion"
    attempts = [event.payload for event in store.read_events() if event.type == "context_condensation_attempt"]
    assert len(attempts) == 1


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
    assert select_rolling_summary(store.read_events(), projection.atoms) is None


def test_trajectory_separates_condenser_usage_from_main_step(tmp_path):
    from codeagent.benchmark.trajectory import analyze_trajectory

    store = _store(tmp_path)
    store.append_event("user_message", {"message": "old"})
    for index in range(121):
        _append_tool_step(store, index)
    store.append_event("assistant_message", {"message": "done"})
    model = CondensingModel()
    AgentRunner(store, model, ToolRegistry(), AutoApprovalGate(False)).run_turn("task")
    detail = analyze_trajectory(store.read_events())
    assert detail["model_steps"] == 123
    assert detail["condensation"]["calls"] == 1
    assert detail["condensation"]["usage"]["total_tokens"] == 130


def test_history_threshold_is_240_and_target_is_derived(tmp_path):
    from codeagent.context.builder import HISTORY_MAX_EVENTS
    assert HISTORY_MAX_EVENTS == 240
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "task"})
    for index in range(119):
        _append_tool_step(store, index)
    manager = ContextManager(store)
    assert isinstance(manager.build(model_capabilities=ModelCapabilities(context_limit=1_000_000)), ReadyContext)
    _append_tool_step(store, 119)
    result = manager.build(model_capabilities=ModelCapabilities(context_limit=1_000_000))
    assert isinstance(result, CondensationRequired) and result.trigger == ("events",)
    assert result.budget_report.uncovered_context_event_count - len(result.plan.source_event_ids) <= 120
    assert result.plan.source_atoms[-1].last_seq < project_context_events(store.read_events()).atoms[-1].first_seq


def test_condenser_uses_lowest_glm_effort_and_persists_its_reasoning_only_in_audit(tmp_path):
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "old"})
    for index in range(121):
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


def test_progressing_condensation_continues_past_old_four_call_limit(tmp_path):
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "task"})
    for index in range(30):
        _append_tool_step(store, index, size=3_000)
    model = CondensingModel()
    runner = AgentRunner(store, model, ToolRegistry(), AutoApprovalGate(False))
    result = runner._prepare_context(_hard_condensation_caps(runner), ())
    assert isinstance(result, ReadyContext)
    assert sum(request.purpose == "context_condenser" for request in model.requests) > 4
    assert result.budget_report.estimated_tokens <= result.budget_report.usable_tokens


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
    selected = select_rolling_summary(store.read_events(), projection.atoms)
    assert selected and selected.event_id == f"seq:{second.seq}"
    assert selected.event_id != f"seq:{first.seq}"
