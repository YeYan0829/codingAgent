"""OpenHands任务入口的真实Runtime验收；模型为免费协议替身，不评估自主分工质量。"""
import json
import os
from pathlib import Path
import shlex
import sys
import time

import pytest

from codeagent.application.session_runtime import build_agent_runner
from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.model_gateway.base import LLMResponse, LLMToolCall, TokenUsage
from codeagent.model_gateway.fake import FakeLLM
from codeagent.runtime.approval import AutoApprovalGate, FakeApprovalGate
from codeagent.runtime.atomic_edit import AtomicEditService
from codeagent.runtime.policy import DefaultPolicy, PolicyDecision, PolicyResult
from codeagent.runtime.sandbox_executor import SandboxedCommandExecutor
from codeagent.tools.observations import build_observation_tool
from runtime_fixtures import execution_session, HostFixtureBackend


class TaskModel(FakeLLM):
    def complete(self, request):
        usage = TokenUsage(input_tokens=100, output_tokens=10, total_tokens=110)
        def respond(text=None, name=None, args=None):
            return LLMResponse(text=text, tool_calls=[LLMToolCall(call_id="action-" + str(len(observations)),
                name=name, arguments=args)] if name else [], finish_reason="tool_calls" if name else "stop", usage=usage)
        if request.purpose == "context_condenser":
            return super().complete(request)
        if request.purpose in {"worker_child", "readonly_child"}:
            assert "PARENT_ONLY" not in json.dumps(request.messages)
            index = max(i for i, m in enumerate(request.messages) if m["role"] == "user")
            case = json.loads(json.loads(request.messages[index]["content"])["goal"])
            observations = [json.loads(m["content"]) for m in request.messages[index + 1:] if m["role"] == "tool"]
            actions = case["actions"]
            if len(observations) < len(actions):
                action = actions[len(observations)]
                args = dict(action.get("arguments", {}))
                if args.get("source_ref") == "$command":
                    args["source_ref"] = observations[0]["metadata"]["source_ref"]
                return respond(name=action["tool"], args=args)
            return respond(text=json.dumps({"observations": observations,
                "tools": [t.name for t in request.tools], "purpose": request.purpose,
                "previous_tool_count": sum(m["role"] == "tool" for m in request.messages[:index])}))
        index = max(i for i, m in enumerate(request.messages) if m["role"] == "user" and '"task_case"' in m.get("content", ""))
        case = json.loads(request.messages[index]["content"])["task_case"]
        observations = [json.loads(m["content"]) for m in request.messages[index + 1:] if m["role"] == "tool"]
        if case.get("parent_plan") and not observations:
            return respond(name="update_task_plan", args={"tasks": [{"title": "parent plan"}]})
        offset = int(bool(case.get("parent_plan")))
        if len(observations) == offset:
            return respond(name="task", args=case["task"])
        if case.get("parent_plan") and len(observations) == offset + 1:
            return respond(name="read_task_plan", args={})
        return respond(text="task finished")


def task_factory(config):
    return TaskModel()


class TaskSDK:
    """截获最终GLM兼容请求；不创建网络客户端。"""
    def __init__(self):
        from types import SimpleNamespace
        self.chat = SimpleNamespace(completions=self)

    def with_options(self, **kwargs):
        assert kwargs == {"max_retries": 0}
        return self

    def create(self, **wire):
        from types import SimpleNamespace as Obj
        from codeagent.model_gateway.base import ModelRequest, ModelTool
        assert wire["max_tokens"] == 65536 and wire["tool_choice"] == "auto"
        assert wire["extra_body"]["thinking"] == {"type": "enabled", "clear_thinking": True}
        tools = [ModelTool(**t["function"]) for t in wire["tools"]]
        child = "\n本次角色：" in wire["messages"][0]["content"]
        if not child:
            task, = [t for t in tools if t.name == "task"]
            assert set(task.parameters["properties"]) == {"description", "prompt", "subagent_type", "resume"}
            assert 'subagent_type="bash-runner"' in task.description
        else:
            assert {t.name for t in tools} == {"run_command", "read_observation"}
        result = TaskModel().complete(ModelRequest(messages=wire["messages"], tools=tools,
            purpose="worker_child" if child else "main_agent"))
        calls = [Obj(id=c.call_id, function=Obj(name=c.name, arguments=json.dumps(c.arguments))) for c in result.tool_calls]
        return Obj(id="offline-task-sdk", choices=[Obj(finish_reason=result.finish_reason,
            message=Obj(content=result.text, reasoning_content=None, tool_calls=calls))],
            usage=Obj(prompt_tokens=100, completion_tokens=10, total_tokens=110))


def task_sdk_factory(config):
    from codeagent.model_gateway.glm_client import GLMClient
    return GLMClient(model=config.resolved_model, client=TaskSDK(), reasoning_enabled=True, reasoning_effort="high")


class ProtocolTaskModel(TaskModel):
    def complete(self, request):
        if request.purpose == "worker_child" and not any(m["role"] == "tool" for m in request.messages) and not any(
                "arguments 不是合法 JSON" in m.get("content", "") for m in request.messages):
            from codeagent.model_gateway.base import MalformedToolArgumentsError
            raise MalformedToolArgumentsError("run_command", "unfinished string", 1, 12, '{"command":',
                usage=TokenUsage(input_tokens=100, output_tokens=10, total_tokens=110),
                provider_request_id="offline-malformed-worker", finish_reason="tool_calls")
        return super().complete(request)


def protocol_task_factory(config):
    return ProtocolTaskModel()


def runner_for(tmp_path, *, approval=None, policy=None, execute=True):
    root, store, context = execution_session(tmp_path)
    runner = build_agent_runner(store, model_config=ModelConfig(provider="glm", model="glm-5.3", reasoning_enabled=True),
        runtime_config=RuntimeConfig(delegation_enabled=True),
        approval_gate=approval or AutoApprovalGate(True), model_factory=task_factory,
        execution_allowed=execute)
    if runner.command_service:
        runner.command_service.executor = SandboxedCommandExecutor(HostFixtureBackend())
    if policy:
        runner.policy = policy
    return root, store, runner


def run(runner, actions, *, role="general-purpose", resume=None, parent_plan=False):
    task = {"description": "fixture subtask", "prompt": json.dumps({"actions": actions})}
    if resume:
        task["resume"] = resume
    else:
        task["subagent_type"] = role
    output = runner.run_turn(json.dumps({"task_case": {"task": task, "parent_plan": parent_plan}, "private": "PARENT_ONLY"}))
    assert output.status == "completed", output
    step = next(s for s in output.steps if s["tool"] == "task")
    return step["result"], output


def payload(result):
    assert result["ok"], result
    meta = json.loads(result["content"])
    return meta, json.loads(meta["text"])


class SequentialTasksModel(TaskModel):
    def complete(self, request):
        if request.purpose != "main_agent":
            return super().complete(request)
        names = {t.name for t in request.tools}
        assert names.intersection({"task", "spawn_agent", "send_agent", "wait_agents",
            "cancel_agents", "close_agent", "delegate_readonly"}) == {"task"}
        assert "spawn_agent" not in request.messages[0]["content"]
        observations = [json.loads(m["content"]) for m in request.messages if m["role"] == "tool"]
        if not observations:
            def edit(old, new):
                return {"path": "sort_utils.py", "edits": [{"old_text": old, "new_text": new}]}
            calls = []
            for ident, old, new in (("first", "reverse=True", "reverse=False"),
                                    ("second", "reverse=0", "reverse=1")):
                calls.append(LLMToolCall(call_id=ident, name="task", arguments={
                    "subagent_type": "general-purpose", "prompt": json.dumps({"actions": [
                        {"tool": "edit_file", "arguments": edit(old, new)}]})}))
            calls.insert(1, LLMToolCall(call_id="parent-edit", name="edit_file",
                arguments=edit("reverse=False", "reverse=0")))
            return LLMResponse(tool_calls=calls, finish_reason="tool_calls")
        assert len(observations) == 3 and all(r["ok"] for r in observations)
        for result in (observations[0], observations[2]):
            _, report = payload(result)
            assert report["observations"][0]["ok"]
        return LLMResponse(text="sequential tasks completed", finish_reason="stop")


def sequential_tasks_factory(config):
    return SequentialTasksModel()


def test_task_batch_is_sequential_and_parent_resumes_after_children(tmp_path):
    """同批两个可写task与父编辑必须顺序生效，下一次父请求看到完整结果。"""
    root, store, runner = runner_for(tmp_path)
    runner.model_factory = sequential_tasks_factory
    output = runner.run_turn("依次执行两个子任务，中间由主方修改同一文件。")
    assert output.status == "completed", output
    assert [s["tool"] for s in output.steps] == ["task", "edit_file", "task"]
    assert "reverse=1" in (root / "sort_utils.py").read_text()
    records = store.read_events()
    timeline = [e.type for e in records if e.type in {"agent_run_running", "agent_run_ack"}]
    assert timeline == ["agent_run_running", "agent_run_ack", "agent_run_running", "agent_run_ack"]
    acks = [e.seq for e in records if e.type == "agent_run_ack"]
    parents = [e.seq for e in records if e.type == "model_request_reserved" and e.payload["purpose"] == "main_agent"]
    assert len(parents) == 2 and parents[0] < acks[0] < acks[1] < parents[1]


class NoTaskModel(FakeLLM):
    def complete(self, request):
        return LLMResponse(text="current workspace inspected", finish_reason="stop")


def no_task_factory(config):
    return NoTaskModel()


def test_resume_old_background_run_does_not_restart_worker(tmp_path):
    from codeagent.orchestration.agents import AgentSessions, SpawnCommand
    from codeagent.orchestration.lease import session_lease
    from codeagent.runtime.delegation import DelegationService
    from codeagent.runtime.request_executor import RequestExecutor
    root, store, runner = runner_for(tmp_path)
    before = (root / "sort_utils.py").read_bytes()
    store.append_event("user_message", {"message": "inspect project"})
    executor = RequestExecutor(store, runner.config, runner.model_config, no_task_factory, lambda: False)
    legacy = DelegationService(store, executor, store.workspace_context, parent_runtime=runner)
    sessions = AgentSessions(store, executor, legacy)
    with session_lease(store) as epoch:
        old = sessions._submit("spawn", SpawnCommand(spec="general-purpose", execution="background",
            goal="旧后台实现任务"), epoch)
    runner.model_factory = no_task_factory
    output = runner.continue_turn()
    assert output.status == "completed", output
    rid = old["run"]["run_id"]
    records = store.read_events()
    report, = [e for e in records if e.type == "agent_run_finished" and e.payload["run_id"] == rid]
    assert report.payload["status"] == "interrupted"
    assert any(e.type == "agent_run_ack" and e.payload["run_id"] == rid for e in records)
    assert not any(e.type == "agent_run_running" for e in records)
    assert not any(e.type == "model_request_reserved" and e.payload["purpose"] != "main_agent" for e in records)
    assert (root / "sort_utils.py").read_bytes() == before


def test_general_task_edits_with_undo_and_resumes_private_history(tmp_path):
    root, store, runner = runner_for(tmp_path)
    result, output = run(runner, [
        {"tool": "edit_file", "arguments": {"path": "sort_utils.py", "edits": [{"old_text": "reverse=True", "new_text": "reverse=False"}]}},
        {"tool": "update_task_plan", "arguments": {"tasks": [{"title": "child plan", "status": "done"}]}},
        {"tool": "read_task_plan"}], parent_plan=True)
    meta, report = payload(result)
    assert "text" not in result["metadata"]  # 正文只交付一次，不在metadata和后台观察中重复。
    assert not any(e.type == "child_report_observation" for e in store.read_events())
    assert report["purpose"] == "worker_child"
    assert all(x["ok"] for x in report["observations"])
    assert "reverse=False" in (root / "sort_utils.py").read_text()
    assert "child plan" in report["observations"][-1]["content"]
    parent = next(s for s in output.steps if s["tool"] == "read_task_plan")
    assert "parent plan" in parent["result"]["content"] and "child plan" not in parent["result"]["content"]
    edits = [e for e in store.read_events() if e.type == "edit_transaction"]
    assert len(edits) == 1 and edits[0].payload["agent_id"] == meta["task_id"]
    again, _ = run(runner, [{"tool": "read_file", "arguments": {"path": "sort_utils.py"}}], resume=meta["task_id"])
    resumed, body = payload(again)
    assert resumed["task_id"] == meta["task_id"] and resumed["run_id"] != meta["run_id"]
    assert body["previous_tool_count"] == 3
    assert "reverse=False" in body["observations"][0]["content"]
    assert AtomicEditService(store.workspace_context(), store).undo().ok
    assert "reverse=True" in (root / "sort_utils.py").read_text()


def test_command_worker_preserves_long_streams_nonzero_status_and_ownership(tmp_path):
    _, store, runner = runner_for(tmp_path)
    command = f'''{sys.executable} -c 'import sys; print("x"*12000); print("y"*10000,file=sys.stderr); sys.exit(7)' '''
    result, _ = run(runner, [
        {"tool": "run_command", "arguments": {"command": command}},
        {"tool": "read_observation", "arguments": {"source_ref": "$command", "stream": "stdout", "max_bytes": 20000}},
        {"tool": "read_observation", "arguments": {"source_ref": "$command", "stream": "stderr", "max_bytes": 20000}}], role="bash-runner")
    meta, body = payload(result)
    command, stdout, stderr = body["observations"]
    assert not command["ok"] and command["metadata"]["exit_code"] == 7
    assert stdout["content"] == "x" * 12000 + "\n"
    assert stderr["content"] == "y" * 10000 + "\n"
    assert set(body["tools"]) == {"run_command", "read_observation"}
    events = [e for e in store.read_events() if e.type == "command_completed"]
    assert len(events) == 1 and events[0].payload["agent_id"] == meta["task_id"]
    other = build_observation_tool(store, agent_id="agent-other").handler
    for ref in (command["metadata"]["source_ref"], "seq:" + str(events[0].seq)):
        assert not other({"source_ref": ref, "stream": "stdout"}).ok
    own = build_observation_tool(store, agent_id=meta["task_id"]).handler
    assert own({"source_ref": "seq:" + str(events[0].seq), "stream": "stderr"}).ok


def test_parent_and_workers_reuse_one_stateful_swe_container(tmp_path):
    from codeagent.benchmark.swebench_executor import SWEbenchDockerCommandExecutor
    from test_swebench_docker_executor import prepared, FakeDockerExecutionBackend
    root, store, runner = runner_for(tmp_path)
    backend = FakeDockerExecutionBackend(root)
    executor = SWEbenchDockerCommandExecutor(prepared(store.workspace_context()), backend, import_probe=lambda *_: None)
    runner.command_service.executor = executor
    try:
        first, _ = run(runner, [{"tool": "run_command", "arguments": {"command": "first child"}}], role="bash-runner")
        assert payload(first)[1]["observations"][0]["ok"]
        assert executor.container_id == "container-456"
        assert runner.command_service.run_command({"command": "parent command"}).ok
        second, _ = run(runner, [{"tool": "run_command", "arguments": {"command": "second child"}}], role="bash-runner")
        assert payload(second)[1]["observations"][0]["ok"]
        assert len(backend.created) == len(backend.started) == 1
        assert len(backend.executions) == 3
    finally:
        executor.close()
    assert backend.removed == ["container-456"]


def test_explorer_cannot_write_execute_or_recurse(tmp_path):
    root, store, runner = runner_for(tmp_path)
    result, _ = run(runner, [
        {"tool": "run_command", "arguments": {"command": "touch ESCAPED"}},
        {"tool": "apply_workspace_edit", "arguments": {"operations": [{"path": "ESCAPED", "content": "bad"}]}},
        {"tool": "task", "arguments": {"prompt": "recurse"}},
        {"tool": "git_status"}], role="code-explorer")
    _, body = payload(result)
    assert all(o["error_code"] == "policy_denied" for o in body["observations"][:3])
    assert body["observations"][-1]["ok"]
    assert not (root / "ESCAPED").exists()
    assert not any(e.type == "command_requested" for e in store.read_events())
    assert "task" not in body["tools"] and "edit_file" not in body["tools"]


def test_worker_network_permission_uses_parent_approval_and_rejects_before_command(tmp_path):
    gate = FakeApprovalGate(False)
    root, store, runner = runner_for(tmp_path, approval=gate)
    result, _ = run(runner, [{"tool": "run_command", "arguments": {"command": "touch ESCAPED",
        "permissions": [{"capability": "network", "reason": "test external access", "scope": "once"}]}}], role="bash-runner")
    _, body = payload(result)
    assert not body["observations"][0]["ok"]
    assert len(gate.permission_requests) == 1
    assert not (root / "ESCAPED").exists()
    assert not any(e.type == "command_completed" for e in store.read_events())


class DenyCommandPolicy(DefaultPolicy):
    def evaluate(self, tool):
        return PolicyResult(PolicyDecision.DENY, "fixture parent command denial") if tool.name == "run_command" else super().evaluate(tool)


def test_parent_policy_and_registry_are_both_enforced(tmp_path):
    root, store, runner = runner_for(tmp_path, policy=DenyCommandPolicy())
    result, _ = run(runner, [{"tool": "run_command", "arguments": {"command": "touch ESCAPED"}}], role="bash-runner")
    _, body = payload(result)
    assert "run_command" not in body["tools"]
    assert body["observations"][0]["error_code"] == "policy_denied"
    assert not (root / "ESCAPED").exists()
    assert not any(e.type == "command_requested" for e in store.read_events())


def test_worker_cannot_acquire_edit_tool_missing_from_parent_registry(tmp_path):
    from codeagent.application.session_runtime import build_registry_for_context
    from codeagent.runtime.runner import AgentRunner
    from codeagent.tools.registry import ToolRegistry
    root, store, context = execution_session(tmp_path)
    allowed = {t.name for t in build_registry_for_context(context).list_tools()} | {
        "run_command", "read_observation", "read_task_plan", "update_task_plan", "task"}
    registry = ToolRegistry(context, allowed_names=frozenset(allowed))
    for tool in build_registry_for_context(context).list_tools():
        registry.register(tool)
    runner = AgentRunner(store, TaskModel(), registry, AutoApprovalGate(True),
        model_config=ModelConfig(provider="glm", model="glm-5.3", reasoning_enabled=True),
        config=RuntimeConfig(delegation_enabled=True), model_factory=task_factory)
    runner.command_service.executor = SandboxedCommandExecutor(HostFixtureBackend())
    result, _ = run(runner, [{"tool": "edit_file", "arguments": {"path": "sort_utils.py",
        "edits": [{"old_text": "reverse=True", "new_text": "reverse=False"}]}}])
    names = {t.name for t in runner.tools.list_tools()}
    assert "task" in names and "spawn_agent" not in names
    _, body = payload(result)
    assert "edit_file" not in body["tools"] and "apply_workspace_edit" not in body["tools"]
    assert body["observations"][0]["error_code"] == "policy_denied"
    assert "reverse=True" in (root / "sort_utils.py").read_text()
    assert not any(e.type == "edit_transaction" for e in store.read_events())


def test_task_validation_and_unavailable_execution_do_not_dispatch_child(tmp_path):
    _, store, runner = runner_for(tmp_path, execute=False)
    result, _ = run(runner, [], role="bash-runner")
    assert not result["ok"] and result["error_code"] == "agent_execution_unavailable"
    task = runner.tools.get("task")
    assert set(task.schema["properties"]) == {"description", "prompt", "subagent_type", "resume"}
    frontier = len(store.read_events())
    for arguments in ({"prompt": "x", "max_turns": 1}, {"prompt": " "}, {"prompt": "x", "resume": ""},
                      {"prompt": "x", "resume": "other-session"}):
        assert not task.handler(arguments).ok
    assert len(store.read_events()) == frontier
    assert not any(e.type == "model_request_reserved" and e.payload["agent_id"] != "main" for e in store.read_events())


def test_final_sdk_task_schema_example_and_worker_command(tmp_path):
    _, store, runner = runner_for(tmp_path)
    runner.model_factory = task_sdk_factory
    result, _ = run(runner, [{"tool": "run_command", "arguments": {"command": "printf task-sdk-ok"}}], role="bash-runner")
    _, report = payload(result)
    assert "task-sdk-ok" in report["observations"][0]["content"]
    assert {e.payload["purpose"] for e in store.read_events() if e.type == "model_request_reserved"} == {"main_agent", "worker_child"}


def test_worker_known_protocol_failure_repairs_without_partial_execution(tmp_path):
    _, store, runner = runner_for(tmp_path)
    runner.model_factory = protocol_task_factory
    result, _ = run(runner, [{"tool": "run_command", "arguments": {"command": "printf repaired-worker"}}], role="bash-runner")
    _, body = payload(result)
    assert body["observations"][0]["ok"]
    events = store.read_events()
    assert sum(e.type == "command_requested" for e in events) == 1
    assert sum(e.type == "agent_history" and e.payload["kind"] == "model_protocol_error" for e in events) == 1
    failures = [e for e in events if e.type == "model_request_settled" and e.payload["outcome"] == "protocol_error"]
    assert len(failures) == 1 and failures[0].payload["usage"] is not None
    assert sum(e.type == "model_response" for e in events) == sum(e.type == "model_request_reserved" for e in events)


class AskReadPolicy(DefaultPolicy):
    def evaluate(self, tool):
        return PolicyResult(PolicyDecision.ASK, "fixture read approval") if tool.name == "read_file" else super().evaluate(tool)


def test_child_tool_approval_is_forwarded_and_scoped(tmp_path):
    _, store, runner = runner_for(tmp_path, policy=AskReadPolicy(), approval=AutoApprovalGate(False))
    result, _ = run(runner, [{"tool": "read_file", "arguments": {"path": "sort_utils.py"}}], role="code-explorer")
    meta, report = payload(result)
    assert report["observations"][0]["error_code"] == "approval_denied"
    decisions = [e for e in store.read_events() if e.type == "approval_decision"]
    assert len(decisions) == 1 and not decisions[0].payload["allowed"]
    assert decisions[0].payload["agent_id"] == meta["task_id"]


@pytest.mark.parametrize("tool,arguments", [
    ("run_command", {"command": "touch ESCAPED"}),
    ("edit_file", {"path": "sort_utils.py", "edits": [{"old_text": "reverse=True", "new_text": "reverse=False"}]})])
def test_unknown_child_side_effect_is_not_replayed(tmp_path, tool, arguments):
    from codeagent.orchestration.agents import AgentSessions, ChildAdapter, SpawnCommand
    from codeagent.orchestration.lease import session_lease
    from codeagent.runtime.delegation import DelegationService
    from codeagent.runtime.execution_service import UnsafeRecovery
    from codeagent.runtime.request_executor import RequestExecutor
    root, store, runner = runner_for(tmp_path)
    store.append_event("user_message", {"message": "unknown effect fixture"})
    requests = RequestExecutor(store, runner.config, runner.model_config, task_factory, lambda: False)
    service = AgentSessions(store, requests, DelegationService(store, requests, store.workspace_context, parent_runtime=runner))
    with session_lease(store) as epoch:
        command = service._submit("spawn", SpawnCommand(spec="general-purpose", goal="inspect recovery", request_key="unknown"), epoch)
        adapter = ChildAdapter(service, requests, command, root, epoch)
        call = LLMToolCall(call_id="unknown", name=tool, arguments=arguments)
        key = adapter.run_id + ":tool:0:0"
        service.emit("tool_execution_intent", {"operation_key": key, "call": call.model_dump(), "readonly": False})
        with pytest.raises(UnsafeRecovery):
            adapter.execute(call, key)
        service.drain(epoch, cancel=True)
    assert not (root / "ESCAPED").exists()
    assert "reverse=True" in (root / "sort_utils.py").read_text()
    assert store.workspace_state().value == "recovery_required"
    assert not any(e.type in {"command_requested", "edit_transaction"} for e in store.read_events())


def real_executor():
    from codeagent.runtime.bubblewrap import BubblewrapBackend
    executable = os.environ.get("CODEAGENT_TEST_BWRAP")
    if not executable:
        pytest.skip("设置CODEAGENT_TEST_BWRAP运行子Agent真实Bubblewrap验收")
    return SandboxedCommandExecutor(BubblewrapBackend(Path(executable)))


def test_real_child_edit_test_and_sandbox_boundaries(tmp_path):
    executor = real_executor()
    root, store, runner = runner_for(tmp_path)
    runner.command_service.executor = executor
    result, _ = run(runner, [
        {"tool": "edit_file", "arguments": {"path": "sort_utils.py", "edits": [{"old_text": "reverse=True", "new_text": "reverse=False"}]}},
        {"tool": "run_command", "arguments": {"command": shlex.quote(sys.executable) + " -m pytest -p no:cacheprovider test_sort_utils.py -q", "purpose": "validation"}},
        {"tool": "run_command", "arguments": {"command": "awk 'NR > 1 { exit 1 }' /proc/net/route"}},
        {"tool": "run_command", "arguments": {"command": "printf hacked > .git/HEAD"}}])
    _, body = payload(result)
    edit, check, network, git_write = body["observations"]
    assert edit["ok"] and check["ok"] and "1 passed" in check["content"]
    assert network["ok"] and not git_write["ok"]
    assert any(e.type == "command_completed" and e.payload["backend"] == "bubblewrap" for e in store.read_events())
    assert (root / ".git/HEAD").read_text().startswith("ref:")
    assert AtomicEditService(store.workspace_context(), store).undo().ok


def test_real_background_worker_cancel_stops_command_before_ack(tmp_path):
    from codeagent.orchestration.lease import session_lease
    executor = real_executor()
    root, store, runner = runner_for(tmp_path)
    runner.command_service.executor = executor
    # 先创建共享会话服务；未发真实模型请求，factory始终是TaskModel。
    run(runner, [], role="code-explorer")
    store.append_event("user_message", {"message": "cancel fixture"})
    from codeagent.runtime.request_executor import RequestExecutor
    from codeagent.runtime.delegation import DelegationService
    from codeagent.orchestration.agents import AgentSessions
    requests = RequestExecutor(store, runner.config, runner.model_config, task_factory, lambda: False)
    legacy = DelegationService(store, requests, store.workspace_context, parent_runtime=runner)
    service = AgentSessions(store, requests, legacy)
    with session_lease(store):
        first = service.spawn({"spec": "bash-runner", "execution": "background", "goal": json.dumps({"actions": [
            {"tool": "run_command", "arguments": {"command": "touch started; sleep 30; touch LATE", "timeout_seconds": 60}}]})})
        assert first.ok
        receipt = json.loads(first.content)
        deadline = time.monotonic() + 15
        while not (root / "started").exists():
            assert time.monotonic() < deadline
            time.sleep(.02)
        assert service.cancel({"run_ids": [receipt["run_id"]]}).ok
        waited = service.wait({"run_ids": [receipt["run_id"]], "timeout": 10.0})
        report = json.loads(waited.content)["reports"][0]
        assert report["status"] == "cancelled" and report["terminal_ack"]
        service.drain(service.scheduler.epoch)
    commands = [e for e in store.read_events() if e.type == "command_completed"]
    ack = next(e for e in store.read_events() if e.type == "agent_run_ack" and e.payload["run_id"] == receipt["run_id"])
    assert len(commands) == 1 and commands[0].seq < ack.seq
    assert commands[0].payload["status"] == "execution_cancelled" and commands[0].payload["process_tree_stopped"]
    assert not (root / "LATE").exists()
