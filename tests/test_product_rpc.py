import io
import json

from codeagent.product.rpc import JsonRpcServer, serve
from codeagent.product.service import ProductApplicationService
from codeagent.session.store import SessionStore
from codeagent.product.execution import ExecutionSupervisor
from codeagent.runtime.runner import RunnerOutput


def _server(tmp_path):
    session_root = tmp_path / "sessions"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(workspace, session_root=session_root).create(title="RPC session")
    return JsonRpcServer(ProductApplicationService(session_root)), store, session_root


def test_rpc_initialize_advertises_phase_two_execution_boundaries(tmp_path):
    server, _, _ = _server(tmp_path)

    response = server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1.0"}})

    assert response["result"]["protocolVersion"] == "1.0"
    capabilities = response["result"]["capabilities"]
    assert capabilities["readOnlyProductShell"] is False
    assert capabilities["execution"] is True
    assert capabilities["liveNotifications"] is True
    assert capabilities["changesReview"] is True
    assert capabilities["changesResolution"] is True
    assert capabilities["interactiveApproval"] is True
    assert capabilities["protectedOperations"] is True
    assert capabilities["stop"] is True


def test_rpc_session_methods_and_after_seq(tmp_path):
    server, store, _ = _server(tmp_path)
    store.append_event("user_message", {"message": "hello"})

    listed = server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "session/list", "params": {}})
    detail = server.dispatch({"jsonrpc": "2.0", "id": 3, "method": "session/get", "params": {"sessionId": store.session_id}})
    events = server.dispatch({"jsonrpc": "2.0", "id": 4, "method": "session/events", "params": {
        "sessionId": store.session_id, "afterSeq": 1, "limit": 20,
    }})

    assert listed["result"]["sessions"][0]["sessionId"] == store.session_id
    assert detail["result"]["conversation"][0]["message"] == "hello"
    assert [event["seq"] for event in events["result"]["events"]] == [2]


def test_rpc_rejects_invalid_execution_and_event_params(tmp_path):
    server, store, _ = _server(tmp_path)

    mutation = server.dispatch({"jsonrpc": "2.0", "id": 5, "method": "session/run", "params": {}})
    invalid = server.dispatch({"jsonrpc": "2.0", "id": 6, "method": "session/events", "params": {
        "sessionId": store.session_id, "afterSeq": -1,
    }})

    assert "error" in mutation
    assert mutation["error"]["code"] == -32602
    assert "error" in invalid
    assert "non-negative" in invalid["error"]["message"]


def test_rpc_creates_session_and_starts_execution_without_waiting(tmp_path):
    server, _, _ = _server(tmp_path)
    workspace = tmp_path / "other-workspace"
    workspace.mkdir()

    created = server.dispatch({"jsonrpc": "2.0", "id": 7, "method": "session/create", "params": {
        "workspace": str(workspace), "provider": "fake",
    }})
    started = server.dispatch({"jsonrpc": "2.0", "id": 8, "method": "session/run", "params": {
        "sessionId": created["result"]["sessionId"], "message": "Read README",
    }})

    assert started["result"]["executionState"] == "running"
    assert started["result"]["approvalMode"] == "interactive"


def test_rpc_freezes_model_and_runtime_options_on_session_creation(tmp_path):
    server, _, _ = _server(tmp_path)
    workspace = tmp_path / "configured-workspace"
    workspace.mkdir()

    created = server.dispatch({"jsonrpc": "2.0", "id": 9, "method": "session/create", "params": {
        "workspace": str(workspace), "provider": "glm", "model": "glm-custom",
        "temperature": 0.3, "maxTokens": 6000, "maxStepsPerTurn": 8,
        "maxModelStepsPerUserTurn": 30,
    }})
    meta = SessionStore.find_session(created["result"]["sessionId"], session_root=tmp_path / "sessions")

    assert meta["provider"] == "glm"
    assert meta["model"] == "glm-custom"
    assert meta["model_options"] == {
        "temperature": 0.3, "max_tokens": 6000,
        "reasoning_enabled": False, "reasoning_effort": None,
    }
    assert meta["runtime_options"] == {"max_steps_per_turn": 8, "max_model_steps_per_user_turn": 30}


def test_rpc_serializes_reasoning_configuration_and_rejects_unsupported_effort(tmp_path):
    server, _, _ = _server(tmp_path)
    workspace = tmp_path / "reasoning-workspace"
    workspace.mkdir()

    created = server.dispatch({"jsonrpc": "2.0", "id": 10, "method": "session/create", "params": {
        "workspace": str(workspace), "provider": "glm", "model": "glm-5.2",
        "reasoningEnabled": True, "reasoningEffort": "max",
    }})
    assert created["result"]["reasoningEnabled"] is True
    assert created["result"]["reasoningEffort"] == "max"
    meta = SessionStore.find_session(created["result"]["sessionId"], session_root=tmp_path / "sessions")
    assert meta["model_options"]["reasoning_enabled"] is True
    assert meta["model_options"]["reasoning_effort"] == "max"

    invalid = server.dispatch({"jsonrpc": "2.0", "id": 11, "method": "session/create", "params": {
        "workspace": str(workspace), "provider": "glm", "model": "glm-5.2",
        "reasoningEnabled": True, "reasoningEffort": "low",
    }})
    assert invalid["error"]["code"] == -32004
    assert "high, max" in invalid["error"]["message"]


def test_stdio_server_emits_one_json_response_per_request(tmp_path):
    _, store, session_root = _server(tmp_path)
    requests = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1.0"}}),
        "not-json",
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "session/get", "params": {"sessionId": store.session_id}}),
    ]) + "\n"
    output = io.StringIO()

    serve(session_root=session_root, input_stream=io.StringIO(requests), output_stream=output)

    lines = output.getvalue().splitlines()
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["id"] == 1
    assert parsed[1]["error"]["code"] == -32700
    assert parsed[2]["result"]["sessionId"] == store.session_id


def test_rpc_budget_increase_requires_explicit_matching_turn_and_integer_limit(tmp_path):
    import threading
    server, store, session_root = _server(tmp_path)
    store.append_event("user_message", {"message": "task", "max_model_steps_per_user_turn": 1})
    store.append_event("assistant_tool_calls", {"tool_calls": []})
    store.append_event("turn_budget_exhausted", {"max_model_steps_per_user_turn": 1, "steps_used_in_turn": 1})
    resumed = threading.Event()
    class Runner:
        def continue_turn(self):
            store.append_event("assistant_message", {"message": "done"})
            resumed.set()
            return RunnerOutput(final_text="done")
    server = JsonRpcServer(ProductApplicationService(session_root, supervisor=ExecutionSupervisor(runner_factory=lambda *a, **k: Runner())))
    params = {"sessionId": store.session_id, "turnId": store.read_meta()["current_turn_id"], "expectedLimit": 1, "newLimit": 4}
    for invalid in [True, "4", 4.5, None, 1, 1001]:
        result = server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "session/increaseBudget", "params": {**params, "newLimit": invalid}})
        assert "error" in result
    assert not resumed.is_set()
    result = server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "session/increaseBudget", "params": params})
    assert result["result"]["executionState"] == "running"
    assert resumed.wait(1)
    assert sum(event.type == "user_message" for event in store.read_events()) == 1


def test_rpc_accepts_user_specified_additional_steps(tmp_path):
    import threading
    _, store, session_root = _server(tmp_path)
    store.append_event("user_message", {"message": "task", "max_model_steps_per_user_turn": 1})
    store.append_event("assistant_tool_calls", {"tool_calls": []})
    store.append_event("turn_budget_exhausted", {"max_model_steps_per_user_turn": 1, "steps_used_in_turn": 1})
    resumed = threading.Event()

    class Runner:
        def continue_turn(self):
            store.append_event("assistant_message", {"message": "done"})
            resumed.set()
            return RunnerOutput(final_text="done")

    server = JsonRpcServer(ProductApplicationService(
        session_root, supervisor=ExecutionSupervisor(runner_factory=lambda *a, **k: Runner()),
    ))
    response = server.dispatch({"jsonrpc": "2.0", "id": 3, "method": "session/increaseBudget", "params": {
        "sessionId": store.session_id, "turnId": store.read_meta()["current_turn_id"],
        "expectedLimit": 1, "additionalSteps": 3,
    }})
    assert response["result"]["executionState"] == "running"
    assert resumed.wait(1)
    event = next(e for e in reversed(store.read_events()) if e.type == "turn_budget_increased")
    assert event.payload["new_limit"] == 4 and event.payload["mode"] == "additional_steps"
