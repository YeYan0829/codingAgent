from pathlib import Path
import sys

import pytest

from codeagent.runtime.approval import FakeApprovalGate
from codeagent.runtime.command import ApprovalDecision, CommandResult, FakeCommandExecutor
from codeagent.runtime.command_service import CommandService
from codeagent.runtime.policy import CommandPolicy, PolicyDecision
from codeagent.session.store import SessionStore
from codeagent.workspace.workspace import WorkspaceContext
from codeagent.model_gateway.base import BaseModelClient, LLMResponse, LLMToolCall, ModelRequest
from codeagent.runtime.runner import AgentRunner
from codeagent.tools.registry import ToolRegistry


def make_service(tmp_path, *, approval=ApprovalDecision.APPROVE_ONCE, result=None):
    source = tmp_path / "source"
    active = tmp_path / "sessions" / "worktrees" / "task"
    source.mkdir()
    active.mkdir(parents=True)
    context = WorkspaceContext(source, active, "git_worktree", "abc", "task")
    store = SessionStore(source, session_root=tmp_path / "session-store").create(workspace_context=context)
    gate = FakeApprovalGate(approval)
    executor = FakeCommandExecutor(result)
    service = CommandService(context, CommandPolicy(), gate, executor, store)
    return service, store, gate, executor, context


def request(**overrides):
    data = {"kind": "pytest", "targets": ["tests/test_one.py"], "cwd": ".", "timeout_seconds": 60}
    data.update(overrides)
    return data


def test_valid_pytest_requires_approval(tmp_path):
    service, _, _, _, context = make_service(tmp_path)
    spec = service._build_spec(request())
    assert CommandPolicy().evaluate(spec, context).decision == PolicyDecision.REQUIRE_APPROVAL


def test_approved_request_calls_fake_executor_with_same_fixed_spec(tmp_path):
    service, _, gate, executor, _ = make_service(tmp_path)
    result = service.run_check(request())
    assert result.ok
    assert len(executor.calls) == 1
    assert gate.command_requests[0] is executor.calls[0]
    assert executor.calls[0].argv == (sys.executable, "-m", "pytest", "tests/test_one.py")


def test_denied_request_does_not_call_executor(tmp_path):
    service, _, gate, executor, _ = make_service(tmp_path, approval=ApprovalDecision.DENY)
    result = service.run_check(request())
    assert not result.ok
    assert len(gate.command_requests) == 1
    assert executor.calls == []


def test_invalid_command_kind_is_denied_before_approval(tmp_path):
    service, _, gate, executor, _ = make_service(tmp_path)
    result = service.run_check(request(kind="ruff"))
    assert not result.ok
    assert gate.command_requests == []
    assert executor.calls == []


def test_parent_cwd_is_denied(tmp_path):
    service, _, gate, _, _ = make_service(tmp_path)
    result = service.run_check(request(cwd="../outside"))
    assert not result.ok
    assert gate.command_requests == []


@pytest.mark.parametrize("target", ["../outside.py", str(Path("C:/absolute/test.py"))])
def test_absolute_or_outside_target_is_denied(tmp_path, target):
    service, _, gate, _, _ = make_service(tmp_path)
    result = service.run_check(request(targets=[target]))
    assert not result.ok
    assert gate.command_requests == []


def test_timeout_over_limit_is_denied(tmp_path):
    service, _, gate, _, _ = make_service(tmp_path)
    result = service.run_check(request(timeout_seconds=301))
    assert not result.ok
    assert gate.command_requests == []


def test_command_receipt_is_written_to_session(tmp_path):
    service, store, _, executor, _ = make_service(tmp_path)
    service.run_check(request())
    receipt = next(event for event in store.read_events() if event.type == "command_receipt")
    assert receipt.payload["command_spec"]["command_id"] == executor.calls[0].command_id
    assert receipt.payload["status"] == "completed"
    assert receipt.payload["policy_decision"] == "require_approval"
    assert receipt.payload["approval_decision"] == "approve_once"
    assert receipt.payload["result"]["exit_code"] == 0


def test_tool_result_contains_exit_code_and_compact_streams(tmp_path):
    fake_result = CommandResult(exit_code=2, stdout="short stdout", stderr="short stderr")
    service, _, _, _, _ = make_service(tmp_path, result=fake_result)
    result = service.run_check(request())
    assert not result.ok
    assert result.metadata["exit_code"] == 2
    assert "exit_code: 2" in result.content
    assert "short stdout" in result.content
    assert "short stderr" in result.content


def test_source_workspace_is_not_executable(tmp_path):
    service, _, gate, _, context = make_service(tmp_path)
    service.context = WorkspaceContext.source(context.source_root)
    result = service.run_check(request())
    assert not result.ok
    assert gate.command_requests == []


class RunCheckModel(BaseModelClient):
    def __init__(self):
        self.calls = 0

    def complete(self, request: ModelRequest) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            assert any(tool.name == "run_check" for tool in request.tools)
            return LLMResponse(tool_calls=[LLMToolCall(call_id="check-1", name="run_check", arguments=request_args())])
        return LLMResponse(text="done")


def request_args():
    return request()


def test_runner_dependency_injection_exposes_complete_run_check_chain(tmp_path):
    service, store, gate, executor, context = make_service(tmp_path)
    registry = ToolRegistry(workspace_context=context)
    runner = AgentRunner(
        store,
        RunCheckModel(),
        registry,
        gate,
        workspace_context=context,
        command_executor=executor,
    )
    output = runner.run_turn("run tests")
    assert output.steps[0]["tool"] == "run_check"
    assert output.steps[0]["result"]["metadata"]["exit_code"] == 0
    assert len(executor.calls) == 1
    assert any(event.type == "command_receipt" for event in store.read_events())
