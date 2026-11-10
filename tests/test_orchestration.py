import hashlib
import json
import sqlite3
import subprocess
from dataclasses import replace

import pytest
from typer.testing import CliRunner

from codeagent.application.session_runtime import build_agent_runner
from codeagent.cli import app
from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.model_gateway.base import BaseModelClient, LLMResponse, LLMToolCall
from codeagent.model_gateway.fake import FakeLLM
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.delegation import DelegationService
from codeagent.runtime.request_executor import RequestExecutor
from codeagent.runtime.policy import DefaultPolicy, PolicyDecision, PolicyResult
from codeagent.session.store import SessionStore
from codeagent.orchestration.agents import AgentSessions



class ConversationModel(FakeLLM):
    """免费可控模型：从本次输入和真实工具 observation 决定后续动作。"""
    def complete(self, request):
        if request.purpose == "context_condenser":
            return super().complete(request)
        users = [m for m in request.messages if m["role"] == "user"]
        if request.purpose == "readonly_child":
            # 最新 user 消息是本次 run 的 anchor；绝不能收到父私密历史。
            assert "PARENT_SECRET" not in json.dumps(request.messages)
            anchor = json.loads(users[-1]["content"])
            after = request.messages[request.messages.index(users[-1]) + 1:]
            observations = [m for m in after if m["role"] == "tool"]
            if not observations:
                name, args = ("run_command", {"command": "touch ESCAPED"}) if anchor["goal"] == "escape" else ("read_file", {"path": "README.md", "start_line": 1, "end_line": 3})
                return LLMResponse(tool_calls=[LLMToolCall(call_id="read-1", name=name, arguments=args)])
            prior = [m for m in request.messages[:request.messages.index(users[-1])] if m["role"] == "tool"]
            metadata = json.loads(request.messages[0]["content"].rsplit("\n", 1)[1])
            return LLMResponse(text=json.dumps({"observations": observations, "previous_tool_count": len(prior),
                "referenced_previous_evidence": []}, ensure_ascii=False), finish_reason="stop")
        observations = [m for m in request.messages if m["role"] == "tool"]
        tool_names = {t.name for t in request.tools}
        if "task" in tool_names and "spawn_agent" not in tool_names:
            if not observations:
                return LLMResponse(tool_calls=[LLMToolCall(call_id="task-first", name="task", arguments={
                    "subagent_type": "review", "prompt": "read"})])
            if len(observations) == 1:
                previous = json.loads(json.loads(observations[0]["content"])["content"])
                return LLMResponse(tool_calls=[LLMToolCall(call_id="task-follow", name="task", arguments={
                    "resume": previous["task_id"], "prompt": "核实先前证据"})])
            if len(observations) == 2:
                return LLMResponse(tool_calls=[LLMToolCall(call_id="parent-read", name="list_dir", arguments={})])
            return LLMResponse(text="synchronous multi round completed", finish_reason="stop")
        if "spawn_agent" not in tool_names:
            return super().complete(request)
        if not observations:
            return LLMResponse(tool_calls=[LLMToolCall(call_id="spawn", name="spawn_agent", arguments=spawn_args())])
        if len(observations) == 1:
            report = json.loads(json.loads(observations[0]["content"])["content"])
            return LLMResponse(tool_calls=[LLMToolCall(call_id="follow", name="send_agent", arguments=send_args(report))])
        if len(observations) == 2:
            return LLMResponse(tool_calls=[LLMToolCall(call_id="parent-read", name="list_dir", arguments={})])
        return LLMResponse(text="graph multi round completed", finish_reason="stop")


def conversation_factory(config):
    return ConversationModel()


@pytest.fixture
def repo_store(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "README.md").write_text("# R1\nENTRY = main.py\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=C2A", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline"], cwd=root, check=True)
    return root, SessionStore(root, session_root=tmp_path / "sessions").create()


def spawn_args(key="first", goal="read"):
    return {"spec": "review", "goal": goal, "done_when": "读取证据", "context": "explicit only", "request_key": key}


def send_args(report, key="second", mode="pinned"):
    return {"agent_id": report["agent_id"], "message": "核实先前证据", "done_when": "重新读取", "request_key": key}


def service_for(store, factory=conversation_factory, policy=None, config=None):
    if not any(e.type == "user_message" for e in store.read_events()):
        store.append_event("user_message", {"message": "PARENT_SECRET", "max_model_steps_per_user_turn": 48})
    budget = RequestExecutor(store, config or RuntimeConfig(delegation_enabled=True), ModelConfig(), factory, lambda: False)
    legacy = DelegationService(store, budget, store.workspace_context, parent_policy=policy)
    return AgentSessions(store, budget, legacy)


def report(result):
    assert result.ok, result
    data = json.loads(result.content)
    assert data["status"] == "completed", data
    return data


def events(store, kind):
    return [e for e in store.read_events() if e.type == kind]


class TruncationSDK:
    """无网络 SDK 替身；检查真实 GLM adapter 最终参数，而非仅检查配置对象。"""
    def __init__(self):
        from types import SimpleNamespace
        self.chat = SimpleNamespace(completions=self)

    def with_options(self, **kwargs):
        assert kwargs == {"max_retries": 0}
        return self

    def create(self, **kwargs):
        from types import SimpleNamespace as Obj
        messages = kwargs["messages"]
        recovering = any(m["role"] == "system" and "上一次响应达到输出长度上限" in m.get("content", "") for m in messages)
        observed = any(m["role"] == "tool" for m in messages)
        assert kwargs["model"] == "glm-5.3"
        assert kwargs["reasoning_effort"] == ("low" if recovering else "high")
        assert kwargs["temperature"] == 1.0 and kwargs["max_tokens"] == 8192
        assert kwargs["extra_body"]["thinking"] == {"type": "enabled", "clear_thinking": True}
        if "always truncate" in json.dumps(messages) or not recovering and not observed:
            message = Obj(content="partial", reasoning_content="partial reasoning", tool_calls=[
                Obj(id="must-not-execute", function=Obj(name="read_file", arguments='{"path":"README.md"}'))])
            if "malformed arguments" in json.dumps(messages):
                message.tool_calls.append(Obj(id="broken", function=Obj(name="read_file", arguments='{"path":')))
            reason, count = "length", 8192
        elif recovering:
            message = Obj(content="", tool_calls=[Obj(id="recovery-read", function=Obj(name="read_file", arguments='{"path":"README.md"}'))])
            reason, count = "tool_calls", 20
        else:
            message = Obj(content="recovered", tool_calls=[])
            reason, count = "stop", 10
        return Obj(id="offline-" + reason, choices=[Obj(message=message, finish_reason=reason)],
                   usage=Obj(prompt_tokens=50, completion_tokens=count, total_tokens=50 + count))


def truncation_sdk_factory(config):
    from codeagent.model_gateway.glm_client import GLMClient
    return GLMClient(model=config.model, client=TruncationSDK(), reasoning_enabled=config.reasoning_enabled,
                     reasoning_effort=config.reasoning_effort)


@pytest.mark.parametrize("repeat", [False, True])
@pytest.mark.parametrize("malformed", [False, True])
def test_graph_reuses_glm_truncation_recovery_in_final_sdk(repo_store, repeat, malformed):
    from codeagent.runtime.runner import MAX_CONSECUTIVE_OUTPUT_TRUNCATIONS
    _, store = repo_store
    runner = build_agent_runner(store, model_config=ModelConfig(provider="glm", model="glm-5.3", temperature=1.0,
        max_tokens=8192, reasoning_enabled=True, reasoning_effort="high"), approval_gate=AutoApprovalGate(False),
        runtime_config=RuntimeConfig(cost_per_token_microunits=1), model_factory=truncation_sdk_factory)
    result = runner.run_turn(("always truncate" if repeat else "read README after recovering")
                             + (" malformed arguments" if malformed else ""))
    usages = events(store, "model_usage")
    assert [e.payload["reasoning_effort"] for e in usages] == (
        ["high"] + ["low"] * (MAX_CONSECUTIVE_OUTPUT_TRUNCATIONS - 1) if repeat else ["high", "low", "high"])
    assert len(events(store, "model_request_reserved")) == len(usages) == len(events(store, "model_request_settled"))
    assert result.status == ("output_truncated" if repeat else "completed")
    assert [e.payload["call_id"] for e in events(store, "tool_result")] == ([] if repeat else ["recovery-read"])
    if not repeat:
        assert result.final_text == "recovered"
    if malformed:
        assert events(store, "model_request_settled")[0].payload["outcome"] == "protocol_error"
        assert events(store, "model_response")[0].payload["response"]["protocol_error"]


class ProtocolErrorSDK:
    def __init__(self):
        from types import SimpleNamespace as Obj
        self.chat = Obj(completions=self)

    def with_options(self, **kwargs):
        assert kwargs == {"max_retries": 0}
        return self

    def create(self, **kwargs):
        from types import SimpleNamespace as Obj
        from pathlib import Path
        import os
        path = Path(os.environ["C4_TEST_PROTOCOL_CAPTURE"])
        previous = path.read_text().splitlines() if path.exists() else []
        with path.open("a") as stream:
            stream.write(json.dumps(kwargs, ensure_ascii=False) + "\n")
        attempt = len(previous) + 1
        mode = os.environ["C4_TEST_PROTOCOL_MODE"]
        if mode in {"repeat", "changing"} or attempt == 1:
            args = "[]" if mode == "nonobject" else '{"path":' + (str(attempt) + ',' if mode == "changing" else '')
            message = Obj(content="not a final answer", tool_calls=[
                Obj(id="prefix-must-not-execute", function=Obj(name="read_file", arguments='{"path":"README.md"}')),
                Obj(id="bad-arguments", function=Obj(name="read_file", arguments=args)),
            ])
            reason = "tool_calls"
        elif not any(m["role"] == "tool" for m in kwargs["messages"]):
            assert any(m["role"] == "system" and "本次响应中的工具均未执行" in m.get("content", "") for m in kwargs["messages"])
            message = Obj(content="", tool_calls=[Obj(id="valid-read", function=Obj(name="read_file", arguments='{"path":"README.md"}'))])
            reason = "tool_calls"
        else:
            assert not any(m["role"] == "system" and "本次响应中的工具均未执行" in m.get("content", "") for m in kwargs["messages"])
            message, reason = Obj(content="recovered after parse error", tool_calls=[]), "stop"
        return Obj(id="offline-protocol-" + str(attempt), choices=[Obj(message=message, finish_reason=reason)],
                   usage=Obj(prompt_tokens=50, completion_tokens=20, total_tokens=70))


def protocol_error_sdk_factory(config):
    from codeagent.model_gateway.glm_client import GLMClient
    return GLMClient(model=config.model, client=ProtocolErrorSDK())


@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize("mode,expected", [("recover", 3), ("nonobject", 3), ("repeat", 2), ("changing", 4)])
def test_graph_known_protocol_errors_repair_without_partial_execution_or_double_charge(repo_store, tmp_path, monkeypatch, child, mode, expected):
    capture = tmp_path / "protocol-sdk.jsonl"
    monkeypatch.setenv("C4_TEST_PROTOCOL_CAPTURE", str(capture))
    monkeypatch.setenv("C4_TEST_PROTOCOL_MODE", mode)
    _, store = repo_store
    success = mode in {"recover", "nonobject"}
    if child:
        service = service_for(store, factory=protocol_error_sdk_factory, config=RuntimeConfig(
            ))
        result = json.loads(service.spawn(spawn_args()).content)
        assert result["status"] == ("completed" if success else "failed")
        histories = events(store, "agent_history")
        executed = [e.payload["data"]["call_id"] for e in histories if e.payload["kind"] == "tool_result"]
        errors = [e.payload["data"] for e in histories if e.payload["kind"] == "model_protocol_error"]
        # Rebuild from durable graph responses: no new request or duplicate error projection.
        from codeagent.orchestration.agents import ChildAdapter
        from codeagent.orchestration.loop import AgentLoop
        from codeagent.orchestration.lease import session_lease
        snapshot = tmp_path / "protocol-replay"
        snapshot.mkdir()
        payload = events(store, "agent_command_intent")[0].payload
        before = [e.model_dump() for e in histories]
        with session_lease(store) as epoch:
            replayed = AgentLoop(ChildAdapter(service, service.executor, payload, store.workspace_root, epoch)).invoke()
            assert replayed["status"] == result["status"]
        assert [e.model_dump() for e in events(store, "agent_history")] == before
    else:
        runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
            runtime_config=RuntimeConfig(delegation_enabled=True),
            model_factory=protocol_error_sdk_factory)
        result = runner.run_turn("read README; correct malformed responses")
        assert result.status == ("completed" if success else "failed")
        executed = [e.payload["call_id"] for e in events(store, "tool_result")]
        errors = [e.payload for e in events(store, "model_protocol_error")]
    assert executed == (["valid-read"] if success else [])
    assert [e["attempt"] for e in errors] == list(range(1, (1 if success else expected) + 1))
    assert len(capture.read_text().splitlines()) == len(events(store, "model_request_reserved")) == expected
    settlements = events(store, "model_request_settled")
    assert len(settlements) == len(events(store, "model_response")) == expected
    assert sum(e.payload["usage"]["total_tokens"] for e in settlements) == expected * 70
    assert settlements[0].payload["outcome"] == "protocol_error"




def test_durable_invalid_response_reconciles_before_history_without_another_request(repo_store):
    from types import SimpleNamespace
    from codeagent.orchestration.lease import reconcile_requests
    from codeagent.runtime.execution_service import ModelToolExecution
    from codeagent.model_gateway.base import ModelRequest, TokenUsage
    _, store = repo_store
    store.append_event("user_message", {"message": "continue"})
    store.append_event("model_request_reserved", {"request_id": "known-bad", "agent_id": "main",
        "operation_key": "op", "fee_limit_enabled": False})
    response = LLMResponse(provider_request_id="provider-known-bad", finish_reason="tool_calls",
        usage=TokenUsage(input_tokens=100, output_tokens=20, total_tokens=120),
        protocol_error={"tool": "read_file", "message": "incomplete JSON", "fingerprint": "bad"})
    store.append_event("model_response", {"request_id": "known-bad", "operation_key": "op",
                                                "agent_id": "main", "response": response.model_dump()})
    reconcile_requests(store)
    reconcile_requests(store)
    settled, = events(store, "model_request_settled")
    assert settled.payload["outcome"] == "protocol_error"
    assert settled.payload["provider_request_id"] == "provider-known-bad"
    assert settled.payload["usage"]["total_tokens"] == 120
    assert settled.payload["fee_state"] == "unpriced"
    execution = ModelToolExecution(store, SimpleNamespace(), lambda *a: pytest.fail("no new event needed"))
    assert execution.request("op", ModelRequest(messages=[]), "main") == response


class ChildTruncationSDK:
    def __init__(self):
        from types import SimpleNamespace as Obj
        self.chat = Obj(completions=self)

    def with_options(self, **kwargs):
        assert kwargs == {"max_retries": 0}
        return self

    def create(self, **kwargs):
        from types import SimpleNamespace as Obj
        import os
        from pathlib import Path
        capture_path = Path(os.environ["C4_TEST_CHILD_CAPTURE"])
        with capture_path.open("a") as stream:
            stream.write(json.dumps(kwargs, ensure_ascii=False) + "\n")
        messages = kwargs["messages"]
        recovering = any(m["role"] == "system" and "上一次响应达到输出长度上限" in m.get("content", "") for m in messages)
        observed = any(m["role"] == "tool" for m in messages)
        assert kwargs["reasoning_effort"] == ("low" if recovering else "high")
        assert kwargs["max_tokens"] == 65536 and kwargs["model"] == "glm-5.3"
        assert kwargs["temperature"] == 1.0
        assert ("partial report" in json.dumps(messages)) == recovering
        if "always truncate" in json.dumps(messages) or not recovering and not observed:
            # 即使调用字段看似完整，也不能执行截断批次。
            message = Obj(content="partial report", reasoning_content="partial reasoning", tool_calls=[
                Obj(id="must-not-execute", function=Obj(name="read_file", arguments='{"path":"README.md"}'))])
            if "malformed arguments" in json.dumps(messages):
                message.tool_calls.append(Obj(id="broken", function=Obj(name="read_file", arguments='{"path":')))
            reason, count = "length", 65536
        elif recovering:
            message = Obj(content="", tool_calls=[Obj(id="recovery-read", function=Obj(name="read_file", arguments='{"path":"README.md"}'))])
            reason, count = "tool_calls", 20
        else:
            assert "# R1" in json.dumps(messages)
            message = Obj(content="README.md:1 confirms R1; no write or validation performed.", tool_calls=[])
            reason, count = "stop", 30
        return Obj(id="offline-child-" + reason, choices=[Obj(message=message, finish_reason=reason)],
                   usage=Obj(prompt_tokens=50, completion_tokens=count, total_tokens=50 + count))


def child_truncation_sdk_factory(config):
    from codeagent.model_gateway.glm_client import GLMClient
    return GLMClient(model=config.model, client=ChildTruncationSDK(), reasoning_enabled=config.reasoning_enabled,
                     reasoning_effort=config.reasoning_effort)


@pytest.mark.parametrize("repeat,child_steps", [(False, 6), (True, 6)])
@pytest.mark.parametrize("malformed", [False, True])
def test_child_truncation_recovers_in_same_run_with_bounded_sdk_requests(repo_store, repeat, child_steps, malformed, tmp_path, monkeypatch):
    from codeagent.model_gateway.output_recovery import MAX_CONSECUTIVE_OUTPUT_TRUNCATIONS
    capture_path = tmp_path / "child-sdk.jsonl"
    monkeypatch.setenv("C4_TEST_CHILD_CAPTURE", str(capture_path))
    _, store = repo_store
    service = service_for(store, factory=child_truncation_sdk_factory, config=RuntimeConfig(
        ))
    service.executor.model_config = ModelConfig(provider="glm", model="glm-5.3", temperature=1.0,
        reasoning_enabled=True, reasoning_effort="high")
    service.routes["parent"] = service.executor.model_config
    result = json.loads(service.spawn(spawn_args(goal=("always truncate" if repeat else "read")
                                                + (" malformed arguments" if malformed else ""))).content)
    captures = [json.loads(line) for line in capture_path.read_text().splitlines()]
    expected = 1 if child_steps == 1 else MAX_CONSECUTIVE_OUTPUT_TRUNCATIONS if repeat else 3
    assert len(captures) == len(events(store, "model_request_reserved")) == len(events(store, "model_request_settled")) == expected
    assert [c["reasoning_effort"] for c in captures] == (["high"] if child_steps == 1 else
        ["high", "low", "low"] if repeat else ["high", "low", "high"])
    history = events(store, "agent_history")
    assert {e.payload["run_id"] for e in history} == {result["run_id"]}
    assert [e.payload["data"]["call_id"] for e in history if e.payload["kind"] == "tool_result"] == (
        ["recovery-read"] if not repeat and child_steps > 1 else [])
    truncations = [e for e in history if e.payload["kind"] == "model_output_truncated"]
    assert [e.payload["data"]["consecutive_count"] for e in truncations] == list(range(1, len(truncations) + 1))
    assert result["status"] == ("budget_exhausted" if child_steps == 1 else "failed" if repeat else "completed")
    assert bool(result["findings"]) == (not repeat and child_steps > 1)
    # 重建图会从 iteration 0 回放可靠响应；不能累加同一次截断或再次收费。
    from codeagent.orchestration.agents import ChildAdapter
    from codeagent.orchestration.loop import AgentLoop
    from codeagent.orchestration.lease import session_lease
    root = tmp_path / "replay-snapshot"
    root.mkdir()
    payload = events(store, "agent_command_intent")[0].payload
    before_history = [e.model_dump() for e in history]
    with session_lease(store) as epoch:
        adapter = ChildAdapter(service, service.executor, payload, store.workspace_root, epoch)
        replayed = AgentLoop(adapter).invoke()
        assert replayed == events(store, "agent_run_finished")[0].payload
        assert all(result[k] == value for k, value in replayed.items())
    assert [e.model_dump() for e in events(store, "agent_history")] == before_history
    assert len(events(store, "model_request_reserved")) == expected
    assert len(capture_path.read_text().splitlines()) == expected


def test_real_graph_main_child_nodes_and_checkpoints(repo_store):
    _, store = repo_store
    runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
        runtime_config=RuntimeConfig(delegation_enabled=True), model_factory=conversation_factory)
    out = runner.run_turn("PARENT_SECRET 委派多轮")
    assert out.status == "completed", out
    assert [s["tool"] for s in out.steps] == ["task", "task", "list_dir"]
    runs = events(store, "agent_run_finished")
    assert len(runs) == 2 and runs[0].payload["agent_id"] == runs[1].payload["agent_id"]
    assert json.loads(runs[1].payload["findings"])["previous_tool_count"] == 1
    assert all("verified" not in r.payload for r in runs)
    assert all("unresolved" not in r.payload for r in runs)
    assert not events(store, "graph_node")
    assert not (store.session_dir / "graph.sqlite").exists()


def test_same_agent_second_run_after_new_service_and_command_dedup(repo_store):
    _, store = repo_store
    service = service_for(store)
    first = report(service.spawn(spawn_args()))
    count = len(events(store, "model_request_reserved"))
    assert report(service.spawn(spawn_args()))["run_id"] == first["run_id"]
    assert len(events(store, "model_request_reserved")) == count
    assert service.spawn(spawn_args(goal="changed")).error_code == "request_key_conflict"
    second = report(service_for(store).send(send_args(first)))
    assert second["agent_id"] == first["agent_id"] and second["run_id"] != first["run_id"]
    assert json.loads(second["findings"])["previous_tool_count"] == 1
    assert len(second["evidence_refs"]) == 1
    assert second["evidence_refs"][0]["event_ref"].startswith("seq:")
    assert len(events(store, "agent_run_finished")) == 2




def test_parent_policy_and_spec_no_escalation(repo_store):
    root, store = repo_store
    class DenyRead(DefaultPolicy):
        def evaluate(self, tool):
            if tool.name == "read_file":
                return PolicyResult(PolicyDecision.ASK, "parent approval required")
            return super().evaluate(tool)
    service = service_for(store, policy=DenyRead())
    first = report(service.spawn(spawn_args()))
    assert "policy_denied" in first["findings"] and all(ref["event_ref"].startswith("seq:") for ref in first["evidence_refs"])
    assert service.spawn({**spawn_args("unsafe"), "permissions": ["WRITE"]}).error_code == "invalid_arguments"
    assert service.spawn({**spawn_args("endpoint"), "gateway": "https://attacker.invalid"}).error_code == "unknown_gateway_alias"
    escape = report(service.spawn(spawn_args("escape", "escape")))
    assert "policy_denied" in escape["findings"]
    assert not (root / "ESCAPED").exists()


def test_cli_graph_roundtrip(repo_store, tmp_path):
    root, _ = repo_store
    out = CliRunner().invoke(app, ["ask", str(root), "test", "--delegation", "--session-root", str(tmp_path / "cli")])
    assert out.exit_code == 0, out.output
    assert "README" in out.output


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize("boundary", ["agent_command_intent", "model_response", "tool_execution_result", "agent_run_finished"])
def test_event_ahead_checkpoint_recovers_without_duplicate_requests(repo_store, monkeypatch, boundary):
    _, store = repo_store
    service = service_for(store)
    original = store.append_derived_events
    crashed = False
    def append(items, **kwargs):
        nonlocal crashed
        result = original(items, **kwargs)
        if not crashed and any(kind == boundary for kind, _ in items):
            crashed = True
            raise SimulatedCrash(boundary)
        return result
    with monkeypatch.context() as patch:
        patch.setattr(store, "append_derived_events", append)
        with pytest.raises(SimulatedCrash):
            service.spawn(spawn_args())
    assert crashed
    before = len(events(store, "model_request_reserved"))
    recovered = report(service_for(store).spawn(spawn_args()))
    assert len(events(store, "agent_command_intent")) == 1
    assert len(events(store, "agent_run_finished")) == 1
    assert len(events(store, "model_request_reserved")) == 2
    if boundary == "agent_run_finished":
        assert before == 2
    assert recovered["evidence_refs"]


def test_unknown_reserved_request_is_retained_not_reissued(repo_store, monkeypatch):
    _, store = repo_store
    config = RuntimeConfig(cost_per_token_microunits=1)
    service = service_for(store, config=config)
    original = service.executor.reserve
    def reserve(*args, **kwargs):
        original(*args, **kwargs)
        raise SimulatedCrash("reserved before response")
    with monkeypatch.context() as patch:
        patch.setattr(service.executor, "reserve", reserve)
        with pytest.raises(SimulatedCrash):
            service.spawn(spawn_args())
    reserved = events(store, "model_request_reserved")[0].payload
    restarted = service_for(store, config=config)
    result = json.loads(restarted.spawn(spawn_args()).content)
    assert result["status"] == "interrupted"
    assert "UNKNOWN" in str(result["error"])
    assert len(events(store, "model_request_reserved")) == 1
    settled = events(store, "model_request_settled")
    assert len(settled) == 1 and settled[0].payload["usage"] is None
    assert settled[0].payload["fee_status"] == "usage_unavailable"
    assert reserved["fee_reserved_microunits"] == 0
    assert result["usage_refs"] == [reserved["request_id"]]
    # 失败历史仍可续问；显式新 run 使用新请求身份。
    second = report(restarted.send(send_args(result)))
    assert second["run_id"] != result["run_id"]
    assert len(events(store, "model_request_reserved")) == 3


def test_busy_followup_conflict_and_foreign_agent_rejected(repo_store, monkeypatch, tmp_path):
    root, store = repo_store
    service = service_for(store)
    original = store.append_derived_events
    def crash(items, **kwargs):
        result = original(items, **kwargs)
        if any(kind == "agent_command_intent" for kind, _ in items):
            raise SimulatedCrash()
        return result
    with monkeypatch.context() as patch:
        patch.setattr(store, "append_derived_events", crash)
        with pytest.raises(SimulatedCrash):
            service.spawn(spawn_args())
    run = events(store, "agent_command_intent")[0].payload["run"]
    assert service.send(send_args(run)).error_code == "agent_busy"
    first = report(service.spawn(spawn_args()))
    assert json.loads(service.send(send_args(first)).content)["status"] == "completed"
    other = SessionStore(root, session_root=tmp_path / "other").create()
    assert service_for(other).send(send_args(first)).error_code == "unknown_agent"
    assert len(events(other, "agent_history")) == 0


def test_two_agents_have_independent_history(repo_store):
    _, store = repo_store
    service = service_for(store)
    first = report(service.spawn(spawn_args()))
    other = report(service.spawn(spawn_args("other")))
    assert first["agent_id"] != other["agent_id"]
    assert json.loads(other["findings"])["previous_tool_count"] == 0
    assert len(events(store, "agent_history")) == 8
    from codeagent.tools.observations import build_observation_tool
    other_source = other['evidence_refs'][0]['event_ref']
    assert not build_observation_tool(store, agent_id=first['agent_id']).handler({'source_ref': other_source}).ok


def test_event_frontier_lag_and_partial_tail_are_reconciled(repo_store):
    _, store = repo_store
    meta = store.read_meta()
    user = store.append_event("user_message", {"message": "recover me"})
    store._write_state(meta)
    with store.events_path.open("ab") as handle:
        handle.write(b'{"seq":')
    from codeagent.orchestration.lease import session_lease
    with session_lease(store):
        pass
    loaded = store.read_events()
    assert len({e.seq for e in loaded}) == len(loaded)
    assert store.read_meta()["current_turn_id"] == user.turn_id
    assert list(store.session_dir.glob("events-tail-*.bin"))




class EditModel(ConversationModel):
    def complete(self, request):
        if request.purpose == "readonly_child":
            return super().complete(request)
        if not any(m["role"] == "tool" for m in request.messages):
            return LLMResponse(tool_calls=[LLMToolCall(call_id="edit", name="apply_workspace_edit", arguments={
                "operations": [{"op": "create_file", "path": "created.py", "content": "VALUE = 1\n"}]})])
        return LLMResponse(text="edited", finish_reason="stop")


def edit_factory(config):
    return EditModel()


def test_unknown_main_write_is_never_replayed(repo_store, monkeypatch):
    _, store = repo_store
    runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
        runtime_config=RuntimeConfig(delegation_enabled=True), model_factory=edit_factory)
    executed = []
    def crash(call):
        executed.append(call.name)
        raise SimulatedCrash("unknown write dispatch")
    with monkeypatch.context() as patch:
        patch.setattr(runner, "_handle_tool_call", crash)
        with pytest.raises(SimulatedCrash):
            runner.run_turn("create file")
    before = len(events(store, "model_request_reserved"))
    restarted = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
        runtime_config=RuntimeConfig(delegation_enabled=True), model_factory=edit_factory)
    output = restarted.continue_turn()
    assert output.status == "failed"
    assert executed == ["apply_workspace_edit"]
    assert store.read_meta()["workspace_state"] == "recovery_required"
    assert len(events(store, "model_request_reserved")) == before


def test_scoped_condensation_uses_shared_budget_and_keeps_private_audit(repo_store, monkeypatch):
    _, store = repo_store
    from codeagent.context.builder import ContextManager
    from codeagent.context.models import ModelCapabilities
    original = ContextManager.build
    def shared_small_context(self, **kwargs):
        monkeypatch.setattr("codeagent.context.builder.HISTORY_MAX_EVENTS", 3)
        return original(self, **kwargs)
    monkeypatch.setattr(ContextManager, "build", shared_small_context)
    service = service_for(store)
    first = report(service.spawn(spawn_args()))
    second = report(service.send(send_args(first)))
    assert second["agent_id"] == first["agent_id"]
    assert events(store, "agent_context_condensed")
    assert len(events(store, "agent_history")) == 8
    requests = events(store, "model_request_reserved")
    assert len(requests) == 5
    assert sum(e.payload["purpose"] == "context_condenser" for e in requests) == 1
    assert all(e.turn_id == requests[0].turn_id for e in requests)
    from codeagent.runtime.turn_state import current_turn_state
    assert current_turn_state(store.read_events()).used == 5


def test_cross_user_turn_followup_uses_new_budget_root(repo_store):
    _, store = repo_store
    service = service_for(store)
    first = report(service.spawn(spawn_args()))
    old_root = first["root_turn_id"]
    store.append_event("user_message", {"message": "PARENT_SECRET next turn"})
    new_service = service_for(store)
    second = report(new_service.send(send_args(first)))
    assert second["agent_id"] == first["agent_id"]
    assert second["root_turn_id"] != old_root
    assert json.loads(second["findings"])["previous_tool_count"] == 1
    reservations = events(store, "model_request_reserved")
    assert len({e.turn_id for e in reservations}) == 2


def test_explicit_gateway_only_and_opt_in_zero_paid_cap(repo_store):
    _, store = repo_store
    service = service_for(store, config=RuntimeConfig(delegation_enabled=True))
    service.routes["paid-configured"] = ModelConfig(provider="deepseek")
    result = json.loads(service.spawn({**spawn_args(), "gateway": "paid-configured"}).content)
    assert result["status"] == "completed"
    assert events(store, "model_request_reserved")
    first = report(service.spawn(spawn_args("free")))
    service.routes["parent"] = ModelConfig(model="changed")
    assert json.loads(service.send(send_args(first)).content)["status"] == "completed"


def test_stale_execution_epoch_and_late_terminal_cannot_overwrite(repo_store):
    _, store = repo_store
    from codeagent.orchestration.agents import ChildAdapter
    from codeagent.orchestration.lease import session_lease
    import tempfile
    service = service_for(store)
    first = report(service.spawn(spawn_args()))
    payload = events(store, "agent_command_intent")[0].payload
    with session_lease(store) as epoch:
        with tempfile.TemporaryDirectory() as path:
            from pathlib import Path
            root = Path(path)
            adapter = ChildAdapter(service, service.executor, payload, store.workspace_root, epoch)
    with session_lease(store):
        with pytest.raises(RuntimeError, match="stale_execution_epoch"):
            adapter.emit("late_worker", {"run_id": first["run_id"]})
        assert adapter.finish({"status": "failed"})["status"] == "completed"
    assert len(events(store, "agent_run_finished")) == 1


def test_graph_legacy_delegate_compatibility(repo_store):
    _, store = repo_store
    runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
        runtime_config=RuntimeConfig(delegation_enabled=True))
    # 旧入口仍保持 C2 同步批次与独立工具循环语义。
    runner.run_turn("ordinary exploration")
    args = {"request_key": "legacy", "tasks": [{"goal": "read", "done_when": "README"}]}
    result = runner.delegation_service.execute(args)
    assert result.ok
    assert json.loads(result.content)["tasks"][0]["state"] == "completed"
    requests = len(events(store, "model_request_reserved"))
    assert runner.delegation_service.execute(args).content == result.content
    assert len(events(store, "model_request_reserved")) == requests
    assert len(events(store, "delegation_task_finished")) == 1


def test_cli_explicit_persistent_followup_and_replayed_command(repo_store, tmp_path):
    from dataclasses import asdict
    _, store = repo_store
    store.update_runtime_options(asdict(RuntimeConfig(delegation_enabled=True)))
    source = tmp_path / "command.json"
    source.write_text(json.dumps(spawn_args()))
    cli = CliRunner()
    args = ["agent-command", store.session_id, "spawn", str(source), "--session-root", str(store.session_root)]
    output = cli.invoke(app, args)
    assert output.exit_code == 0, output.output
    first = json.loads(json.loads(output.output)["content"])
    assert first["status"] == "completed"
    before = len(events(store, "model_request_reserved"))
    repeated = cli.invoke(app, args)
    assert repeated.exit_code == 0, repeated.output
    assert json.loads(json.loads(repeated.output)["content"])["run_id"] == first["run_id"]
    assert len(events(store, "model_request_reserved")) == before
    source.write_text(json.dumps(send_args(first)))
    output = cli.invoke(app, ["agent-command", store.session_id, "send", str(source), "--session-root", str(store.session_root)])
    assert output.exit_code == 0, output.output
    second = json.loads(json.loads(output.output)["content"])
    assert second["agent_id"] == first["agent_id"] and second["run_id"] != first["run_id"]
    assert second["root_turn_id"] != first["root_turn_id"]
    assert len(events(store, "agent_run_finished")) == 2


class SummaryModel(FakeLLM):
    def complete(self, request):
        if request.purpose == "context_condenser":
            return super().complete(request)
        return LLMResponse(text="summary complete", finish_reason="stop")


def summary_factory(config):
    return SummaryModel()


def seed_main_history(store):
    store.append_event("user_message", {"message": "old exploration"})
    from codeagent.application.session_runtime import build_registry_for_context
    registry = build_registry_for_context(store.workspace_context())
    observation = registry.call("list_dir", {}).model_dump()
    for i in range(123):
        store.append_event("assistant_tool_calls", {"tool_calls": [{"call_id": "old-" + str(i), "name": "list_dir", "arguments": {}}]})
        store.append_event("tool_result", {"call_id": "old-" + str(i), "name": "list_dir", "result": observation})
    store.append_event("assistant_message", {"message": "old completed"})


def test_main_condensation_is_journaled_and_journaled(repo_store):
    _, store = repo_store
    seed_main_history(store)
    runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
        runtime_config=RuntimeConfig(delegation_enabled=True), model_factory=summary_factory)
    output = runner.run_turn("new task")
    assert output.status == "completed", output
    assert len(events(store, "context_condensed")) == 1
    reservations = events(store, "model_request_reserved")
    assert [e.payload["purpose"] for e in reservations] == ["context_condenser", "main_agent"]
    assert all(e.payload["operation_key"] for e in reservations)
    assert output.steps_used_in_turn == 2


def test_unknown_main_condenser_not_automatically_retried(repo_store, monkeypatch):
    _, store = repo_store
    seed_main_history(store)
    original = RequestExecutor.reserve
    def crash(self, request, *args, **kwargs):
        result = original(self, request, *args, **kwargs)
        if request.purpose == "context_condenser":
            raise SimulatedCrash("summary reserved")
        return result
    runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
        runtime_config=RuntimeConfig(delegation_enabled=True), model_factory=summary_factory)
    with monkeypatch.context() as patch:
        patch.setattr(RequestExecutor, "reserve", crash)
        with pytest.raises(SimulatedCrash):
            runner.run_turn("new task")
    restarted = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
        runtime_config=RuntimeConfig(delegation_enabled=True), model_factory=summary_factory)
    assert restarted.continue_turn().status == "failed"
    assert len(events(store, "model_request_reserved")) == 1
    assert events(store, "model_request_settled")[0].payload["usage"] is None


def test_known_main_tool_result_recovers_without_replaying_handler(repo_store, monkeypatch):
    root, store = repo_store
    (root / "test_created.py").write_text("def test_created():\n    from created import VALUE\n    assert VALUE == 1\n")
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    subprocess.run(["git", "add", "test_created.py", ".gitignore"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=C2A", "-c", "user.email=test@example.invalid", "commit", "-qm", "validation fixture"], cwd=root, check=True)
    (root / "README.md").write_text("# staged user change\n")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    (root / "README.md").write_text("# unstaged user change\n")
    original_index = (root / ".git/index").read_bytes()
    runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
        runtime_config=RuntimeConfig(delegation_enabled=True), model_factory=edit_factory)
    original = runner._handle_tool_call
    calls = []
    def crash(call):
        output = original(call)
        calls.append(call.name)
        raise SimulatedCrash("main result durable; graph wrapper missing")
    with monkeypatch.context() as patch:
        patch.setattr(runner, "_handle_tool_call", crash)
        with pytest.raises(SimulatedCrash):
            runner.run_turn("create file")
    assert (root / "created.py").read_text() == "VALUE = 1\n"
    assert (store.workspace_context().active_root / "created.py").read_text() == "VALUE = 1\n"
    restarted = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
        runtime_config=RuntimeConfig(delegation_enabled=True), model_factory=edit_factory)
    def forbidden(call):
        pytest.fail("known main write replayed")
    monkeypatch.setattr(restarted, "_handle_tool_call", forbidden)
    output = restarted.continue_turn()
    assert output.status == "completed", output
    assert calls == ["apply_workspace_edit"]
    assert len(events(store, "model_request_reserved")) == 2
    assert (root / ".git/index").read_bytes() == original_index
    assert (root / "README.md").read_text() == "# unstaged user change\n"
    assert (root / "created.py").read_text() == "VALUE = 1\n"
    assert "task" in {t.name for t in restarted.tools.list_tools()}
    assert "spawn_agent" not in {t.name for t in restarted.tools.list_tools()}

def test_session_lease_excludes_threads_and_processes(repo_store):
    _, store = repo_store
    import sys
    from concurrent.futures import ThreadPoolExecutor
    from codeagent.orchestration.lease import session_lease
    def contender():
        with pytest.raises(RuntimeError, match="session_busy"):
            with session_lease(store):
                pytest.fail("concurrent thread acquired Session")
    script = '''import sys
from codeagent.session.store import SessionStore
from codeagent.orchestration.lease import session_lease
store = SessionStore(sys.argv[1], sys.argv[2], sys.argv[3])
try:
    with session_lease(store):
        raise AssertionError("concurrent process acquired Session")
except RuntimeError as exc:
    assert str(exc) == "session_busy"
    print("session_busy")
'''
    with session_lease(store):
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(contender).result()
        result = subprocess.run([sys.executable, "-c", script, str(store.workspace_root), store.session_id, str(store.session_root)], capture_output=True, text=True, check=True)
        assert result.stdout.strip() == "session_busy"




def test_graph_can_follow_old_session_without_event_seq(repo_store):
    _, store = repo_store
    store.append_event("user_message", {"message": "old user"})
    store.append_event("assistant_message", {"message": "old final"})
    old = [e.model_dump(exclude={"seq", "turn_id", "model_step_id"}) for e in store.read_events()]
    store.events_path.write_text("".join(json.dumps(e) + "\n" for e in old))
    meta = store.read_meta()
    for key in ("event_seq", "current_turn_id", "current_model_step_id"):
        meta.pop(key, None)
    store._write_state(meta)
    runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
        runtime_config=RuntimeConfig(delegation_enabled=True), model_factory=summary_factory)
    assert runner.run_turn("new user").status == "completed"
    assert store.read_events()[1].seq is None




class LongTaskModel(FakeLLM):
    def complete(self, request):
        if request.purpose == "context_condenser":
            return super().complete(request)
        seen = [int(m["tool_call_id"].removeprefix("read-")) for m in request.messages
                if m.get("role") == "tool" and m.get("tool_call_id", "").startswith("read-")]
        next_call = max(seen, default=0) + 1
        if next_call <= 66:
            return LLMResponse(tool_calls=[LLMToolCall(
                call_id=f"read-{next_call}", name="read_file",
                arguments={"path": "README.md", "start_line": 1, "end_line": 2},
            )], finish_reason="tool_calls")
        return LLMResponse(text="long task finished", finish_reason="stop")


def long_task_factory(config):
    return LongTaskModel()


def test_long_graph_turn_runs_without_recursion_or_slice_limit(repo_store):
    """超过旧图 256 节点边界；一次调用完成，无切片或次数限额。"""
    _, store = repo_store
    runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
        runtime_config=RuntimeConfig(delegation_enabled=True),
        model_factory=long_task_factory)
    result = runner.run_turn("read across multiple execution slices")
    assert result.status == "completed", result
    reservations = events(store, "model_request_reserved")
    main_requests = sum(e.payload["purpose"] == "main_agent" for e in reservations)
    assert len(events(store, "model_response")) == len(reservations)
    assert main_requests == 67
    calls = events(store, "tool_result")
    assert len(calls) == min(66, main_requests)
    assert len({event.payload["call_id"] for event in calls}) == len(calls)
    assert len(events(store, "user_message")) == 1
    assert not events(store, "execution_slice_exhausted")
    assert not any(event.payload.get("error_type") == "GraphRecursionError"
                   for event in events(store, "turn_terminated"))


def test_process_exit_after_durable_response_resumes_same_cli_run(repo_store, tmp_path):
    import sys
    from dataclasses import asdict
    root, store = repo_store
    store.update_runtime_options(asdict(RuntimeConfig(delegation_enabled=True)))
    script = tmp_path / "restart_graph.py"
    script.write_text('''import json, os, sys
from codeagent.session.store import SessionStore
from codeagent.orchestration.commands import execute_agent_command

if __name__ == "__main__":
    store = SessionStore(sys.argv[1], sys.argv[2], sys.argv[3]).load()
    original = store.append_derived_events
    def append(items, **kwargs):
        result = original(items, **kwargs)
        if sys.argv[4] == "crash" and any(kind == "model_response" for kind, _ in items):
            os._exit(73)
        return result
    store.append_derived_events = append
    result = execute_agent_command(store, "spawn", {"spec": "review", "goal": "read README", "done_when": "path evidence", "request_key": "restart-v1"})
    print(result.model_dump_json())
''')
    args = [sys.executable, str(script), str(root), store.session_id, str(store.session_root)]
    first = subprocess.run([*args, "crash"], capture_output=True, text=True, timeout=30)
    assert first.returncode == 73, first.stderr
    assert len(events(store, "model_response")) == 1
    assert not events(store, "agent_run_finished")
    second = subprocess.run([*args, "resume"], capture_output=True, text=True, timeout=30)
    assert second.returncode == 0, second.stderr
    data = json.loads(json.loads(second.stdout)["content"])
    assert data["status"] == "completed"
    assert len(events(store, "agent_command_intent")) == 1
    assert len(events(store, "agent_run_finished")) == 1
    assert len(events(store, "model_request_reserved")) == 3
    assert len(events(store, "execution_acquired")) == 2
    assert not events(store, "agent_snapshot_reclaimed")


def test_replayed_old_cli_command_does_not_close_newer_user_turn(repo_store):
    from codeagent.orchestration.commands import execute_agent_command
    from codeagent.runtime.turn_state import current_turn_state
    from dataclasses import asdict
    _, store = repo_store
    store.update_runtime_options(asdict(RuntimeConfig(delegation_enabled=True)))
    first = report(execute_agent_command(store, "spawn", spawn_args(), model_factory=conversation_factory))
    pending = store.append_event("user_message", {"message": "PARENT_SECRET new unfinished root"})
    count = len(events(store, "model_request_reserved"))
    replayed = report(execute_agent_command(store, "spawn", spawn_args(), model_factory=conversation_factory))
    assert replayed["run_id"] == first["run_id"]
    budget = current_turn_state(store.read_events())
    assert budget.turn_id == pending.turn_id and not budget.closed
    assert len(events(store, "model_request_reserved")) == count


def test_gap_in_event_log_is_not_silently_accepted(repo_store):
    _, store = repo_store
    store.append_event("user_message", {"message": "test gap"})
    store.append_event("assistant_message", {"message": "done"})
    lines = store.events_path.read_text().splitlines()
    store.events_path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    from codeagent.orchestration.lease import session_lease
    from codeagent.session.store import SessionStoreError
    with pytest.raises(SessionStoreError, match="sequence"):
        with session_lease(store):
            pytest.fail("corrupt events accepted")


def test_graph_runtime_build_requires_session_execution_lease(repo_store):
    _, store = repo_store
    from concurrent.futures import ThreadPoolExecutor
    from codeagent.orchestration.lease import session_lease
    def contender():
        with pytest.raises(RuntimeError, match="session_busy"):
            build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
                runtime_config=RuntimeConfig(delegation_enabled=True))
    with session_lease(store):
        before = store.read_meta()["event_seq"]
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(contender).result()
        assert store.read_meta()["event_seq"] == before
