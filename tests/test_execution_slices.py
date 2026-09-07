from typer.testing import CliRunner
import pytest

import codeagent.cli as cli_module
from codeagent.cli import app
from codeagent.config import RuntimeConfig
from codeagent.context.builder import ContextManager
from codeagent.context.models import BudgetComponent, BudgetObservation, BudgetReport, ContextBudgetExceeded
from codeagent.model_gateway.base import BaseModelClient, LLMResponse, LLMToolCall, ModelRequest
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.runner import AgentRunner
from codeagent.runtime.turn_budget import increase_turn_budget, store_turn_budget
from codeagent.session.store import SessionStore
from codeagent.tools.fs_read import build_fs_tools
from codeagent.tools.registry import ToolRegistry
from codeagent.workspace.workspace import Workspace


class ToolUntilFinalModel(BaseModelClient):
    def __init__(self, tool_steps: int) -> None:
        self.tool_steps = tool_steps
        self.calls = 0
        self.requests = []

    def complete(self, request: ModelRequest) -> LLMResponse:
        self.requests.append(request)
        self.calls += 1
        if self.calls <= self.tool_steps:
            return LLMResponse(tool_calls=[LLMToolCall(
                call_id=f"call-{self.calls}", name="list_dir", arguments={"path": "."},
            )])
        return LLMResponse(text="done")


def _runner(tmp_path, model, config):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = Workspace(tmp_path)
    store = SessionStore(workspace.root).create()
    registry = ToolRegistry(workspace_context=workspace.context)
    for tool in build_fs_tools(workspace.context):
        registry.register(tool)
    return store, AgentRunner(store, model, registry, AutoApprovalGate(False), config=config)


def test_slice_continuation_keeps_one_user_turn_and_no_fake_final(tmp_path):
    store, runner = _runner(tmp_path, ToolUntilFinalModel(5), RuntimeConfig(
        max_steps_per_turn=2, max_model_steps_per_user_turn=8,
    ))

    first = runner.run_turn("original task with constraints")
    assert first.status == "slice_exhausted"
    assert not [event for event in store.read_events() if event.type == "assistant_message"]

    second = runner.continue_turn()
    assert second.status == "slice_exhausted"
    final = runner.continue_turn()
    assert final.status == "completed" and final.final_text == "done"

    events = store.read_events()
    assert sum(event.type == "user_message" for event in events) == 1
    assert sum(event.type == "execution_slice_exhausted" for event in events) == 2
    assert sum(event.type == "assistant_message" for event in events) == 1
    assert len({event.turn_id for event in events if event.type != "session_created"}) == 1


def test_continuation_context_keeps_original_request_and_control_hint(tmp_path):
    model = ToolUntilFinalModel(2)
    store, runner = _runner(tmp_path, model, RuntimeConfig(
        max_steps_per_turn=2, max_model_steps_per_user_turn=6,
    ))
    assert runner.run_turn("keep this exact original requirement").status == "slice_exhausted"
    runner.continue_turn()
    request = model.requests[2]
    rendered = "\n".join(str(item.get("content") or "") for item in request.messages)
    assert "keep this exact original requirement" in rendered
    assert "同一 UserTurn 的后续执行切片" in rendered
    assert sum(event.type == "user_message" for event in store.read_events()) == 1


def test_overall_budget_waits_without_closing_user_turn_or_running_extra_steps(tmp_path):
    store, runner = _runner(tmp_path, ToolUntilFinalModel(100), RuntimeConfig(
        max_steps_per_turn=2, max_model_steps_per_user_turn=3,
    ))
    assert runner.run_turn("long task").status == "slice_exhausted"
    stopped = runner.continue_turn()
    assert stopped.status == "model_step_budget_exhausted"
    assert stopped.steps_used_in_turn == 3
    assert not any(event.type in {"assistant_message", "turn_terminated"} for event in store.read_events())
    assert runner.continue_turn().status == "model_step_budget_exhausted"
    assert runner.model.calls == 3
    assert sum(event.type == "turn_budget_exhausted" for event in store.read_events()) == 1
    assert store_turn_budget(store).waiting


def test_increase_preserves_turn_count_and_does_not_change_next_turn_default(tmp_path):
    model = ToolUntilFinalModel(4)
    store, runner = _runner(tmp_path, model, RuntimeConfig(max_steps_per_turn=2, max_model_steps_per_user_turn=2))
    assert runner.run_turn("original request").status == "model_step_budget_exhausted"
    original = store_turn_budget(store)
    increase_turn_budget(store, turn_id=original.turn_id, expected_limit=2, new_limit=6)
    assert runner.continue_turn().status == "slice_exhausted"
    assert runner.continue_turn().status == "completed"
    assert store_turn_budget(store).used == 5
    assert store_turn_budget(store).limit == 6
    assert store_turn_budget(store).turn_id == original.turn_id
    assert sum(event.type == "user_message" for event in store.read_events()) == 1
    ContextManager(store).build()  # 扩额不破坏 tool-call/result 协议。
    runner.run_turn("new request")
    assert store_turn_budget(store).limit == 2
    assert store_turn_budget(store).used == 1


@pytest.mark.parametrize("new_limit", [True, 2, 1, 2.5, "6", 1001])
def test_invalid_budget_increases_do_not_mutate_events(tmp_path, new_limit):
    store, runner = _runner(tmp_path, ToolUntilFinalModel(100), RuntimeConfig(max_steps_per_turn=2, max_model_steps_per_user_turn=2))
    runner.run_turn("task")
    before = store.events_path.read_bytes()
    with pytest.raises(ValueError):
        increase_turn_budget(store, turn_id=store_turn_budget(store).turn_id, expected_limit=2, new_limit=new_limit)
    assert store.events_path.read_bytes() == before


def test_budget_increase_accepts_explicit_increment_and_total_limit(tmp_path):
    store, runner = _runner(tmp_path, ToolUntilFinalModel(100), RuntimeConfig(
        max_steps_per_turn=2, max_model_steps_per_user_turn=2,
    ))
    runner.run_turn("task")
    budget = store_turn_budget(store)
    increase_turn_budget(store, turn_id=budget.turn_id, expected_limit=2, additional_steps=3)
    event = next(e for e in reversed(store.read_events()) if e.type == "turn_budget_increased")
    assert event.payload == {
        "previous_limit": 2, "new_limit": 5, "additional_steps": 3,
        "mode": "additional_steps", "steps_used_in_turn": 2, "source": "user",
    }
    runner.continue_turn()
    runner.continue_turn()
    budget = store_turn_budget(store)
    increase_turn_budget(store, turn_id=budget.turn_id, expected_limit=5, new_limit=7)
    event = next(e for e in reversed(store.read_events()) if e.type == "turn_budget_increased")
    assert event.payload["mode"] == "total_limit"
    assert event.payload["new_limit"] == 7 and event.payload["additional_steps"] == 2


@pytest.mark.parametrize("kwargs", [{}, {"new_limit": 4, "additional_steps": 2}, {"additional_steps": True},
                                     {"additional_steps": 0}, {"additional_steps": 999}])
def test_budget_increase_requires_one_valid_user_specification(tmp_path, kwargs):
    store, runner = _runner(tmp_path, ToolUntilFinalModel(100), RuntimeConfig(
        max_steps_per_turn=2, max_model_steps_per_user_turn=2,
    ))
    runner.run_turn("task")
    budget = store_turn_budget(store)
    before = store.events_path.read_bytes()
    with pytest.raises(ValueError):
        increase_turn_budget(store, turn_id=budget.turn_id, expected_limit=2, **kwargs)
    assert store.events_path.read_bytes() == before


@pytest.mark.parametrize("reason", ["user_stop", "context_budget_exceeded", "runtime_error", "model_step_budget_exhausted"])
def test_increase_does_not_reopen_terminal_or_legacy_turns(tmp_path, reason):
    store, runner = _runner(tmp_path, ToolUntilFinalModel(100), RuntimeConfig(max_steps_per_turn=1, max_model_steps_per_user_turn=1))
    runner.run_turn("task")
    store.append_event("turn_terminated", {"reason": reason})
    with pytest.raises(ValueError):
        increase_turn_budget(store, turn_id=store_turn_budget(store).turn_id, expected_limit=1, new_limit=5)


def test_cli_auto_continues_slices_and_explicit_budget_command_preserves_request(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(workspace, session_root=tmp_path / "sessions").create(
        provider="fake", runtime_options={"max_steps_per_turn": 1, "max_model_steps_per_user_turn": 2},
    )
    model = ToolUntilFinalModel(3)
    monkeypatch.setattr(cli_module, "build_model_client", lambda _: model)
    monkeypatch.setattr(cli_module.Confirm, "ask", lambda *a, **kw: pytest.fail("slice must not ask for continuation"))
    result = CliRunner().invoke(app, ["resume", store.session_id, "--session-root", str(store.session_root)],
                                input="original task\n/continue\n/budget +4\n/exit\n")
    assert result.exit_code == 0, result.output
    assert "Step Budget Reached" in result.output and "done" in result.output
    assert model.calls == 4
    assert sum(event.type == "user_message" for event in store.read_events()) == 1


def test_stop_closes_unexecuted_tool_calls_before_followup(tmp_path):
    class MultipleCalls(ToolUntilFinalModel):
        def complete(self, request):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(tool_calls=[
                    LLMToolCall(call_id=f"call-{i}", name="list_dir", arguments={"path": "."}) for i in range(2)
                ])
            return LLMResponse(text="followup")
    model = MultipleCalls(0)
    store, runner = _runner(tmp_path, model, RuntimeConfig())
    stopped = False
    def progress(_):
        nonlocal stopped
        stopped = True
    runner.progress_callback = progress
    runner.cancellation_callback = lambda: stopped
    assert runner.run_turn("task").status == "stopped"
    assert sum(e.type == "tool_result" for e in store.read_events()) == 1
    assert any(e.type == "tool_denied" and e.payload["reason"] == "user_stop" for e in store.read_events())
    runner.cancellation_callback = lambda: False
    assert runner.run_turn("followup").status == "completed"
    ContextManager(store).build()


def test_workspace_rejection_is_not_reset_by_auto_slice_or_budget_increase(tmp_path):
    from codeagent.application.session_runtime import build_agent_runner
    class EditModel(ToolUntilFinalModel):
        def complete(self, request):
            self.calls += 1
            return LLMResponse(tool_calls=[LLMToolCall(call_id=f"edit-{self.calls}", name="apply_workspace_edit", arguments={
                "operations": [{"op": "create_file", "path": "new.txt", "content": "x"}],
            })])
    class RejectGate(AutoApprovalGate):
        calls = 0
        def request_workspace_upgrade(self, *_):
            self.calls += 1
            return False
    store = SessionStore(tmp_path).create()
    model, gate = EditModel(10), RejectGate(False)
    def build():
        return build_agent_runner(store, model_config=cli_module.ModelConfig(), model_factory=lambda _: model,
                                  approval_gate=gate, runtime_config=RuntimeConfig(max_steps_per_turn=1, max_model_steps_per_user_turn=2))
    runner = build()
    assert runner.run_turn("edit").status == "slice_exhausted"
    assert runner.continue_turn().status == "model_step_budget_exhausted"
    budget = store_turn_budget(store)
    increase_turn_budget(store, turn_id=budget.turn_id, expected_limit=2, new_limit=3)
    assert build().continue_turn().status == "model_step_budget_exhausted"
    assert gate.calls == 1
    assert not (tmp_path / "new.txt").exists()
    assert store.workspace_state().value == "source_only"


def test_resume_continue_command_does_not_append_user_message(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_root = tmp_path / "sessions"
    store = SessionStore(workspace, session_root=session_root).create(provider="fake", model="fake")
    store.append_event("user_message", {"message": "original task"})
    store.append_event("execution_slice_exhausted", {"steps_used_in_turn": 12, "candidate_revision": 0,
                                                       "workspace_state": "source_only"})

    monkeypatch.setattr(cli_module, "build_model_client", lambda config: ToolUntilFinalModel(0))
    result = CliRunner().invoke(app, ["resume", store.session_id, "--session-root", str(session_root)],
                                input="/continue\n/exit\n")

    assert result.exit_code == 0, result.output
    assert "done" in result.output
    assert sum(event.type == "user_message" for event in store.read_events()) == 1


def test_context_drops_old_preambles_but_keeps_protocol_pairs(tmp_path):
    store, runner = _runner(tmp_path, ToolUntilFinalModel(6), RuntimeConfig(
        max_steps_per_turn=6, max_model_steps_per_user_turn=8,
    ))
    runner.run_turn("original")
    messages = ContextManager(store).build()
    assistants = [item for item in messages if item.get("tool_calls")]
    tools = [item for item in messages if item.get("role") == "tool"]
    assert len(assistants) == len(tools) == 6
    assert all(item["tool_call_id"] == assistants[index]["tool_calls"][0]["id"] for index, item in enumerate(tools))


def test_context_budget_failure_is_recorded_and_cli_does_not_crash(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_root = tmp_path / "sessions"
    store = SessionStore(workspace, session_root=session_root).create(provider="fake", model="fake")

    def fail_build(self, **kwargs):
        raise ContextBudgetExceeded(BudgetReport(
            23_503, 22_000, reductions=("minimum_set",),
            minimum_set_components=(BudgetComponent("current_user_message", 23_000, 1),),
            largest_observations=(BudgetObservation("read_file", "call-1", 10_000, "large.py"),),
            component_estimated_tokens=23_000, estimation_residual=503,
        ))

    monkeypatch.setattr(ContextManager, "build", fail_build)
    result = CliRunner().invoke(
        app,
        ["resume", store.session_id, "--session-root", str(session_root)],
        input="continue the task\n/exit\n",
    )

    assert result.exit_code == 0, result.output
    assert "Context Capacity Reached" in result.output
    assert "23503" in result.output and "22000" in result.output
    events = store.read_events()
    terminated = next(event for event in events if event.type == "turn_terminated")
    assert terminated.payload["reason"] == "context_budget_exceeded"
    assert terminated.payload["estimated_tokens"] == 23_503
    assert terminated.payload["usable_tokens"] == 22_000
    assert terminated.payload["minimum_set_components"] == [{
        "category": "current_user_message", "estimated_tokens": 23_000, "item_count": 1,
    }]
    assert terminated.payload["largest_observations"][0]["path"] == "large.py"
    assert terminated.payload["estimation_residual"] == 503
    assert not any(event.type == "assistant_message" for event in events)
