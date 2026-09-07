from pathlib import Path
from typer.testing import CliRunner

from codeagent.runtime.approval import AutoApprovalGate, FakeApprovalGate
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.command import CommandExecutionStatus, CommandResult
from codeagent.runtime.command_service import CommandService
from codeagent.runtime.workspace_audit import WorkspaceAuditError
from codeagent.runtime.validation import current_validation_evidence
from codeagent.cli import app
from codeagent.tools.command import command_tool_schema
from codeagent.session.store import SessionStore
from test_candidate_loop import execution_session


class FakeSandboxExecutor:
    def __init__(self, mutate=None, result=None):
        self.mutate = mutate
        self.result = result
        self.calls = []

    def execute(self, request, context, artifacts):
        self.calls.append(request)
        if self.mutate:
            self.mutate(context.active_root)
        return self.result or CommandResult(
            exit_code=0, status=CommandExecutionStatus.COMPLETED,
            stdout="ok", payload_started=True, process_tree_stopped=True,
            environment_names=("HOME", "PATH"), environment_contract_revision="command-environment-v1",
            backend="fixture", backend_availability="available", launch_cwd=str(context.active_root / request.command.cwd),
            effective_network_policy=request.policy.network_mode.value,
            effective_filesystem_policy={"policy_revision": request.policy.policy_revision},
            effective_policy=request.policy.summary(),
        )


def service(store, context, executor, approval=None):
    return CommandService(context, approval or AutoApprovalGate(True), executor, store, CommandArtifactStore(store))


def test_run_command_schema_is_closed_and_permission_branches_are_explicit():
    schema = command_tool_schema()
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["command"]
    permission = schema["properties"]["permissions"]["items"]["oneOf"]
    assert permission[0]["additionalProperties"] is False
    assert permission[1]["properties"]["capability"]["const"] == "network"
    assert "resource" not in permission[1]["properties"]


def test_invalid_cwd_and_unknown_fields_are_rejected_before_spawn(tmp_path):
    _, store, context = execution_session(tmp_path)
    executor = FakeSandboxExecutor()
    commands = service(store, context, executor)
    escaped = commands.run_command({"command": "true", "cwd": ".."})
    unknown = commands.run_command({"command": "true", "argv": []})
    assert not escaped.ok and not unknown.ok
    assert escaped.error_code == "invalid_arguments" and "workspace" in escaped.error.lower()
    assert "未知字段" in unknown.error
    assert executor.calls == []


def test_command_change_is_legal_candidate_change_and_advances_revision(tmp_path):
    _, store, context = execution_session(tmp_path)
    executor = FakeSandboxExecutor(lambda root: (root / "generated.py").write_text("VALUE = 1\n"))
    result = service(store, context, executor).run_command({"command": "generate", "purpose": "utility"})
    assert result.ok
    assert store.workspace_state().value == "changes_active"
    assert result.metadata["after_candidate_revision"] == 1
    assert result.metadata["command_induced_changes"] == [{"path": "generated.py", "kind": "create"}]
    shown = CliRunner().invoke(app, ["show-changes", store.session_id, "--session-root", str(store.session_root)])
    assert shown.exit_code == 0
    assert "generated.py" in shown.output and "VALUE = 1" in shown.output


def test_failed_payload_changes_are_still_legal_and_not_replayed(tmp_path):
    _, store, context = execution_session(tmp_path)
    result_value = CommandResult(exit_code=2, status=CommandExecutionStatus.COMPLETED, payload_started=True, process_tree_stopped=True)
    executor = FakeSandboxExecutor(lambda root: (root / "lock.file").write_text("partial\n"), result_value)
    result = service(store, context, executor).run_command({"command": "failing-tool"})
    assert not result.ok and len(executor.calls) == 1
    assert store.workspace_state().value == "changes_active"
    assert (context.active_root / "lock.file").exists()
    assert store.read_meta()["candidate_revision"] == 1


def test_validation_evidence_binds_after_revision(tmp_path):
    _, store, context = execution_session(tmp_path)
    result = service(store, context, FakeSandboxExecutor()).run_command({"command": "true", "purpose": "validation"})
    assert result.ok
    evidence = [event.payload for event in store.read_events() if event.type == "validation_completed"][-1]
    assert evidence["candidate_revision"] == 0
    assert evidence["command"] == "true"


def test_command_result_exposes_bounded_output_tails_for_context(tmp_path):
    _, store, context = execution_session(tmp_path)
    value = CommandResult(exit_code=2, status=CommandExecutionStatus.COMPLETED,
                          stdout="\n".join(f"pass-{i}" for i in range(30)),
                          stderr="missing interpreter", payload_started=True, process_tree_stopped=True)
    result = service(store, context, FakeSandboxExecutor(result=value)).run_command({"command": "check"})
    assert result.metadata["status"] == "completed"
    assert result.metadata["exit_code"] == 2
    assert result.metadata["stdout_tail"].startswith("pass-10")
    assert result.metadata["stderr_tail"] == "missing interpreter"


def test_command_result_exposes_active_workspace_and_warns_on_baseline_reference(tmp_path):
    _, store, context = execution_session(tmp_path)
    command = f"cd {context.source_root} && pwd"

    result = service(store, context, FakeSandboxExecutor()).run_command({"command": command})

    assert result.ok
    assert result.metadata["active_workspace"] == str(context.active_root)
    assert result.metadata["baseline_workspace"] == str(context.source_root)
    assert result.metadata["initial_cwd"] == str(context.active_root)
    assert "不包含当前 Candidate 修改" in result.metadata["workspace_warning"]
    assert "workspace_warning:" in result.content


def test_nonzero_completed_command_has_specific_error_code(tmp_path):
    _, store, context = execution_session(tmp_path)
    value = CommandResult(exit_code=2, status=CommandExecutionStatus.COMPLETED,
                          payload_started=True, process_tree_stopped=True)

    result = service(store, context, FakeSandboxExecutor(result=value)).run_command({"command": "false"})

    assert not result.ok
    assert result.error_code == "command_exit_nonzero"


def test_external_write_asks_and_session_grant_persists(tmp_path):
    _, store, context = execution_session(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    gate = FakeApprovalGate()
    gate.allow = True
    command = {"command": "write", "permissions": [{
        "capability": "filesystem_write", "resource": str(external),
        "reason": "cache", "scope": "session",
    }]}
    result = service(store, context, FakeSandboxExecutor(), gate).run_command(command)
    assert result.ok and len(gate.permission_requests) == 1
    assert store.permission_grants()[0].resolved_resource == external


def test_network_once_records_effective_environment_without_persisting_grant(tmp_path):
    _, store, context = execution_session(tmp_path)
    gate = FakeApprovalGate(True)
    result = service(store, context, FakeSandboxExecutor(), gate).run_command({
        "command": "network-client", "permissions": [{
            "capability": "network", "reason": "fetch fixture", "scope": "once",
        }],
    })

    completed = [event.payload for event in store.read_events() if event.type == "command_completed"][-1]
    assert result.ok and result.metadata["effective_network_policy"] == "host"
    assert result.metadata["environment_contract_revision"] == "command-environment-v1"
    assert completed["effective_network_policy"] == "host"
    assert completed["environment_names"] == ["HOME", "PATH"]
    assert store.permission_grants() == ()


def test_network_reject_never_executes_or_creates_grant(tmp_path):
    _, store, context = execution_session(tmp_path)
    executor = FakeSandboxExecutor()
    gate = FakeApprovalGate(False)

    result = service(store, context, executor, gate).run_command({
        "command": "network-client", "permissions": [{
            "capability": "network", "reason": "fetch fixture", "scope": "once",
        }],
    })

    assert not result.ok and result.error_code == "approval_denied"
    assert executor.calls == [] and store.permission_grants() == ()
    assert not any(event.type == "command_completed" for event in store.read_events())


def test_network_session_grant_changes_default_snapshot(tmp_path):
    _, store, context = execution_session(tmp_path)
    commands = service(store, context, FakeSandboxExecutor(), FakeApprovalGate(True))
    result = commands.run_command({
        "command": "network-client", "permissions": [{
            "capability": "network", "reason": "session service", "scope": "session",
        }],
    })

    assert result.ok
    assert commands.environment_snapshot()["network_mode"] == "host"


def test_process_tree_uncertainty_enters_recovery_required(tmp_path):
    _, store, context = execution_session(tmp_path)
    value = CommandResult(exit_code=None, status=CommandExecutionStatus.TIMED_OUT, timed_out=True,
                          payload_started=True, process_tree_stopped=False)
    result = service(store, context, FakeSandboxExecutor(result=value)).run_command({"command": "fork"})
    assert not result.ok
    assert store.workspace_state().value == "recovery_required"


def test_timeout_changes_remain_legal_when_process_tree_and_audit_are_complete(tmp_path):
    _, store, context = execution_session(tmp_path)
    value = CommandResult(exit_code=-15, status=CommandExecutionStatus.TIMED_OUT, timed_out=True,
                          payload_started=True, process_tree_stopped=True)
    executor = FakeSandboxExecutor(lambda root: (root / "timeout.lock").write_text("partial\n"), value)
    result = service(store, context, executor).run_command({"command": "slow-generator"})
    assert not result.ok
    assert store.workspace_state().value == "changes_active"
    assert result.metadata["command_induced_changes"] == [{"path": "timeout.lock", "kind": "create"}]


def test_later_no_change_command_keeps_previous_validation_evidence_current(tmp_path):
    _, store, context = execution_session(tmp_path)
    commands = service(store, context, FakeSandboxExecutor())
    assert commands.run_command({"command": "true", "purpose": "validation"}).ok
    assert current_validation_evidence(store, context)
    assert commands.run_command({"command": "inspect", "purpose": "utility"}).ok
    assert current_validation_evidence(store, context)


def test_session_grant_is_requested_again_if_resource_object_is_replaced(tmp_path):
    _, store, context = execution_session(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    gate = FakeApprovalGate(True)
    commands = service(store, context, FakeSandboxExecutor(), gate)
    request = {"command": "write", "permissions": [{
        "capability": "filesystem_write", "resource": str(external),
        "reason": "cache", "scope": "session",
    }]}
    assert commands.run_command(request).ok
    external.rename(tmp_path / "old-external")
    external.mkdir()
    assert commands.run_command(request).ok
    assert len(gate.permission_requests) == 2


def test_after_audit_failure_is_the_condition_that_taints_workspace(tmp_path, monkeypatch):
    _, store, context = execution_session(tmp_path)
    from codeagent.runtime import command_service as module

    real_snapshot = module.take_workspace_snapshot
    calls = 0

    def fail_after(root, base):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise WorkspaceAuditError("injected after audit failure")
        return real_snapshot(root, base)

    monkeypatch.setattr(module, "take_workspace_snapshot", fail_after)
    executor = FakeSandboxExecutor(lambda root: (root / "unknown.txt").write_text("changed\n"))
    result = service(store, context, executor).run_command({"command": "mutate"})
    assert not result.ok
    assert store.workspace_state().value == "workspace_tainted"
