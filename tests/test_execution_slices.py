from runtime_fixtures import in_process_runner
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
from codeagent.runtime.turn_state import store_turn_state
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


def test_long_turn_completes_in_one_call_without_slice_events(tmp_path):
    model = ToolUntilFinalModel(25)
    store, runner = _runner(tmp_path, model, RuntimeConfig())
    output = runner.run_turn("original task with constraints")
    assert output.status == "completed" and output.final_text == "done"
    assert len(output.steps) == 25 and model.calls == 26
    events = store.read_events()
    assert sum(event.type == "user_message" for event in events) == 1
    assert not any(event.type == "execution_slice_exhausted" for event in events)
    assert sum(event.type == "assistant_message" for event in events) == 1
    assert len({event.turn_id for event in events if event.turn_id is not None}) == 1


def test_context_keeps_original_request_without_slice_hint(tmp_path):
    model = ToolUntilFinalModel(13)
    store, runner = _runner(tmp_path, model, RuntimeConfig())
    assert runner.run_turn("keep this exact original requirement").status == "completed"
    rendered = "\n".join(str(item.get("content") or "") for item in model.requests[13].messages)
    assert "keep this exact original requirement" in rendered
    assert "后续执行切片" not in rendered
    assert sum(event.type == "user_message" for event in store.read_events()) == 1


def test_manual_stop_still_interrupts_after_old_slice_boundary(tmp_path):
    model = ToolUntilFinalModel(100)
    store, runner = _runner(tmp_path, model, RuntimeConfig())
    runner.cancellation_callback = lambda: model.calls >= 20
    output = runner.run_turn("keep investigating")
    assert output.status == "stopped"
    assert model.calls == 20
    assert len(output.steps) == 19  # Stop 后不执行刚到达响应中的工具。
    assert not any(e.type == "execution_slice_exhausted" for e in store.read_events())
    assert store.read_events()[-1].payload["reason"] == "user_stop"


def test_old_slice_option_is_ignored_when_loading_session_configuration():
    config = RuntimeConfig.from_options({"max_steps_per_turn": 1})
    assert not hasattr(config, "max_steps_per_turn")














def test_cli_ignores_historical_limits_and_preserves_request(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(workspace, session_root=tmp_path / "sessions").create(
        provider="fake", runtime_options={"max_steps_per_turn": 1, "max_model_steps_per_user_turn": 2},
    )
    (workspace / "README.md").write_text("runtime fixture\n")
    result = CliRunner().invoke(app, ["resume", store.session_id, "--session-root", str(store.session_root)],
                                input="original task\n/exit\n")
    assert result.exit_code == 0, result.output
    assert "Step Budget Reached" not in result.output and "runtime fixture" in result.output
    assert sum(e.type == "model_request_reserved" for e in store.read_events()) == 3
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


def test_legacy_workspace_denial_does_not_block_explicit_resume(tmp_path):
    from codeagent.application.session_runtime import build_agent_runner
    from runtime_fixtures import make_bug_repo
    class EditModel(ToolUntilFinalModel):
        def complete(self, request):
            self.calls += 1
            if self.calls > 1:
                return LLMResponse(text="done")
            return LLMResponse(tool_calls=[LLMToolCall(call_id=f"edit-{self.calls}", name="apply_workspace_edit", arguments={
                "operations": [{"op": "create_file", "path": "new.txt", "content": "x"}],
            })])
    repo = make_bug_repo(tmp_path)
    store = SessionStore(repo, session_root=tmp_path / "sessions").create()
    store.append_event("user_message", {"message": "edit"})
    store.append_event("approval_decision", {"kind": "workspace_upgrade", "allowed": False})
    store.append_event("execution_slice_exhausted", {})
    model = EditModel(10)
    def build():
        return in_process_runner(store, model, model_config=cli_module.ModelConfig(),
                                  approval_gate=AutoApprovalGate(False), runtime_config=RuntimeConfig())
    runner = build()
    assert runner.continue_turn().status in {"completed", "completed_with_warnings"}
    assert (store.workspace_context().active_root / "new.txt").read_text() == "x"
    assert (repo / "new.txt").read_text() == "x"
    assert store.workspace_state().value == "current_project"
    assert not any(e.type == "approval_requested" for e in store.read_events())


def test_resume_continue_command_does_not_append_user_message(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_root = tmp_path / "sessions"
    store = SessionStore(workspace, session_root=session_root).create(provider="fake", model="fake")
    store.append_event("user_message", {"message": "original task"})
    store.append_event("execution_slice_exhausted", {"steps_used_in_turn": 12, "candidate_revision": 0,
                                                       "workspace_state": "source_only"})

    monkeypatch.setattr(cli_module, "build_runner", lambda loaded, ws, **kwargs: in_process_runner(loaded, ToolUntilFinalModel(0), approval_gate=AutoApprovalGate(False), model_config=kwargs["model_config"]))
    result = CliRunner().invoke(app, ["resume", store.session_id, "--session-root", str(session_root)],
                                input="/continue\n/exit\n")

    assert result.exit_code == 0, result.output
    assert "done" in result.output
    assert sum(event.type == "user_message" for event in store.read_events()) == 1


def test_context_drops_old_preambles_but_keeps_protocol_pairs(tmp_path):
    store, runner = _runner(tmp_path, ToolUntilFinalModel(6), RuntimeConfig(

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
