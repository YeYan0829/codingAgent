import json
import multiprocessing
import subprocess
import threading
import time
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from typer.testing import CliRunner

from codeagent.application.session_runtime import build_agent_runner
from codeagent.cli import app
from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.context.projector import project_events
from codeagent.model_gateway.base import BaseModelClient, LLMResponse, LLMToolCall, ModelRequest
from codeagent.model_gateway.fake import FakeLLM
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.delegation import DelegationService, recover_delegations
from codeagent.runtime.request_executor import RequestExecutor, ExecutionInterrupted
from codeagent.runtime.policy import ReadOnlyChildPolicy
from codeagent.runtime.turn_state import current_turn_state
from codeagent.session.store import SessionStore
from codeagent.tools.base import PermissionLevel, ToolResult, ToolSpec
from codeagent.tools.registry import ToolRegistry


class ControlledModel(BaseModelClient):
    """可 spawn 的免费模型；按任务及真实 observation 决定下一步。"""
    def complete(self, request):
        task = json.loads([m for m in request.messages if m["role"] == "user"][-1]["content"])
        mode = task["goal"]
        observations = [m for m in request.messages if m["role"] == "tool"]
        if mode == "slow":
            time.sleep(30)
            return LLMResponse(tool_calls=[LLMToolCall(call_id="late", name="read_file", arguments={"path": "README.md"})])
        if mode == "crash":
            raise RuntimeError("controlled failure")
        if mode == "escape" and not observations:
            return LLMResponse(tool_calls=[LLMToolCall(call_id=str(i), name=name, arguments=args)
                for i, (name, args) in enumerate([
                    ("run_command", {"command": "touch ESCAPED"}),
                    ("apply_workspace_edit", {"operations": [{"op": "create_file", "path": "ESCAPED", "content": "x"}]}),
                    ("delegate_readonly", {"tasks": []}),
                    ("read_file", {"path": "../outside"}),
                    ("read_file", {"path": ".env"}),
                ])])
        if mode == "duplicate":
            return LLMResponse(tool_calls=[LLMToolCall(call_id="same", name="list_dir", arguments={})] * 2)
        if mode == "loop" or not observations:
            return LLMResponse(tool_calls=[LLMToolCall(call_id="read-" + str(len(observations)), name="read_file",
                                                     arguments={"path": "README.md"})])
        return LLMResponse(text=json.dumps({"context": task, "observations": observations}, ensure_ascii=False))


def controlled_factory(config):
    return ControlledModel()


class FinalModel(BaseModelClient):
    def complete(self, request):
        return LLMResponse(text="final")


def final_factory(config):
    return FinalModel()


class SlowParent(FakeLLM):
    def complete(self, request):
        if request.purpose == "readonly_child":
            return ControlledModel().complete(request)
        return LLMResponse(tool_calls=[LLMToolCall(call_id="slow-delegate", name="task",
            arguments={"subagent_type": "explore", "prompt": "slow"})])


def slow_factory(config):
    return SlowParent()


class EditAfterDelegation(FakeLLM):
    def complete(self, request):
        if request.purpose == "readonly_child":
            return super().complete(request)
        obs = [m for m in request.messages if m.get("role") == "tool"]
        if not obs:
            return LLMResponse(tool_calls=[LLMToolCall(call_id="delegate", name="task",
                arguments={"subagent_type": "explore", "prompt": "read"})])
        if len(obs) == 1:
            return LLMResponse(tool_calls=[LLMToolCall(call_id="edit", name="apply_workspace_edit", arguments={
                "operations": [{"op": "create_file", "path": "new.py", "content": "VALUE = 1\n"}],
            })])
        assert obs[-1]["tool_call_id"] == "edit" and json.loads(obs[-1]["content"])["ok"]
        return LLMResponse(text="主 Agent 已直接编辑当前项目，旧观察保留")


def editing_factory(config):
    return EditAfterDelegation()


def arguments(*goals, key="batch-one"):
    return {"request_key": key, "tasks": [{"goal": goal, "done_when": "有路径证据", "context": "only-explicit-context"}
                                         for goal in (goals or ("read", "read"))]}


@pytest.fixture
def repo_store(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "README.md").write_text("# Snapshot baseline\nENTRY = main.py\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=C2", "-c", "user.email=c2@example.invalid", "commit", "-qm", "baseline"], cwd=repo, check=True)
    return repo, SessionStore(repo, session_root=tmp_path / "sessions").create()


def service_for(store, config=None, factory=controlled_factory, cancelled=lambda: False, model_config=None):
    store.append_event("user_message", {"message": "PARENT_SECRET_DO_NOT_COPY", "max_model_steps_per_user_turn": None})
    budget = RequestExecutor(store, config or RuntimeConfig(delegation_enabled=True), model_config or ModelConfig(), factory, cancelled)
    return DelegationService(store, budget, store.workspace_context)


def events(store, kind):
    return [e for e in store.read_events() if e.type == kind]


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition timed out"
        time.sleep(.02)


def test_real_runner_delegates_gets_results_and_keeps_acting(repo_store):
    repo, store = repo_store
    runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
                                runtime_config=RuntimeConfig(delegation_enabled=True))
    output = runner.run_turn("请委派定位并独立审查")
    assert output.status == "completed"
    assert [s["tool"] for s in output.steps] == ["task", "task", "list_dir"]
    assert len(events(store, "agent_run_finished")) == 2
    assert events(store, "agent_run_finished")[0].payload["status"] == "completed"
    assert any(e.payload["kind"] == "tool_result" for e in events(store, "agent_history"))
    assert output.steps_used_in_turn == len(events(store, "model_request_reserved"))
    assert len(project_events(store.read_events())) == 1
    assert (repo / "README.md").read_text().startswith("# Snapshot")


def test_cli_free_delegation_roundtrip(repo_store, tmp_path):
    repo, _ = repo_store
    result = CliRunner().invoke(app, ["ask", str(repo), "委派审查", "--delegation", "--session-root", str(tmp_path / "cli-sessions")])
    assert result.exit_code == 0, result.output
    assert "task" in result.output and "spawn_agent" not in result.output


def test_child_context_and_tool_results_are_isolated(repo_store):
    _, store = repo_store
    service = service_for(store)
    result = json.loads(service.execute(arguments("read")).content)
    report = json.loads(result["tasks"][0]["report"])
    assert "PARENT_SECRET_DO_NOT_COPY" not in json.dumps(report)
    assert report["context"]["context"] == "only-explicit-context"
    assert "identity" not in report["context"] and "identity" not in result
    assert "Snapshot baseline" in report["observations"][0]["content"]


def test_runtime_denies_command_write_recursion_and_path_escapes(repo_store):
    repo, store = repo_store
    result = json.loads(service_for(store).execute(arguments("escape")).content)
    assert result["tasks"][0]["state"] == "completed"
    results = [e.payload["data"]["result"] for e in events(store, "agent_history") if e.payload["kind"] in {"tool_result", "tool_denied"}]
    assert [r["error_code"] for r in results[:3]] == ["policy_denied"] * 3
    assert all(not r["ok"] for r in results)
    assert not (repo / "ESCAPED").exists()


def test_registry_and_policy_enforce_capabilities():
    registry = ToolRegistry(allowed_names=frozenset({"read_file"}), readonly=True)
    for name, permission in [("run_command", PermissionLevel.READ), ("read_file", PermissionLevel.WORKSPACE_WRITE)]:
        tool = ToolSpec(name, "", permission, {}, lambda _: ToolResult(ok=True))
        with pytest.raises(ValueError):
            registry.register(tool)
        assert ReadOnlyChildPolicy().evaluate(tool).decision == "deny"


def test_child_cannot_widen_parent_policy(repo_store):
    from codeagent.runtime.policy import DefaultPolicy, PolicyResult, PolicyDecision
    _, store = repo_store
    service = service_for(store)
    class ParentPolicy(DefaultPolicy):
        def evaluate(self, tool):
            if tool.name == "read_file":
                return PolicyResult(PolicyDecision.DENY, "restricted parent")
            return super().evaluate(tool)
    service.parent_policy = ParentPolicy()
    result = json.loads(service.execute(arguments("read")).content)
    assert result["tasks"][0]["state"] == "completed"
    assert next(e.payload["data"]["result"]["error_code"] for e in events(store, "agent_history") if e.payload["kind"] in {"tool_result", "tool_denied"}) == "policy_denied"


def test_concurrent_step_reservation_is_top_level_and_atomic(repo_store):
    _, store = repo_store
    config = RuntimeConfig(delegation_enabled=True)
    service = service_for(store, config, factory=final_factory)
    request = ModelRequest(messages=[{"role": "user", "content": "finish"}])
    def call(index):
        try:
            return service.executor.complete(request, str(index)).text
        except ExecutionInterrupted as exc:
            return exc.reason
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(call, range(8)))
    assert outcomes.count("final") == 8
    assert "steps" not in outcomes
    assert len(events(store, "model_request_reserved")) == 8
    assert current_turn_state(store.read_events()).used == 8




def test_fee_unknown_and_failures_keep_reservations(repo_store):
    _, store = repo_store
    config = RuntimeConfig(delegation_enabled=True, cost_per_token_microunits=2)
    service = service_for(store, config)
    result = json.loads(service.execute(arguments("crash")).content)
    assert result["tasks"][0]["state"] == "failed"
    unknown_fee = events(store, "model_request_reserved")[0].payload["fee_reserved_microunits"]
    assert service.executor.occupied_fee() == unknown_fee == 0
    service.execute(arguments("read", key="retry"))
    assert events(store, "model_request_settled")[0].payload["fee_status"] == "usage_unavailable"
    assert service.executor.occupied_fee() == 0






@pytest.mark.parametrize("mode", ["crash", "duplicate"])
def test_child_error_is_returned_once(repo_store, mode):
    _, store = repo_store
    result = json.loads(service_for(store).execute(arguments(mode)).content)
    assert result["tasks"][0]["state"] == "failed"
    assert len(events(store, "delegation_task_finished")) == 1


def test_idempotent_batch_and_revision_invalidation(repo_store):
    repo, store = repo_store
    service = service_for(store)
    args = arguments("read")
    first = service.execute(args)
    requests = len(events(store, "model_request_reserved"))
    replay = service.execute(args)
    assert json.loads(replay.content) == json.loads(first.content)
    assert len(events(store, "model_request_reserved")) == requests
    assert service.execute(arguments("escape")).error_code == "request_key_conflict"
    (repo / "README.md").write_text("changed\n")
    assert json.loads(service.execute(args).content) == json.loads(first.content)
    refreshed = json.loads(service.execute(arguments("read", key="current-files")).content)
    assert "changed" in refreshed["tasks"][0]["report"]
    assert not events(store, "delegation_results_invalidated")




def test_main_can_edit_after_delegation_and_sees_stale_results(repo_store):
    repo, store = repo_store
    runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
                                runtime_config=RuntimeConfig(delegation_enabled=True), model_factory=editing_factory)
    output = runner.run_turn("调查后编辑")
    assert output.status == "completed", output
    assert output.steps[-1]["result"]["ok"]
    assert (store.workspace_context().active_root / "new.py").exists()
    assert (repo / "new.py").read_text() == "VALUE = 1\n"
    assert "task" in {t.name for t in runner.tools.list_tools()}
    assert "delegate_readonly" not in {t.name for t in runner.tools.list_tools()}
    assert not events(store, "delegation_results_invalidated")


def test_restart_converges_and_does_not_refund_or_replay(repo_store):
    _, store = repo_store
    service = service_for(store)
    store.append_event("assistant_tool_calls", {"tool_calls": [{"call_id": "unfinished", "name": "delegate_readonly", "arguments": arguments()}]})
    service.executor.emit("delegation_task_queued", {"batch_id": "old", "task_id": "old-child", "parent_id": "main"})
    service.executor.reserve(ModelRequest(messages=[], max_tokens=100), "old-child")
    recover_delegations(store)
    recover_delegations(store)
    assert len(events(store, "delegation_task_finished")) == 1
    assert events(store, "delegation_task_finished")[0].payload["state"] == "interrupted"
    assert len(events(store, "model_request_settled")) == 1
    assert len(events(store, "tool_denied")) == 1
    assert project_events(store.read_events())[0].model_steps[0].closed
    resumed = RequestExecutor(store, RuntimeConfig(delegation_enabled=True), ModelConfig(), controlled_factory, lambda: False)
    assert not hasattr(resumed, "deadline_epoch")
    assert current_turn_state(store.read_events()).used == 1






def test_product_stop_reaches_children_and_acknowledges_after_cleanup(repo_store):
    from codeagent.product.execution import ExecutionSupervisor
    _, store = repo_store
    store.update_runtime_options({"delegation_enabled": True})
    def factory(*args, **kwargs):
        return build_agent_runner(*args, **kwargs, model_factory=slow_factory)
    supervisor = ExecutionSupervisor(runner_factory=factory)
    supervisor.start(store, message="受控取消")
    wait_for(lambda: len(events(store, "model_request_reserved")) >= 2)
    supervisor.stop(store)
    wait_for(lambda: supervisor.state(store.session_id) == "idle")
    assert current_turn_state(store.read_events()).closed
    assert len(events(store, "agent_run_finished")) == len(events(store, "agent_run_ack")) == 1
    assert all(e.payload["status"] == "cancelled" for e in events(store, "agent_run_finished"))
    assert len(events(store, "tool_denied")) == 0  # 已执行的同步委派工具有失败 / 取消任务结果。
    assert events(store, "execution_stop_acknowledged")
    assert not multiprocessing.active_children()


def test_transport_disables_hidden_retries_and_fallback():
    from codeagent.model_gateway.openai_client import OpenAICompatibleClient
    calls = []
    class Transport:
        def with_options(self, **options):
            assert options == {"max_retries": 0}
            return self
        def create(self, **kwargs):
            calls.append(kwargs)
            raise TypeError("unsupported extra_body")
    transport = Transport()
    transport.chat = SimpleNamespace(completions=transport)
    client = OpenAICompatibleClient(model="fixture", api_key="none", base_url="http://invalid",
                                    provider_name="fixture", client=transport, extra_body={"option": True})
    with pytest.raises(TypeError):
        client.complete(ModelRequest(messages=[], journaled=True))
    assert len(calls) == 1


def test_summaries_failures_and_retries_all_share_ledger(repo_store):
    _, store = repo_store
    service = service_for(store, RuntimeConfig(delegation_enabled=True), final_factory)
    for purpose in ("main_agent", "context_condenser", "readonly_child"):
        service.executor.complete(ModelRequest(messages=[], purpose=purpose))
    service.executor.complete(ModelRequest(messages=[], purpose="context_condenser"))
    assert [e.payload["purpose"] for e in events(store, "model_request_reserved")] == ["main_agent", "context_condenser", "readonly_child", "context_condenser"]


def test_real_condenser_frontier_accepts_only_own_budget_events(repo_store, monkeypatch):
    from test_semantic_condensation import _append_tool_step
    _, store = repo_store
    store.append_event("user_message", {"message": "历史任务"})
    for i in range(6):
        _append_tool_step(store, i)
    store.append_event("assistant_message", {"message": "历史完成"})
    runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
                                runtime_config=RuntimeConfig(delegation_enabled=True))
    build = runner.context_manager.build
    def small_context(**kwargs):
        monkeypatch.setattr("codeagent.context.builder.HISTORY_MAX_EVENTS", 4)
        return build(**kwargs)
    monkeypatch.setattr(runner.context_manager, "build", small_context)
    output = runner.run_turn("继续只读检查")
    assert output.status == "completed", output
    assert events(store, "context_condensed")
    assert any(e.payload["purpose"] == "context_condenser" for e in events(store, "model_request_reserved"))
    assert not any(e.payload.get("failure_code") == "stale_frontier" for e in events(store, "context_condensation_attempt"))
