"""同步主入口与旧后台内部兼容的免费工程验收，不评估模型语义。"""
import copy
import hashlib
import json
import multiprocessing
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codeagent.application.session_runtime import build_agent_runner
from codeagent.cli import app
from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.model_gateway.base import LLMResponse, LLMToolCall, ModelRequest, TokenUsage
from codeagent.model_gateway.fake import FakeLLM
from codeagent.orchestration.agents import AgentSessions
from codeagent.orchestration.lease import session_lease
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.delegation import DelegationService
from codeagent.runtime.request_executor import RequestExecutor, ExecutionInterrupted, occupied_fee
from codeagent.runtime.turn_state import current_turn_state
from test_orchestration import repo_store, spawn_args, send_args, service_for, events, SimulatedCrash


def data(result):
    assert result.ok, result
    return json.loads(result.content)


def bg(key, goal="slow"):
    return {**spawn_args(key, goal), "execution": "background"}


def until(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "fixture barrier timed out"
        time.sleep(.01)


class BackgroundModel(FakeLLM):
    def complete(self, request):
        if request.purpose != "readonly_child":
            return super().complete(request)
        users = [m for m in request.messages if m["role"] == "user"]
        goal = json.loads(users[-1]["content"])["goal"]
        if goal == "slow":
            time.sleep(30)
        if goal == "fail":
            raise RuntimeError("fixture failure")
        return super().complete(request)


def background_factory(config):
    return BackgroundModel()


@contextmanager
def running_service(store, **kwargs):
    service = service_for(store, factory=background_factory, **kwargs)
    with session_lease(store) as epoch:
        yield service, epoch


def test_b2_b3_real_wait_busy_queue_cancel_and_duplicate_ack(repo_store):
    _, store = repo_store
    before = {p.pid for p in multiprocessing.active_children()}
    with running_service(store) as (service, epoch):
        receipts = [data(service.spawn(bg(str(i)))) for i in range(6)]
        ids = [r["run_id"] for r in receipts]
        until(lambda: len(events(store, "model_request_reserved")) == 2)
        assert len(service.scheduler.active) == 2 and len(service.scheduler.queue) == 4
        assert service.spawn(bg("overload")).error_code == "queue_full"
        assert len(events(store, "agent_command_intent")) == 6
        assert service.send({**send_args(receipts[0]), "execution": "background"}).error_code == "agent_busy"
        assert service.close({"agent_id": receipts[0]["agent_id"]}).error_code == "agent_busy"
        start = time.monotonic()
        timed = data(service.wait({"run_ids": ids, "mode": "any", "timeout": .05}))
        assert time.monotonic() - start < 2
        assert timed["timed_out"] and timed["pending"] == ids and not timed["reports"]
        assert not events(store, "agent_cancel_requested") and not events(store, "agent_run_ack")
        assert len(events(store, "model_request_reserved")) == 2  # 等待不轮询模型。
        assert service.wait({"run_ids": ["unknown"], "timeout": 0.0}).error_code == "unknown_run"
        assert service.wait({"run_ids": [ids[0], ids[0]]}).error_code == "duplicate_run_id"
        cancel = {"run_ids": ids, "request_key": "stop-all"}
        receipt = data(service.cancel(cancel))
        assert receipt["receipt"] == "cancel_requested"
        assert data(service.cancel(cancel))["cancel_id"] == receipt["cancel_id"]
        assert service.cancel({**cancel, "run_ids": [ids[0]]}).error_code == "request_key_conflict"
        waited = data(service.wait({"run_ids": ids, "mode": "all", "timeout": 10.0}))
        assert waited["satisfied"] and not waited["pending"]
        assert {r["status"] for r in waited["reports"]} == {"cancelled"}
        assert len(events(store, "agent_run_finished")) == len(events(store, "agent_run_ack")) == 6
        assert len(events(store, "model_request_reserved")) == 2  # queued 取消不 dispatch。
        for rid in ids:
            service.ack(rid, epoch)
        assert len(events(store, "agent_run_ack")) == 6
        again = data(service.wait({"run_ids": [ids[0]], "mode": "any", "timeout": 0.0}))
        assert again["satisfied"] and again["run_ids"] == [ids[0]]
        first = receipts[0]
        assert data(service.close({"agent_id": first["agent_id"]}))["status"] == "closed"
        assert service.send(send_args(first)).error_code == "agent_closed"
    assert {p.pid for p in multiprocessing.active_children()} == before
    assert not any(t.name.startswith(("codeagent-graph", "codeagent-root-watch")) for t in threading.enumerate())
    for ack in events(store, "agent_run_ack"):
        rid = ack.payload["run_id"]
        assert next(e.seq for e in events(store, "agent_run_finished") if e.payload["run_id"] == rid) < ack.seq
        assert all(e.seq < ack.seq for e in events(store, "agent_snapshot_reclaimed") if e.payload["run_id"] == rid)


def test_b2_any_counts_failure_and_leaves_other_run_running(repo_store):
    _, store = repo_store
    with running_service(store) as (service, _):
        slow = data(service.spawn(bg("slow")))
        failed = data(service.spawn(bg("failure", "fail")))
        result = data(service.wait({"run_ids": [slow["run_id"], failed["run_id"]], "mode": "any", "timeout": 10.0}))
        assert result["satisfied"] and result["pending"] == [slow["run_id"]]
        assert result["reports"][0]["status"] == "failed"
        assert not events(store, "agent_cancel_requested")
        assert not service.terminal(slow["run_id"])
        assert data(service.spawn(bg("failure", "fail")))["run_id"] == failed["run_id"]


class DynamicModel(BackgroundModel):
    """同步任务、当前文件续问与父编辑；所有动作由真实工具观察推进。"""
    def complete(self, request):
        if request.purpose != "main_agent":
            return super().complete(request)
        obs = {m["tool_call_id"]: json.loads(m["content"]) for m in request.messages if m["role"] == "tool"}
        def value(cid):
            assert obs[cid]["ok"], obs[cid]
            return json.loads(obs[cid]["content"])
        def call(cid, name, args):
            return LLMToolCall(call_id=cid, name=name, arguments=args)
        def response(*calls):
            return LLMResponse(tool_calls=list(calls))
        if "spawn-A" not in obs:
            return response(call("spawn-A", "task", {"subagent_type": "review", "prompt": "A"}))
        if "spawn-B" not in obs:
            return response(call("spawn-B", "task", {"subagent_type": "review", "prompt": "B"}))
        a, b = value("spawn-A"), value("spawn-B")
        if "main-read" not in obs:
            return response(call("main-list", "list_dir", {}), call("main-read", "read_file", {"path": "README.md"}))
        if "follow-B" not in obs:
            return response(call("follow-B", "task", {"resume": b["task_id"], "prompt": "follow B"}))
        if "edit-R2" not in obs:
            return response(call("edit-R2", "apply_workspace_edit", {"operations": [{"op": "replace_text", "path": "README.md",
                "expected_sha256": obs["main-read"]["metadata"]["sha256"], "old_text": "# R1", "new_text": "# R2"}]}))
        if "refresh-B" not in obs:
            assert obs["edit-R2"]["ok"]
            return response(call("refresh-B", "task", {"resume": b["task_id"], "prompt": "review R2"}))
        assert "R1" in a["text"] and "R1" in value("follow-B")["text"]
        assert "R2" in value("refresh-B")["text"]
        return LLMResponse(text="fixture delivered R2; historical reports retained", finish_reason="stop")

def dynamic_factory(config):
    return DynamicModel()


@pytest.mark.parametrize("entry", ["runtime", "cli"])
def test_b1_sync_task_followup_reads_current_files(repo_store, tmp_path, monkeypatch, entry):
    root, store = repo_store
    message = json.dumps({"fixture_dir": str(tmp_path)})
    if entry == "runtime":
        runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
            runtime_config=RuntimeConfig(delegation_enabled=True), model_factory=dynamic_factory)
        out = runner.run_turn(message)
        assert out.status == "completed", out
        assert "delivered R2" in out.final_text
    else:
        monkeypatch.setattr("codeagent.cli.build_model_client", dynamic_factory)
        out = CliRunner().invoke(app, ["ask", str(root), message, "--delegation", "--session-root", str(tmp_path / "cli")])
        assert out.exit_code == 0 and "delivered R2" in out.output, out.output
        meta = store.list_all_sessions(session_root=tmp_path / "cli")[0]
        store = type(store)(root, meta["session_id"], tmp_path / "cli").load()
    assert (root / "README.md").read_text().startswith("# R2")
    assert (store.workspace_context().active_root / "README.md").read_text().startswith("# R2")
    commands = events(store, "agent_command_intent")
    assert len(commands) == 4
    assert commands[1].payload["agent"]["agent_id"] == commands[2].payload["agent"]["agent_id"] == commands[3].payload["agent"]["agent_id"]
    main_read = next(e for e in events(store, "tool_result") if e.payload["call_id"] == "main-read")
    finished = events(store, "agent_run_finished")
    assert all(e.seq < main_read.seq for e in finished[:2])
    assert all(e.seq > main_read.seq for e in finished[2:])
    assert len(events(store, "agent_run_ack")) == 4
    assert max(e.seq for e in events(store, "agent_run_ack")) < events(store, "assistant_message")[-1].seq
    follow_run = commands[2].payload["run"]["run_id"]
    assert any(e.payload["data"].get("message", "").startswith("我完成") for e in events(store, "agent_history")
               if e.payload["run_id"] == follow_run and e.payload["kind"] == "assistant_message")


class EarlyFinalModel(BackgroundModel):
    def complete(self, request):
        if request.purpose == "readonly_child":
            return super().complete(request)
        if not any(m["role"] == "tool" for m in request.messages):
            goal = "read" if "normal-child" in str(request.messages) else "slow" if "slow" in str(request.messages) else "fail"
            return LLMResponse(tool_calls=[LLMToolCall(call_id="child", name="task", arguments={"subagent_type": "review", "prompt": goal})])
        return LLMResponse(text="parent final")


def early_final_factory(config):
    return EarlyFinalModel()


@pytest.mark.parametrize("mode", ["failure", "stop"])
def test_b4_sync_child_failure_and_stop_leave_no_billable_children(repo_store, mode):
    _, store = repo_store
    stop = threading.Event()
    config = RuntimeConfig(delegation_enabled=True)
    runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
        runtime_config=config, model_factory=early_final_factory, cancellation_callback=stop.is_set)
    if mode == "stop":
        def stop_when_running():
            until(lambda: any(e.payload["agent_id"] != "main" for e in events(store, "model_request_reserved")))
            stop.set()
        thread = threading.Thread(target=stop_when_running)
        thread.start()
    out = runner.run_turn("failure" if mode == "failure" else "slow " + mode)
    if mode == "stop":
        thread.join()
    assert not multiprocessing.active_children()
    assert len(events(store, "agent_run_finished")) == len(events(store, "agent_run_ack")) == 1
    end = events(store, "assistant_message") + events(store, "turn_terminated")
    assert end and max(e.seq for e in events(store, "agent_run_ack")) < max(e.seq for e in end)
    if mode == "failure":
        assert out.status == "completed"
        assert not events(store, "agent_parent_closing")
    else:
        assert out.status == "stopped"
        assert events(store, "agent_run_finished")[0].payload["status"] == "cancelled"




def test_b6_known_settlement_releases_difference_unknown_and_duplicate_stay(repo_store):
    _, store = repo_store
    store.append_event("user_message", {"message": "fees"})
    config = RuntimeConfig(cost_per_token_microunits=2)
    budget = RequestExecutor(store, config, ModelConfig(), background_factory, lambda: False)
    req = ModelRequest(messages=[], max_tokens=100)
    known = budget.reserve(req, "main")
    unknown = budget.reserve(req, "main")
    budget.settle(known, "main", "completed", {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    budget.settle(known, "main", "completed", {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150})
    budget.settle(unknown, "main", "interrupted", None)
    reserved_unknown = events(store, "model_request_reserved")[1].payload["fee_reserved_microunits"]
    assert budget.occupied_fee() == 30 + reserved_unknown
    assert len(events(store, "model_request_settled")) == 2
    third = budget.reserve(req, "main")  # 差额释放后可以预留。
    budget.settle(third, "main", "completed", {"input_tokens": 10, "output_tokens": 5, "total_tokens": 1000})
    assert budget.occupied_fee() == 30 + 2 * reserved_unknown
    assert events(store, "model_request_settled")[-1].payload["fee_state"] == "unknown"
    old_turn = budget.turn_id
    store.append_event("assistant_message", {"message": "done"})
    store.append_event("user_message", {"message": "new"})
    renewed = RequestExecutor(store, config, ModelConfig(), background_factory, lambda: False)
    assert renewed.occupied_fee() == 0
    assert occupied_fee(store.read_events(), old_turn) == 30 + 2 * reserved_unknown


def test_b5_background_intent_recovery_and_unknown_no_resend(repo_store, monkeypatch):
    _, store = repo_store
    service = service_for(store, factory=background_factory)
    original = store.append_derived_events
    def crash(items, **kwargs):
        result = original(items, **kwargs)
        if any(kind == "agent_command_intent" for kind, _ in items):
            raise SimulatedCrash()
        return result
    with monkeypatch.context() as patch:
        patch.setattr(store, "append_derived_events", crash)
        with pytest.raises(SimulatedCrash), session_lease(store):
            service.spawn(bg("recover", "read"))
    rid = events(store, "agent_command_intent")[0].payload["run"]["run_id"]
    assert not events(store, "model_request_reserved")
    restarted = service_for(store, factory=background_factory)
    with session_lease(store) as epoch:
        restarted.recover(epoch)
        completed = data(restarted.wait({"run_ids": [rid], "timeout": 10.0}))
        assert completed["satisfied"] and completed["reports"][0]["status"] == "completed"
    assert len(events(store, "agent_command_intent")) == 1




@pytest.mark.parametrize("boundary", ["agent_command_intent", "model_request_reserved", "model_response",
                                      "tool_execution_result", "agent_run_finished", "agent_run_ack"])
def test_b5_real_process_exit_reconciles_background_windows(repo_store, tmp_path, boundary):
    import os
    root, store = repo_store
    script = tmp_path / "background_restart.py"
    script.write_text('''import json, os, sys
from codeagent.session.store import SessionStore
from codeagent.orchestration.lease import session_lease
from test_background_orchestration import service_for, background_factory, bg

if __name__ == "__main__":
    store = SessionStore(sys.argv[1], sys.argv[2], sys.argv[3]).load()
    boundary, mode = sys.argv[4:6]
    original = store.append_derived_events
    def append(items, **kwargs):
        result = original(items, **kwargs)
        if mode == "crash" and any(kind == boundary for kind, _ in items):
            os._exit(73)
        return result
    store.append_derived_events = append
    service = service_for(store, factory=background_factory)
    with session_lease(store) as epoch:
        service.recover(epoch)
        receipt = service.spawn(bg("process-recovery", "read"))
        rid = json.loads(receipt.content)["run_id"]
        result = service.wait({"run_ids": [rid], "timeout": 60.0})
        print(result.model_dump_json(), flush=True)
''')
    env = {**os.environ, "PYTHONPATH": str(Path.cwd()) + ":" + str(Path.cwd() / "tests")}
    args = [sys.executable, str(script), str(root), store.session_id, str(store.session_root), boundary]
    first = subprocess.run([*args, "crash"], capture_output=True, text=True, timeout=90, env=env)
    assert first.returncode == 73, first.stderr + first.stdout
    before = len(events(store, "model_request_reserved"))
    resumed = subprocess.run([*args, "resume"], capture_output=True, text=True, timeout=90, env=env)
    assert resumed.returncode == 0, resumed.stderr + resumed.stdout
    result = json.loads(json.loads(resumed.stdout)["content"])
    assert result["satisfied"]
    assert len(events(store, "agent_command_intent")) == 1
    assert len(events(store, "agent_run_finished")) == len(events(store, "agent_run_ack")) == 1
    assert len({e.payload["request_id"] for e in events(store, "model_request_settled")}) == len(events(store, "model_request_settled"))
    if boundary == "model_request_reserved":
        assert result["reports"][0]["status"] == "interrupted"
        assert len(events(store, "model_request_reserved")) == before == 1
        assert events(store, "model_request_settled")[0].payload["fee_state"] == "unknown"
    else:
        assert result["reports"][0]["status"] == "completed", result
        assert len(events(store, "model_request_reserved")) == 3
    assert len(events(store, "execution_acquired")) == 2


def test_b5_late_worker_budget_and_notification_fenced(repo_store):
    from contextvars import copy_context
    _, store = repo_store
    service = service_for(store, factory=background_factory)
    with session_lease(store) as epoch:
        stale_context = copy_context()
        budget = copy.copy(service.executor)
        budget.execution_epoch = epoch
        scheduler = service._scheduler(epoch)
    with session_lease(store):
        with pytest.raises(RuntimeError, match="stale_execution_epoch"):
            stale_context.run(service.emit, "late_notification", {"run_id": "late"})
        with pytest.raises(RuntimeError, match="stale_execution_epoch"):
            stale_context.run(budget.reserve, ModelRequest(messages=[], max_tokens=100), "late")
        with pytest.raises(RuntimeError, match="stale_execution_epoch"):
            stale_context.run(scheduler._done, "late", None)
    assert not events(store, "late_notification")
    assert not events(store, "model_request_reserved")


class KnownUsageModel(FakeLLM):
    def complete(self, request):
        return LLMResponse(text="known", finish_reason="stop", usage=TokenUsage(input_tokens=5, output_tokens=7, total_tokens=12))


def known_factory(config):
    return KnownUsageModel()


def test_b6_transport_and_summaries_settle_each_request_once(repo_store):
    _, store = repo_store
    store.append_event("user_message", {"message": "known transport"})
    config = RuntimeConfig(cost_per_token_microunits=2)  # 本例只测试transport/摘要去重；收尾竞争另测。
    budget = RequestExecutor(store, config, ModelConfig(), known_factory, lambda: False)
    for purpose in ("main_agent", "context_condenser", "main_agent"):
        assert budget.complete(ModelRequest(messages=[], max_tokens=100, purpose=purpose)).text == "known"
    assert budget.occupied_fee() == 3 * 24
    assert len(events(store, "model_request_reserved")) == len(events(store, "model_request_settled")) == 3
    assert all(e.payload["fee_state"] == "settled_known" for e in events(store, "model_request_settled"))




def test_wait_intent_result_window_keeps_fixed_id_and_original_timeout(repo_store, monkeypatch):
    from codeagent.orchestration.agents import ACTIVE_TOOL_OPERATION
    _, store = repo_store
    with running_service(store) as (service, epoch):
        run = data(service.spawn(bg("instant", "read")))
        assert data(service.wait({"run_ids": [run["run_id"]], "timeout": 10.0}))["satisfied"]
        original = store.append_derived_events
        def crash(items, **kwargs):
            result = original(items, **kwargs)
            if any(kind == "agent_wait_finished" for kind, _ in items):
                raise SimulatedCrash()
            return result
        token = ACTIVE_TOOL_OPERATION.set("main:fixed-wait")
        try:
            with monkeypatch.context() as patch:
                patch.setattr(store, "append_derived_events", crash)
                with pytest.raises(SimulatedCrash):
                    service.wait({"run_ids": [run["run_id"]], "timeout": .1})
            result = data(service.wait({"run_ids": [run["run_id"]], "timeout": .1}))
            assert result["satisfied"]
            assert len([e for e in events(store, "agent_wait_finished") if e.payload["wait_id"] == result["wait_id"]]) == 1
            assert service.wait({"run_ids": [run["run_id"]], "timeout": .2}).error_code == "wait_identity_conflict"
        finally:
            ACTIVE_TOOL_OPERATION.reset(token)


def test_idle_session_explicit_new_user_turn_background_followup(repo_store):
    from codeagent.orchestration.commands import execute_agent_command
    _, store = repo_store
    config = RuntimeConfig(delegation_enabled=True)
    store.update_runtime_options(asdict(config))
    first = data(execute_agent_command(store, "spawn", bg("first-user", "read"), model_factory=background_factory))
    assert first["receipt"] == "accepted" and current_turn_state(store.read_events()).closed
    old_turn = current_turn_state(store.read_events()).turn_id
    old_count = len(events(store, "model_request_reserved"))
    second = data(execute_agent_command(store, "send", {**send_args(first, "next-user"), "execution": "background"}, model_factory=background_factory))
    assert second["agent_id"] == first["agent_id"] and second["run_id"] != first["run_id"]
    new_turn = current_turn_state(store.read_events()).turn_id
    assert new_turn != old_turn and current_turn_state(store.read_events()).closed
    assert len([e for e in events(store, "model_request_reserved") if e.turn_id == old_turn]) == old_count
    assert events(store, "agent_run_finished")[-1].turn_id == new_turn
    assert len(events(store, "agent_run_ack")) == 2


def test_b5_main_usage_window_does_not_count_same_request_twice(repo_store, monkeypatch):
    _, store = repo_store
    runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
        runtime_config=RuntimeConfig(delegation_enabled=True), model_factory=known_factory)
    original = store.append_event
    def crash(kind, payload=None):
        result = original(kind, payload)
        if kind == "model_usage":
            raise SimulatedCrash()
        return result
    with monkeypatch.context() as patch:
        patch.setattr(store, "append_event", crash)
        with pytest.raises(SimulatedCrash):
            runner.run_turn("known fake")
    restarted = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
        runtime_config=RuntimeConfig(delegation_enabled=True), model_factory=known_factory)
    assert restarted.continue_turn().status == "completed"
    assert len(events(store, "model_request_reserved")) == len(events(store, "model_usage")) == 1




def test_b3_second_service_cannot_multiply_workers_in_same_scope(repo_store):
    _, store = repo_store
    with running_service(store) as (service, _):
        first = data(service.spawn(bg("owned")))
        other = service_for(store, factory=background_factory)
        assert other.spawn(bg("second-owner")).error_code == "session_busy"
        assert len(events(store, "agent_command_intent")) == 1
        assert not service.terminal(first["run_id"])


def test_b6_main_summary_commits_across_independent_child_notifications(repo_store, monkeypatch):
    """摘要来源只包含主历史；无关的历史子通知不使正在生成的摘要失效。"""
    from test_semantic_condensation import _append_tool_step
    _, store = repo_store
    store.append_event("user_message", {"message": "历史只读任务"})
    for i in range(6):
        _append_tool_step(store, i)
    store.append_event("assistant_message", {"message": "历史完成"})
    monkeypatch.setattr("codeagent.context.builder.HISTORY_MAX_EVENTS", 4)
    runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
        runtime_config=RuntimeConfig(delegation_enabled=True), model_factory=known_factory)
    condenser = runner._call_condenser
    crossed = []
    def notification_during_plan(plan, *args):
        event = store.append_event("agent_history", {"agent_id": "historical-child", "run_id": "historical-unrelated-run",
            "kind": "assistant_message", "data": {"message": "独立子历史追加"}})
        crossed.append((max(int(ref.split(":")[1]) for ref in plan.source_event_ids), event.seq))
        return condenser(plan, *args)
    monkeypatch.setattr(runner, "_call_condenser", notification_during_plan)
    output = runner.run_turn("继续当前任务")
    assert output.status == "completed", output
    assert crossed and crossed[0][0] < crossed[0][1]
    assert events(store, "context_condensed")
    assert not any(e.payload.get("failure_code") == "stale_frontier" for e in events(store, "context_condensation_attempt"))
    assert any(e.payload["purpose"] == "context_condenser" for e in events(store, "model_request_reserved"))

def test_b4_parent_finishes_after_child_ack_without_slice_boundary(repo_store):
    _, store = repo_store
    runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
        runtime_config=RuntimeConfig(delegation_enabled=True), model_factory=early_final_factory)
    out = runner.run_turn("normal-child")
    assert out.status == "completed", out
    assert len(events(store, "agent_run_ack")) == 1
    assert not events(store, "execution_slice_exhausted")
    assert events(store, "agent_run_ack")[0].seq < events(store, "assistant_message")[-1].seq
    assert not events(store, "agent_parent_closed")
    assert all(e.payload["status"] in {"completed", "cancelled"} for e in events(store, "agent_run_finished"))
    assert len(events(store, "user_message")) == 1


def test_b4_durable_stop_before_parent_terminal_does_not_restart_requests(repo_store):
    _, store = repo_store
    service = service_for(store, factory=background_factory)
    service.emit("agent_root_stopping", {"reason": "cancelled", "execution_epoch": "old-worker"})
    runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
        runtime_config=RuntimeConfig(delegation_enabled=True), model_factory=known_factory)
    output = runner.continue_turn()
    assert output.status == "stopped", output
    assert not events(store, "model_request_reserved")
    assert current_turn_state(store.read_events()).closed


def test_model_identity_variants_keep_valid_route_and_explicit_zero_paid_gate(repo_store):
    _, store = repo_store
    service = service_for(store, factory=background_factory, config=RuntimeConfig(delegation_enabled=True))
    service.routes["configured"] = ModelConfig(provider="glm", model="glm-5.3", reasoning_enabled=True, reasoning_effort="low")
    result = data(service.spawn({**spawn_args("no-paid-authorization"), "gateway": "configured"}))
    assert result["status"] == "completed"
    assert events(store, "model_request_reserved")




def test_b3_legacy_batch_backpressure_does_not_leave_partial_hidden_work(repo_store):
    _, store = repo_store
    with running_service(store) as (service, _):
        for i in range(3):
            data(service.spawn(bg("busy-" + str(i))))
        count = len(events(store, "agent_command_intent"))
        result = service.delegation_adapter.execute({"request_key": "legacy-full", "tasks": [
            {"goal": "read", "done_when": "README"} for _ in range(4)]})
        assert not result.ok and result.error_code == "queue_full"
        assert len(events(store, "agent_command_intent")) == count
        assert not events(store, "delegation_batch_started")


def test_b5_graph_session_owner_cannot_be_bypassed_with_legacy_backend(repo_store):
    _, store = repo_store
    # 即使 legacy Runner 在图 lease 文件出现前已构造，执行时也要重新检查执行权。
    legacy = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False), runtime_config=RuntimeConfig(delegation_enabled=True))
    with session_lease(store):
        before = store.read_meta()["event_seq"]
        def contend():
            with pytest.raises(RuntimeError, match="session_busy"):
                build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False), runtime_config=RuntimeConfig(delegation_enabled=True))
            with pytest.raises(RuntimeError, match="session_busy"):
                legacy.run_turn("must not append a new UserTurn")
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(contend).result()
        assert store.read_meta()["event_seq"] == before
        assert not events(store, "user_message")
