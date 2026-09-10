import json

from codeagent.benchmark.accounting import aggregate_usage
from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.model_gateway.base import BaseModelClient, LLMResponse, LLMToolCall, ModelRequest, TokenUsage
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.runner import AgentRunner, MAX_CONSECUTIVE_OUTPUT_TRUNCATIONS
from codeagent.session.store import SessionStore
from codeagent.tools.base import PermissionLevel, ToolResult, ToolSpec
from codeagent.tools.registry import ToolRegistry
from codeagent.workspace.workspace import Workspace


def _usage(output_tokens: int, total_tokens: int) -> TokenUsage:
    return TokenUsage(input_tokens=total_tokens - output_tokens, output_tokens=output_tokens,
                      total_tokens=total_tokens, reasoning_tokens=output_tokens)


def _runner(tmp_path, model, *, max_steps=12, max_turn_steps=72):
    workspace = Workspace(tmp_path)
    store = SessionStore(workspace.root, session_root=tmp_path / "sessions").create(
        provider="glm", model="glm-5.3",
    )
    edits = []
    registry = ToolRegistry(workspace.context)
    registry.register(ToolSpec(
        "edit_file", "测试用受控编辑", PermissionLevel.CANDIDATE_WRITE,
        {"type": "object", "properties": {"path": {"type": "string"}}},
        lambda arguments: edits.append(arguments) or ToolResult(ok=True, content="edited"),
    ))
    runner = AgentRunner(
        store, model, registry, AutoApprovalGate(False),
        config=RuntimeConfig(max_steps_per_turn=max_steps,
                             max_model_steps_per_user_turn=max_turn_steps),
        model_config=ModelConfig(provider="glm", model="glm-5.3", max_tokens=8192,
                                 reasoning_enabled=True, reasoning_effort="max"),
    )
    return store, runner, edits


class TruncateEditFinalModel(BaseModelClient):
    def __init__(self):
        self.requests = []

    def complete(self, request: ModelRequest) -> LLMResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            return LLMResponse(
                text="partial content", reasoning_content="reasoning at the limit",
                finish_reason="length", provider_request_id="req-1", usage=_usage(8192, 9000),
            )
        if len(self.requests) == 2:
            return LLMResponse(
                tool_calls=[LLMToolCall(call_id="edit-1", name="edit_file", arguments={"path": "a.py"})],
                finish_reason="tool_calls", provider_request_id="req-2", usage=_usage(20, 120),
            )
        return LLMResponse(
            text="done", finish_reason="stop", provider_request_id="req-3", usage=_usage(4, 54),
        )


def test_length_response_continues_same_turn_preserves_context_executes_edit_and_usage(tmp_path):
    model = TruncateEditFinalModel()
    store, runner, edits = _runner(tmp_path, model)

    output = runner.run_turn("fix it")

    assert output.status == "completed" and output.final_text == "done"
    assert edits == [{"path": "a.py"}]
    assert len(model.requests) == 3
    assert model.requests[0].reasoning_effort is None
    assert model.requests[1].reasoning_effort == "low"
    assert model.requests[2].reasoning_effort is None
    second = model.requests[1].messages
    truncated = next(item for item in second if item.get("role") == "assistant")
    assert truncated["content"] == "partial content"
    assert truncated["reasoning_content"] == "reasoning at the limit"
    assert any("上一次响应达到输出长度上限" in str(item.get("content")) for item in second)
    third = json.dumps(model.requests[2].messages, ensure_ascii=False)
    assert "reasoning at the limit" not in third
    assert "partial content" not in third

    events = store.read_events()
    types = [event.type for event in events]
    assert types.index("model_output_truncated") < types.index("assistant_tool_calls") < types.index("assistant_message")
    assert sum(event.type == "assistant_message" for event in events) == 1
    assert len({event.turn_id for event in events if event.type != "session_created"}) == 1
    usages = [event.payload["usage"] for event in events if event.type == "model_usage"]
    total = aggregate_usage(usages)
    assert total["requests"] == total["requests_with_usage"] == 3
    assert total["total_tokens"] == 9174
    reasons = [event.payload["finish_reason"] for event in events if event.type == "model_usage"]
    assert reasons == ["length", "tool_calls", "stop"]


def test_missing_finish_reason_uses_explicit_max_token_fallback(tmp_path):
    class FallbackModel(BaseModelClient):
        calls = 0

        def complete(self, request):
            if request.purpose == "context_condenser":
                return LLMResponse(text="PENDING:\ncontinue", finish_reason="stop")
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(text="", usage=_usage(8192, 8200))
            return LLMResponse(text="done", finish_reason="stop", usage=_usage(2, 12))

    model = FallbackModel()
    store, runner, _ = _runner(tmp_path, model)
    assert runner.run_turn("continue").status == "completed"
    event = next(event for event in store.read_events() if event.type == "model_output_truncated")
    assert event.payload["detection"] == "usage_fallback"
    assert event.payload["finish_reason"] is None
    assert model.calls == 2


def test_truncation_continuation_survives_execution_slice_boundary(tmp_path):
    model = TruncateEditFinalModel()
    store, runner, edits = _runner(tmp_path, model, max_steps=1)

    assert runner.run_turn("fix it").status == "slice_exhausted"
    assert not any(event.type == "assistant_message" for event in store.read_events())
    assert runner.continue_turn().status == "slice_exhausted"
    assert edits == [{"path": "a.py"}]
    assert runner.continue_turn().status == "completed"
    rendered = model.requests[1].messages
    assert any(item.get("reasoning_content") == "reasoning at the limit" for item in rendered)
    assert any("上一次响应达到输出长度上限" in str(item.get("content")) for item in rendered)


def test_explicit_stop_is_not_overridden_by_usage_fallback(tmp_path):
    class StopModel(BaseModelClient):
        def complete(self, request):
            return LLMResponse(text="", finish_reason="stop", usage=_usage(8192, 8200))

    store, runner, _ = _runner(tmp_path, StopModel())
    output = runner.run_turn("done")
    assert output.status == "completed"
    assert any(event.type == "assistant_message" for event in store.read_events())
    assert not any(event.type == "model_output_truncated" for event in store.read_events())


def test_consecutive_truncation_is_bounded_and_explicitly_terminated(tmp_path):
    class AlwaysTruncated(BaseModelClient):
        calls = 0

        def complete(self, request):
            self.calls += 1
            return LLMResponse(reasoning_content=f"reasoning-{self.calls}", finish_reason="length",
                               usage=_usage(8192, 8200))

    model = AlwaysTruncated()
    store, runner, _ = _runner(tmp_path, model)
    output = runner.run_turn("do not loop")
    assert output.status == "output_truncated"
    assert model.calls == MAX_CONSECUTIVE_OUTPUT_TRUNCATIONS
    assert not any(event.type == "assistant_message" for event in store.read_events())
    terminal = store.read_events()[-1]
    assert terminal.type == "turn_terminated" and terminal.payload["reason"] == "output_truncated"


def test_consecutive_truncation_retry_only_replays_latest_reasoning(tmp_path):
    class TwiceTruncated(BaseModelClient):
        def __init__(self):
            self.requests = []

        def complete(self, request):
            self.requests.append(request)
            if len(self.requests) <= 2:
                return LLMResponse(
                    reasoning_content=f"truncated-reason-{len(self.requests)}",
                    finish_reason="length", usage=_usage(8192, 8200),
                )
            return LLMResponse(text="done", finish_reason="stop", usage=_usage(2, 12))

    model = TwiceTruncated()
    _, runner, _ = _runner(tmp_path, model)

    assert runner.run_turn("continue").status == "completed"
    second = json.dumps(model.requests[1].messages, ensure_ascii=False)
    third = json.dumps(model.requests[2].messages, ensure_ascii=False)
    assert model.requests[1].reasoning_effort == "low"
    assert model.requests[2].reasoning_effort == "low"
    assert "truncated-reason-1" in second
    assert "truncated-reason-1" not in third
    assert "truncated-reason-2" in third


def test_total_model_step_budget_takes_precedence_at_72(tmp_path):
    class SeventyTwoSteps(BaseModelClient):
        calls = 0

        def complete(self, request):
            if request.purpose == "context_condenser":
                return LLMResponse(text="PENDING:\ncontinue", finish_reason="stop")
            self.calls += 1
            if self.calls < 72:
                return LLMResponse(tool_calls=[LLMToolCall(
                    call_id=f"edit-{self.calls}", name="edit_file", arguments={"path": "a.py"},
                )], finish_reason="tool_calls")
            return LLMResponse(reasoning_content="last truncated step", finish_reason="length",
                               usage=_usage(8192, 8200))

    model = SeventyTwoSteps()
    store, runner, edits = _runner(tmp_path, model, max_steps=72, max_turn_steps=72)
    output = runner.run_turn("use the fixed budget")
    assert output.status == "model_step_budget_exhausted"
    assert output.steps_used_in_turn == model.calls == 72
    assert len(edits) == 71
    assert not any(event.type in {"assistant_message", "turn_terminated"} for event in store.read_events())
    assert sum(event.type == "turn_budget_exhausted" for event in store.read_events()) == 1
