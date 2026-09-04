from typer.testing import CliRunner

import codeagent.cli as cli_module
from codeagent.cli import app
from codeagent.config import RuntimeConfig
from codeagent.context.builder import ContextManager
from codeagent.context.models import BudgetComponent, BudgetObservation, BudgetReport, ContextBudgetExceeded
from codeagent.model_gateway.base import BaseModelClient, LLMResponse, LLMToolCall, ModelRequest
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.runner import AgentRunner
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


def test_overall_budget_terminates_without_success_assistant_message(tmp_path):
    store, runner = _runner(tmp_path, ToolUntilFinalModel(100), RuntimeConfig(
        max_steps_per_turn=2, max_model_steps_per_user_turn=3,
    ))
    assert runner.run_turn("long task").status == "slice_exhausted"
    stopped = runner.continue_turn()
    assert stopped.status == "model_step_budget_exhausted"
    assert stopped.steps_used_in_turn == 3
    assert any(event.type == "turn_terminated" and event.payload["reason"] == "model_step_budget_exhausted"
               for event in store.read_events())
    assert not any(event.type == "assistant_message" for event in store.read_events())


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
