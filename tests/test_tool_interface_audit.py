"""生效接口逐项验收：真实注册、参数拒绝无副作用、调用结果与角色能力。"""
import json
from dataclasses import replace

import pytest

from codeagent.application.session_runtime import build_agent_runner
from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.model_gateway.base import LLMResponse
from codeagent.model_gateway.openai_client import _to_chat_completion_tool
from codeagent.orchestration.agents import AgentSessions, WaitCommand, CancelCommand, ChildAdapter
from codeagent.orchestration.lease import session_lease
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.delegation import DelegationService
from codeagent.runtime.request_executor import RequestExecutor
from codeagent.runtime.sandbox_executor import SandboxedCommandExecutor
from codeagent.tools.base import PermissionLevel
from codeagent.tools.registry import ToolRegistry
from runtime_fixtures import execution_session, HostFixtureBackend
from test_delegation import controlled_factory

FILES = {'list_dir', 'show_tree', 'read_file', 'search_text', 'find_files'}
LOCAL = FILES | {'git_status', 'git_diff', 'git_diff_stat', 'edit_file', 'apply_workspace_edit', 'run_command',
                 'read_task_plan', 'update_task_plan', 'read_observation'}
CONTROL = {'task', 'delegate_readonly', 'spawn_agent', 'send_agent', 'wait_agents', 'cancel_agents', 'close_agent'}
VALID_ARGUMENTS = {
    'list_dir': {}, 'show_tree': {}, 'read_file': {'path': 'sort_utils.py'},
    'search_text': {'query': 'sort_values'}, 'find_files': {},
    'git_status': {}, 'git_diff': {}, 'git_diff_stat': {},
    'edit_file': {'path': 'sort_utils.py', 'edits': [{'old_text': 'reverse=True', 'new_text': 'reverse=False'}]},
    'apply_workspace_edit': {'operations': [{'path': 'unexpected.txt', 'content': 'must not write'}]},
    'run_command': {'command': 'touch unexpected.txt'},
    'read_task_plan': {}, 'update_task_plan': {'tasks': [{'title': 'must not save'}]},
    'read_observation': {'source_ref': 'seq:1'},
    'spawn_agent': {'goal': 'must not run'},
    'send_agent': {'agent_id': 'agent-placeholder', 'message': 'must not run'},
    'wait_agents': {'run_ids': ['run-placeholder']},
    'cancel_agents': {'run_ids': ['run-placeholder']},
    'close_agent': {'agent_id': 'agent-placeholder'},
    'delegate_readonly': {'tasks': [{'goal': 'must not run'}]},
    'task': {'prompt': 'must not run', 'subagent_type': 'code-explorer'},
}


@pytest.fixture
def live_tools(tmp_path):
    root, store, context = execution_session(tmp_path)
    runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(True))
    assert {t.name for t in runner.tools.list_tools()} == LOCAL
    runner.command_service.executor = SandboxedCommandExecutor(HostFixtureBackend())
    store.append_event('user_message', {'message': '接口验收'})
    executor = RequestExecutor(store, RuntimeConfig(delegation_enabled=True), ModelConfig(), controlled_factory, lambda: False)
    delegation = DelegationService(store, executor, store.workspace_context)
    sessions = AgentSessions(store, executor, delegation)
    runner.tools.register(delegation.tool())
    for tool in sessions.tools(): runner.tools.register(tool)
    with session_lease(store):
        yield root, store, runner, sessions


def test_all_live_definitions_use_their_input_type_and_provider_payload(live_tools):
    _, _, runner, _ = live_tools
    tools = runner.tools.list_tools()
    assert {t.name for t in tools} == LOCAL | CONTROL
    for tool in tools:
        assert tool.input_model is not None, tool.name
        assert tool.schema == tool.input_model.model_json_schema(), tool.name
        assert tool.schema['additionalProperties'] is False, tool.name
        payload = _to_chat_completion_tool(tool.to_model_tool())['function']
        assert payload == {'name': tool.name, 'description': tool.description, 'parameters': tool.schema}
    assert runner.tools.get('run_command').permission_level == PermissionLevel.WORKSPACE_EXECUTE
    assert all(runner.tools.get(n).permission_level == PermissionLevel.SESSION_WRITE for n in CONTROL)


@pytest.mark.parametrize('name', sorted(LOCAL | CONTROL))
def test_unknown_field_is_rejected_before_io_or_requests(live_tools, name):
    root, store, runner, _ = live_tools
    before = (root / 'sort_utils.py').read_bytes()
    frontier = len(store.read_events())
    arguments = {**VALID_ARGUMENTS[name], '_unexpected': 'must not execute'}
    result = runner.tools.call(name, arguments)
    assert not result.ok and result.error_code in {'invalid_arguments', 'task_plan_update_rejected'}, (name, result)
    assert 'extra_forbidden' in result.error
    assert (root / 'sort_utils.py').read_bytes() == before
    assert not (root / 'unexpected.txt').exists()
    assert len(store.read_events()) == frontier


def test_valid_examples_for_all_tools_have_real_results(live_tools):
    root, store, runner, _ = live_tools
    observed = set()
    def call(name, args):
        result = runner.tools.call(name, args)
        assert result.ok, (name, result)
        observed.add(name)
        return result
    call('list_dir', {})
    call('show_tree', {'max_depth': 1})
    original = call('read_file', {'path': 'sort_utils.py'})
    call('find_files', {'include': ['**/*.py']})
    call('search_text', {'query': 'sort_values', 'context_lines': 25})
    call('git_status', {})
    call('git_diff', {})
    call('git_diff_stat', {'path': 'sort_utils.py'})
    call('edit_file', {'path': 'sort_utils.py', 'edits': [{'old_text': 'reverse=True', 'new_text': 'reverse=False'}]})
    call('apply_workspace_edit', {'operations': [{'path': 'audit.txt', 'content': 'note\n'}]})
    command = call('run_command', {'command': 'printf audit'})
    history = call('read_observation', {'source_ref': command.metadata['source_ref'], 'stream': 'stdout'})
    assert history.content == 'audit'
    call('update_task_plan', {'tasks': [{'title': '核对接口', 'status': 'done'}]})
    call('read_task_plan', {})
    first = json.loads(call('spawn_agent', {'goal': 'read'}).content)
    second = json.loads(call('send_agent', {'agent_id': first['agent_id'], 'message': 'read'}).content)
    waited = json.loads(call('wait_agents', {'run_ids': [first['run_id'], second['run_id']], 'timeout': 0}).content)
    assert waited['satisfied']
    call('cancel_agents', {'run_ids': [first['run_id']]})
    call('close_agent', {'agent_id': first['agent_id']})
    call('delegate_readonly', {'tasks': [{'goal': 'read'}]})
    call('task', {'prompt': 'read', 'subagent_type': 'code-explorer'})
    assert observed == LOCAL | CONTROL
    assert (root / 'audit.txt').read_text() == 'note\n'
    assert 'reverse=False' in (root / 'sort_utils.py').read_text()


def test_child_registry_rejects_command_state_and_write_tools(live_tools):
    _, _, runner, _ = live_tools
    child = ToolRegistry(readonly=True)
    for tool in runner.tools.list_tools():
        if tool.name in CONTROL | {'run_command', 'edit_file', 'apply_workspace_edit', 'update_task_plan'}:
            with pytest.raises(ValueError): child.register(tool)


def test_read_modes_hash_and_empty_byte_fragment_match_descriptions(live_tools):
    root, _, runner, _ = live_tools
    read = runner.tools.get('read_file').handler
    first = read({'path': 'sort_utils.py'})
    missing = read({'path': 'sort_utils.py', 'start_byte': 0})
    assert not missing.ok and missing.error_code == 'invalid_arguments'
    mixed = read({'path': 'sort_utils.py', 'start_byte': 0, 'start_line': 1, 'expected_sha256': first.metadata['sha256']})
    assert not mixed.ok and mixed.error_code == 'invalid_arguments'
    stale = read({'path': 'sort_utils.py', 'expected_sha256': '0' * 64})
    assert not stale.ok and stale.error_code == 'cursor_stale'
    (root / 'empty').touch()
    empty = read({'path': 'empty'})
    fragment = read({'path': 'empty', 'start_byte': 0, 'expected_sha256': empty.metadata['sha256']})
    assert fragment.ok and fragment.metadata['returned_range'] is None


def test_explicit_rg_glob_overrides_ignore_but_never_denied_paths(live_tools):
    root, store, runner, _ = live_tools
    (root / '.gitignore').write_text('ignored.txt\nprivate/\n')
    (root / 'ignored.txt').write_text('needle\n')
    (root / 'private').mkdir(); (root / 'private/hidden.txt').write_text('private needle\n')
    find = runner.tools.get('find_files')
    assert 'ignored.txt' not in find.handler({}).content
    assert 'ignored.txt' in find.handler({'include': ['*.txt']}).content
    from codeagent.tools.fs_read import build_fs_tools
    private = {t.name: t for t in build_fs_tools(replace(store.workspace_context(), denied_paths=('private',)))}
    assert 'private' not in private['find_files'].handler({'include': ['*.txt']}).content
    assert 'private needle' not in private['search_text'].handler({'query': 'needle', 'include': ['*.txt']}).content


def test_search_counts_only_matches_in_returned_page(live_tools):
    root, _, runner, _ = live_tools
    (root / 'first.txt').write_text('needle\n')
    (root / 'second.txt').write_text('needle\n')
    result = runner.tools.call('search_text', {'query': 'needle', 'include': ['*.txt'], 'max_results': 1})
    assert result.ok and result.truncated
    assert len(result.content.splitlines()) == 1
    assert result.metadata['match_count'] == 1
    assert result.metadata['files_with_matches'] == 1


def test_wait_and_cancel_accept_more_than_old_run_quota_without_dispatch():
    ids = ['run-' + str(i) for i in range(24)]
    assert WaitCommand.model_validate({'run_ids': ids}).run_ids == ids
    assert CancelCommand.model_validate({'run_ids': ids}).run_ids == ids


@pytest.mark.parametrize('name', ['git_diff', 'git_diff_stat'])
def test_git_path_is_literal_instead_of_a_pathspec(live_tools, name):
    import subprocess
    root, store, runner, _ = live_tools
    for path in ('[x].py', 'x.py'):
        (root / path).write_text('before\n')
    subprocess.run(['git', 'add', '--', '.'], cwd=root, check=True)
    (root / '[x].py').write_text('literal-change\n')
    (root / 'x.py').write_text('unrelated-change\n')
    from codeagent.tools.git_read import build_git_tools
    for context in (store.workspace_context(), replace(store.workspace_context(), denied_paths=('private',))):
        tool = next(t for t in build_git_tools(context) if t.name == name)
        result = tool.handler({'path': '[x].py'})
        assert result.ok
        assert '[x].py' in result.content
        assert 'unrelated-change' not in result.content
        if name == 'git_diff':
            assert 'literal-change' in result.content


def test_actual_child_exports_only_six_read_tools_with_shared_schemas(live_tools):
    _, store, runner, sessions = live_tools
    from codeagent.orchestration.lease import current_execution_epoch
    first = sessions.spawn({'goal': 'read'})
    assert first.ok
    payload = next(e.payload for e in store.read_events() if e.type == 'agent_command_intent')
    adapter = ChildAdapter(sessions, sessions.executor, payload, store.workspace_root, current_execution_epoch(store))
    assert {t.name for t in adapter.registry.list_tools()} == FILES | {'read_observation'}
    for tool in adapter.registry.list_tools():
        assert tool.schema == runner.tools.get(tool.name).schema
        assert tool.permission_level == PermissionLevel.READ


def test_task_tracking_switch_does_not_change_command_schema(tmp_path):
    _, store, _ = execution_session(tmp_path)
    runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(True),
                                runtime_config=RuntimeConfig(task_tracking=False))
    assert {t.name for t in runner.tools.list_tools()} == LOCAL - {'read_task_plan', 'update_task_plan'}
    from codeagent.tools.command import command_tool_schema
    assert command_tool_schema(True) == command_tool_schema(False) == runner.tools.get('run_command').schema
