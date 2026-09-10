from __future__ import annotations

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
        allowed = self.gate.request_workspace_upgrade("apply_workspace_edit", {
            "operations": [{"op": "create_file", "path": "new.txt", "content": "secretless"}],
        })
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
    assert runner_options[0]["runtime_config"].max_steps_per_turn == 12
    assert runner_options[0]["runtime_config"].max_model_steps_per_user_turn == 48
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


def test_approval_waits_without_blocking_service_and_resolves(tmp_path):
    service, session_id, _entered, _release, notifications, _options = _service(tmp_path, ApprovalRunner)
    service.run_session(session_id, "edit")
    assert _wait_until(lambda: service.get_session(session_id)["executionState"] == "awaiting_approval")

    detail = service.get_session(session_id)
    pending = detail["pendingApproval"]
    assert pending["kind"] == "workspace_upgrade"
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


def _real_service(tmp_path, model, *, limit=48, slice_steps=12):
    from codeagent.application.session_runtime import build_agent_runner
    notifications = []
    def factory(store, **kwargs):
        return build_agent_runner(store, model_factory=lambda _: model, **kwargs)
    supervisor = ExecutionSupervisor(lambda method, params: notifications.append((method, params)), runner_factory=factory)
    service = ProductApplicationService(tmp_path / "sessions", supervisor=supervisor)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    detail = service.create_session(workspace, provider="fake", max_steps_per_turn=slice_steps,
                                    max_model_steps_per_user_turn=limit)
    return service, detail["sessionId"], factory, notifications


def test_product_auto_runs_to_48_then_user_increase_survives_restart(tmp_path):
    model = CountingModel()
    service, sid, factory, notifications = _real_service(tmp_path, model)
    service.run_session(sid, "original task")
    assert _wait_until(lambda: service.supervisor.state(sid) == "idle", timeout=5)
    assert model.calls == 48
    detail = service.get_session(sid)
    budget = detail["turnBudget"]
    assert budget["waiting"] and budget["used"] == budget["limit"] == 48
    assert not detail["availableActions"]["canContinue"]
    assert detail["availableActions"]["canIncreaseBudget"]
    assert detail["availableActions"]["canStop"]
    assert not detail["availableActions"]["canRun"]
    assert not any(e.type == "turn_budget_increased" for e in service._load(sid).read_events())
    states = [params["executionState"] for method, params in notifications if method == "session/executionStateChanged"]
    assert states == ["running", "idle"]  # 内部切片不闪烁 idle，不要求人工 Continue。
    with pytest.raises(ProductServiceError):
        service.continue_session(sid)
    before_meta = service._load(sid).read_meta()
    reopened = ProductApplicationService(service.session_root, supervisor=ExecutionSupervisor(runner_factory=factory))
    assert reopened.get_session(sid)["turnBudget"] == budget
    reopened.increase_budget(sid, turn_id=budget["turnId"], expected_limit=48, new_limit=96)
    assert _wait_until(lambda: reopened.supervisor.state(sid) == "idle", timeout=5)
    final = reopened.get_session(sid)["turnBudget"]
    assert final == {**budget, "used": 51, "limit": 96, "closed": True, "waiting": False}
    store = reopened._load(sid)
    assert sum(e.type == "user_message" for e in store.read_events()) == 1
    assert store.read_meta()["runtime_options"] == before_meta["runtime_options"]
    assert store.workspace_state().value == "source_only"
    reopened.run_session(sid, "next task")
    assert _wait_until(lambda: reopened.supervisor.state(sid) == "idle")
    assert reopened.get_session(sid)["turnBudget"]["limit"] == 48


def test_budget_increase_is_idle_only_and_rejects_duplicate_or_stale_requests(tmp_path):
    model = CountingModel(tool_steps=100, block_at=3)
    service, sid, _factory, _notifications = _real_service(tmp_path, model, limit=2, slice_steps=1)
    service.run_session(sid, "task")
    assert _wait_until(lambda: service.supervisor.state(sid) == "idle")
    budget = service.get_session(sid)["turnBudget"]
    with pytest.raises(ProductServiceError):
        service.run_session(sid, "meaningless continue")
    for turn_id, expected, limit in [("old-turn", 2, 4), (budget["turnId"], 1, 4), (budget["turnId"], 2, True)]:
        with pytest.raises(ProductServiceError):
            service.increase_budget(sid, turn_id=turn_id, expected_limit=expected, new_limit=limit)
    service.increase_budget(sid, turn_id=budget["turnId"], expected_limit=2, new_limit=4)
    assert model.entered.wait(1)
    try:
        with pytest.raises(ProductServiceError, match="active execution"):
            service.increase_budget(sid, turn_id=budget["turnId"], expected_limit=2, new_limit=4)
    finally:
        model.release.set()
    assert _wait_until(lambda: service.supervisor.state(sid) == "idle")
    with pytest.raises(ProductServiceError, match="budget changed"):
        service.increase_budget(sid, turn_id=budget["turnId"], expected_limit=2, new_limit=6)
    assert sum(e.type == "turn_budget_increased" for e in service._load(sid).read_events()) == 1
    assert service.stop_session(sid)["accepted"]
    assert not service.stop_session(sid)["accepted"]
    with pytest.raises(ProductServiceError):
        service.increase_budget(sid, turn_id=budget["turnId"], expected_limit=4, new_limit=6)
    assert service.get_session(sid)["availableActions"]["canRun"]


def test_stop_during_auto_continuation_does_not_start_another_model_step(tmp_path):
    model = CountingModel(tool_steps=100, block_at=2)
    service, sid, _factory, _notifications = _real_service(tmp_path, model, limit=6, slice_steps=1)
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
