"""项目命令按原样执行；清单和验证版本不构成执行条件。"""
import pytest
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.sandbox_executor import SandboxedCommandExecutor
from codeagent.runtime.command import CommandExecutionStatus, CommandResult
from codeagent.tools.command import command_tool_schema
from runtime_fixtures import execution_session, HostFixtureBackend
from test_command_sandbox_service import service, FakeSandboxExecutor


@pytest.mark.parametrize('command,code,output', [
    ('false; printf after', 0, 'after'),
    ('false || printf recovered', 0, 'recovered'),
    ("python3 - <<'EOF'\nprint('checked')\nEOF", 0, 'checked'),
    ('exit 3', 3, ''),
])
def test_command_syntax_is_preserved_for_every_purpose(tmp_path, command, code, output):
    _, store, context = execution_session(tmp_path)
    commands = service(store, context, SandboxedCommandExecutor(HostFixtureBackend()))
    result = commands.run_command({'command': command, 'purpose': 'validation'})
    assert result.metadata['exit_code'] == code
    assert result.ok == (code == 0)
    assert output in result.content
    assert not any(e.type == 'validation_completed' for e in store.read_events())


def test_command_schema_does_not_depend_on_plan_capability():
    assert command_tool_schema(True) == command_tool_schema(False)
    assert 'verifies' not in command_tool_schema()['properties']


def test_failed_command_does_not_require_a_task_or_block_following_work(tmp_path):
    _, store, context = execution_session(tmp_path)
    commands = service(store, context, FakeSandboxExecutor(result=CommandResult(
        status=CommandExecutionStatus.COMPLETED, exit_code=1, stdout='failed check', payload_started=True)))
    failed = commands.run_command({'command': 'pytest', 'purpose': 'validation'})
    assert not failed.ok and failed.metadata['exit_code'] == 1
    commands.executor = FakeSandboxExecutor()
    assert commands.run_command({'command': 'true'}).ok
    assert store.workspace_state().value == 'current_project'
    assert not any(e.type == 'task_plan_updated' for e in store.read_events())
