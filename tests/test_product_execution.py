from __future__ import annotations
from runtime_fixtures import in_process_runner

import threading

import pytest

from codeagent.product.execution import ExecutionSupervisor
from codeagent.product.service import ProductApplicationService, ProductServiceError
from codeagent.runtime.runner import RunnerOutput
from codeagent.session.store import SessionStore


class BlockingRunner:
    def __init__(self, store, entered: threading.Event, release: threading.Event, **_kwargs):
        self.store = store
        self.entered = entered
        self.release = release

    def run_turn(self, message):
        self.store.append_event("user_message", {"message": message})
        self.entered.set()
        assert self.release.wait(2)
        self.store.append_event("assistant_message", {"message": "done"})
        return RunnerOutput(final_text="done", status="completed")

    def continue_turn(self):
        self.store.append_event("assistant_message", {"message": "continued"})
        return RunnerOutput(final_text="continued", status="completed")


class ApprovalRunner:
    def __init__(self, store, entered, release, approval_gate, **_kwargs):
        self.store, self.gate = store, approval_gate

    def run_turn(self, message):
        self.store.append_event("user_message", {"message": message})
        from codeagent.tools.base import PermissionLevel, ToolSpec
        from codeagent.model_gateway.base import LLMToolCall
        tool = ToolSpec("inspect_tool", "Inspect executable", PermissionLevel.EXEC_READONLY, {}, lambda _: None)
        allowed = self.gate.request(tool, LLMToolCall(call_id="inspect", name="inspect_tool", arguments={"content": "secretless"}))
        self.store.append_event("assistant_message", {"message": "allowed" if allowed else "rejected"})
        return RunnerOutput(final_text="done")

    def continue_turn(self):
        raise AssertionError("not used")


def _service(tmp_path, runner_factory):
    entered = threading.Event()
    release = threading.Event()
    notifications = []
    runner_options = []

    def factory(store, **kwargs):
        runner_options.append(kwargs)
        return runner_factory(store, entered, release, **kwargs)

    supervisor = ExecutionSupervisor(
        lambda method, params: notifications.append((method, params)), runner_factory=factory,
    )
    service = ProductApplicationService(tmp_path / "sessions", supervisor=supervisor)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    detail = service.create_session(workspace, provider="fake")
    return service, detail["sessionId"], entered, release, notifications, runner_options


def test_execution_is_async_and_one_active_job_per_session(tmp_path):
    service, session_id, entered, release, notifications, runner_options = _service(tmp_path, BlockingRunner)

    started = service.run_session(session_id, "hello")
    assert started["executionState"] == "running"
    assert started["approvalMode"] == "interactive"
    assert entered.wait(1)
    assert runner_options[0]["approval_gate"].manages_events is True
    assert not hasattr(runner_options[0]["runtime_config"], "max_steps_per_turn")
    assert not hasattr(runner_options[0]["runtime_config"], "max_model_steps_per_user_turn")
    assert service.get_session(session_id)["executionState"] == "running"
    with pytest.raises(ProductServiceError, match="active execution"):
        service.run_session(session_id, "second")

    release.set()
    assert _wait_until(lambda: service.get_session(session_id)["executionState"] == "idle")
    detail = service.get_session(session_id)
    assert detail["turns"][0]["userMessage"] == "hello"
    assert detail["turns"][0]["items"][-1]["text"] == "done"
    assert any(method == "session/event" for method, _ in notifications)
    assert notifications[-1][0] == "session/executionStateChanged"


def test_continue_uses_same_background_execution_path(tmp_path):
    service, session_id, _entered, _release, _notifications, runner_options = _service(tmp_path, BlockingRunner)
    store = service._load(session_id)
    store.append_event("user_message", {"message": "unfinished"})
    store.append_event("execution_slice_exhausted", {})

    started = service.continue_session(session_id)

    assert started["approvalMode"] == "interactive"
    assert _wait_until(lambda: service.get_session(session_id)["executionState"] == "idle")
    assert runner_options[0]["approval_gate"].manages_events is True
    assert service.get_session(session_id)["turns"][0]["items"][-1]["text"] == "continued"


def test_execution_reloads_independent_model_output_settings(tmp_path):
    service, _, _entered, _release, _notifications, runner_options = _service(tmp_path, BlockingRunner)
    detail = service.create_session(tmp_path / "workspace", max_tokens=6000, summary_max_tokens=3072)
    session_id = detail["sessionId"]
    store = service._load(session_id)
    store.append_event("user_message", {"message": "unfinished"})
    service.continue_session(session_id)
    assert _wait_until(lambda: service.get_session(session_id)["executionState"] == "idle")
    config = runner_options[0]["model_config"]
    assert config.resolved_max_tokens == 6000 and config.summary_max_tokens == 3072


def test_approval_waits_without_blocking_service_and_resolves(tmp_path):
    service, session_id, _entered, _release, notifications, _options = _service(tmp_path, ApprovalRunner)
    service.run_session(session_id, "edit")
    assert _wait_until(lambda: service.get_session(session_id)["executionState"] == "awaiting_approval")

    detail = service.get_session(session_id)
    pending = detail["pendingApproval"]
    assert pending["kind"] == "tool"
    assert detail["availableActions"]["canResolveApproval"] is True
    assert "content" not in str(pending["summary"]["arguments"])
    service.resolve_approval(session_id, pending["approvalId"], True)

    assert _wait_until(lambda: service.get_session(session_id)["executionState"] == "idle")
    assert service.get_session(session_id)["turns"][0]["items"][-1]["text"] == "allowed"
    assert any(method == "approval/requested" for method, _ in notifications)


def test_stop_is_idempotent_and_records_request_and_acknowledgement(tmp_path):
    service, session_id, entered, release, _notifications, _options = _service(tmp_path, BlockingRunner)
    service.run_session(session_id, "long task")
    assert entered.wait(1)

    first = service.stop_session(session_id)
    second = service.stop_session(session_id)
    assert first["accepted"] is True and second["accepted"] is True
    assert service.get_session(session_id)["executionState"] == "stopping"
    release.set()

    assert _wait_until(lambda: service.get_session(session_id)["executionState"] == "idle")
    types = [event.type for event in service._load(session_id).read_events()]
    assert types.count("execution_stop_requested") == 1
    assert types.count("execution_stop_acknowledged") == 1


def _wait_until(predicate, timeout=2.0):
    event = threading.Event()
    deadline_steps = int(timeout / 0.01)
    for _ in range(deadline_steps):
        if predicate():
            return True
        event.wait(0.01)
    return predicate()


class CountingModel:
    def __init__(self, tool_steps=50, block_at=None):
        self.calls = 0
        self.condenser_calls = 0
        self.tool_steps, self.block_at = tool_steps, block_at
        self.entered, self.release = threading.Event(), threading.Event()

    def complete(self, request):
        from codeagent.model_gateway.base import LLMResponse, LLMToolCall
        if request.purpose == "context_condenser":
            self.condenser_calls += 1
            return LLMResponse(text="PENDING:\ncontinue", finish_reason="stop")
        self.calls += 1
        if self.calls == self.block_at:
            self.entered.set()
            assert self.release.wait(3)
        if self.calls > self.tool_steps:
            return LLMResponse(text="done")
        return LLMResponse(tool_calls=[LLMToolCall(call_id=f"c{self.calls}", name="list_dir", arguments={"path": "."})])


def _real_service(tmp_path, model):
    from codeagent.application.session_runtime import build_agent_runner
    notifications = []
    def factory(store, **kwargs):
        return in_process_runner(store, model, **kwargs)
    supervisor = ExecutionSupervisor(lambda method, params: notifications.append((method, params)), runner_factory=factory)
    service = ProductApplicationService(tmp_path / "sessions", supervisor=supervisor)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    detail = service.create_session(workspace, provider="fake")
    return service, detail["sessionId"], factory, notifications






def test_stop_during_running_turn_does_not_start_another_model_step(tmp_path):
    model = CountingModel(tool_steps=100, block_at=2)
    service, sid, _factory, _notifications = _real_service(tmp_path, model)
    service.run_session(sid, "task")
    assert model.entered.wait(1)
    service.stop_session(sid)
    model.release.set()
    assert _wait_until(lambda: service.supervisor.state(sid) == "idle")
    assert model.calls == 2
    detail = service.get_session(sid)
    assert detail["turnBudget"]["closed"]
    assert not detail["availableActions"]["canIncreaseBudget"]
    assert not detail["availableActions"]["canContinue"]


@pytest.mark.parametrize("request_network", [False, True])
def test_real_runner_auto_isolates_before_any_optional_resource_approval(tmp_path, request_network):
    import hashlib
    from codeagent.application.session_runtime import build_agent_runner
    from codeagent.model_gateway.base import LLMResponse, LLMToolCall
    from codeagent.runtime.sandbox_executor import SandboxedCommandExecutor
    from runtime_fixtures import make_bug_repo, git, HostFixtureBackend
    from runtime_fixtures import state

    repo = make_bug_repo(tmp_path)
    target = repo / "sort_utils.py"
    target.write_text("# user staged\n" + target.read_text())
    git(repo, "add", "sort_utils.py")
    target.write_text(target.read_text() + "# user unstaged\n")
    original = state(repo)
    source_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    class Model:
        calls = 0

        def complete(self, _request):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(tool_calls=[LLMToolCall(call_id="edit", name="apply_workspace_edit", arguments={"operations": [{
                    "op": "replace_text", "path": "sort_utils.py", "expected_sha256": source_hash,
                    "old_text": "reverse=True", "new_text": "reverse=False",
                }]})])
            if self.calls == 2 and request_network:
                return LLMResponse(tool_calls=[LLMToolCall(call_id="network", name="run_command", arguments={
                    "command": "true", "permissions": [{"capability": "network", "scope": "once", "reason": "check optional permission"}],
                })])
            return LLMResponse(text="done")

    model = Model()
    def factory(store, **kwargs):
        runner = in_process_runner(store, model, **kwargs)
        runner.command_executor = SandboxedCommandExecutor(HostFixtureBackend())
        runner.command_service.executor = runner.command_executor
        return runner

    service = ProductApplicationService(tmp_path / "sessions", supervisor=ExecutionSupervisor(runner_factory=factory))
    sid = service.create_session(repo, provider="fake")["sessionId"]
    service.run_session(sid, "edit safely")
    if request_network:
        assert _wait_until(lambda: service.supervisor.state(sid) == "awaiting_approval", timeout=5)
        pending = service.get_session(sid)["pendingApproval"]
        assert pending["kind"] == "command_permissions"
        assert pending["summary"]["permissions"][0]["capability"] == "network"
        assert state(repo)[:3] == original[:3]
        assert "reverse=False" in target.read_text()
        service.resolve_approval(sid, pending["approvalId"], False)
    assert _wait_until(lambda: service.supervisor.state(sid) == "idle", timeout=5)
    store = service._load(sid)
    assert store.workspace_state().value == "current_project"
    assert "reverse=False" in (store.workspace_context().active_root / "sort_utils.py").read_text()
    assert state(repo)[:3] == original[:3]
    assert "# user staged" in target.read_text() and "# user unstaged" in target.read_text()
    approvals = [e for e in store.read_events() if e.type == "approval_requested"]
    assert [e.payload["kind"] for e in approvals] == (["command_permissions"] if request_network else [])


def test_product_long_turn_runs_without_budget_wait_or_expansion(tmp_path):
    model = CountingModel(tool_steps=50)
    service, sid, factory, notifications = _real_service(tmp_path, model)
    service.run_session(sid, "original task")
    assert _wait_until(lambda: service.supervisor.state(sid) == "idle", timeout=15)
    assert model.calls == 51
    detail = service.get_session(sid)
    assert detail["turnBudget"]["closed"] and detail["turnBudget"]["limit"] is None
    assert not detail["availableActions"]["canIncreaseBudget"]
    assert detail["availableActions"]["canRun"]
    events = service._load(sid).read_events()
    assert not any(e.type in {"execution_slice_exhausted", "turn_budget_exhausted", "turn_budget_increased"} for e in events)
    assert sum(e.type == "user_message" for e in events) == 1
    reopened = ProductApplicationService(service.session_root, supervisor=ExecutionSupervisor(runner_factory=factory))
    assert reopened.get_session(sid)["turnBudget"] == detail["turnBudget"]
