"""核对实际模型视图与操作结果；不把确定性动作当成模型理解能力。"""
import json
import pytest

from codeagent.context.builder import ContextManager
from codeagent.runtime.command import CommandResult
from codeagent.runtime.validation_output import empty_test_summary
from codeagent.benchmark.swebench_executor import SWEbenchDockerCommandExecutor
from runtime_fixtures import execution_session
from test_command_sandbox_service import FakeSandboxExecutor, service
from test_swebench_docker_executor import FakeDockerExecutionBackend, prepared


EMPTY_SYMPY = '\n================== tests finished: 0 passed, in 0.00 seconds ===================\nrunner_exit=0\n'


def test_docker_model_snapshot_uses_command_root_without_host_mapping(tmp_path):
    _, store, context = execution_session(tmp_path)
    commands = service(store, context, SWEbenchDockerCommandExecutor(prepared(context)))
    rendered = ContextManager(store, environment_provider=commands.environment_snapshot).build().messages[2]['content']
    snapshot = json.loads(rendered.split('\n', 1)[1])['runtime_context']
    assert snapshot['active_workspace'] == '/testbed'
    assert snapshot['execution_environment']['default_cwd_resolves_to'] == '/testbed'
    assert str(context.active_root) not in rendered and str(context.source_root) not in rendered
    assert '相对路径' in rendered
    environment = snapshot['execution_environment']
    assert environment['container_state_persists'] is True
    assert environment['shell_state_persists'] is False
    assert environment['container_lifetime'] == 'one_task'


def test_docker_command_metadata_reports_actual_initial_cwd(tmp_path):
    _, store, context = execution_session(tmp_path)
    executor = SWEbenchDockerCommandExecutor(prepared(context), FakeDockerExecutionBackend(context.active_root))
    result = service(store, context, executor).run_command({'command': 'pwd'})
    assert result.ok
    assert result.metadata['initial_cwd'] == result.metadata['launch_cwd'] == '/testbed'
    assert result.metadata['active_workspace'] == '/testbed'




def test_empty_test_output_in_utility_does_not_change_execution_result(tmp_path):
    _, store, context = execution_session(tmp_path)
    result = service(store, context, FakeSandboxExecutor(result=CommandResult(
        exit_code=0, stdout=EMPTY_SYMPY, payload_started=True,
    ))).run_command({'command': 'python bin/test -k missing', 'purpose': 'utility'})
    assert result.ok and 'validation_evidence_recorded' not in result.metadata


@pytest.mark.parametrize('summary', [EMPTY_SYMPY, 'Ran 0 tests in 0.000s\n\nOK',
                                    '\x1b[33m===== no tests ran in 0.02s =====\x1b[0m'])
def test_recognizes_explicit_empty_runner_summaries(summary):
    assert empty_test_summary(summary, '')


@pytest.mark.parametrize('output', ['tests finished: 0 passed, 1 failed, in 0.01 seconds',
                                   'tests finished: 0 passed, 2 expected to fail, in 0.01 seconds',
                                   'tests finished: 2 passed, in 0.01 seconds',
                                   'documentation says no tests ran', '0 passed', 'assertions OK', ''])
def test_unknown_or_nonempty_output_does_not_infer_empty_tests(output):
    assert empty_test_summary(output, '') is None








def test_empty_check_is_an_observation_without_verification_eligibility(tmp_path):
    _, store, context = execution_session(tmp_path)
    commands = service(store, context, FakeSandboxExecutor(result=CommandResult(
        exit_code=0, stdout=EMPTY_SYMPY, payload_started=True)))
    result = commands.run_command({'command': 'test runner', 'purpose': 'validation'})
    assert result.ok and result.metadata['exit_code'] == 0
    assert result.metadata['validation_issue'] == 'no_tests'
    assert '未运行测试' in result.metadata['validation_note']
    assert not any(e.type == 'validation_completed' for e in store.read_events())
