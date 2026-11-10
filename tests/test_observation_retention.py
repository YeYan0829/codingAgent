"""保存先于模型投影；来源可在重启后分页读取，不重复原操作。"""
import json
from types import SimpleNamespace
from dataclasses import replace

from codeagent.context.builder import ContextManager
from codeagent.model_gateway.base import LLMResponse, LLMToolCall
from codeagent.orchestration.agents import ChildAdapter
from codeagent.runtime.sandbox_executor import SandboxedCommandExecutor
from codeagent.session.store import SessionStore
from codeagent.tools.base import ToolResult, ToolSpec, PermissionLevel
from codeagent.tools.observations import build_observation_tool
from codeagent.tools.registry import ToolRegistry
from runtime_fixtures import execution_session, HostFixtureBackend
from test_command_sandbox_service import service
from test_orchestration import repo_store


def test_successful_command_middle_output_survives_restart(tmp_path):
    _, store, context = execution_session(tmp_path)
    commands = service(store, context, SandboxedCommandExecutor(HostFixtureBackend()))
    result = commands.run_command({'command': 'python3 -c "print(\'x\'*12000 + \'中间观察\' + \'y\'*12000)"'})
    assert result.ok and result.truncated and '中间观察' not in result.content
    assert result.metadata['stdout_preview_truncated'] and not result.metadata['stdout_capture_truncated']
    root = store.diagnostics_dir / 'commands' / result.metadata['execution_id']
    assert (root / 'stdout.log').exists() and (root / 'result.json').exists()
    restored = SessionStore(store.workspace_root, store.session_id, store.session_root).load()
    read = build_observation_tool(restored).handler
    page = read({'source_ref': result.metadata['source_ref'], 'stream': 'stdout', 'start_byte': 11990, 'max_bytes': 128})
    assert page.ok and '中间观察' in page.content and page.metadata['has_more_after']
    assert not page.metadata['source_capture_truncated']
    assert sum(e.type == 'command_completed' for e in restored.read_events()) == 1


def test_capture_limit_and_model_pagination_are_separate(tmp_path, monkeypatch):
    _, store, context = execution_session(tmp_path)
    monkeypatch.setattr(SandboxedCommandExecutor, 'OUTPUT_LIMIT_BYTES', 512)
    result = service(store, context, SandboxedCommandExecutor(HostFixtureBackend())).run_command({
        'command': 'python3 -c "print(\'z\'*2000)"'})
    assert result.ok and result.metadata['stdout_capture_truncated']
    page = build_observation_tool(store).handler({'source_ref': result.metadata['source_ref'], 'stream': 'stdout'})
    assert page.ok and not page.metadata['has_more_after']
    assert page.metadata['total_bytes'] == 512 and page.metadata['source_capture_truncated']


def test_child_raw_result_is_not_projected_before_persistence(repo_store):
    root, store = repo_store
    body = 'head\n' + 'a' * 10000 + 'PRIVATE_MIDDLE' + 'b' * 10000 + '\ntail'
    adapter = ChildAdapter.__new__(ChildAdapter)
    adapter.registry = ToolRegistry(store.workspace_context(), readonly=True)
    adapter.registry.register(ToolSpec('read_file', 'read', PermissionLevel.READ, {},
        lambda args: ToolResult(ok=True, content=body, metadata={'path': 'README.md'})))
    from codeagent.runtime.policy import ReadOnlyChildPolicy
    adapter.policy, adapter.approval_gate = ReadOnlyChildPolicy(), None
    result = adapter._call(LLMToolCall(call_id='read', name='read_file', arguments={'path': 'README.md'}))
    assert result['content'] == body
    event = store.append_event('agent_history', {'agent_id': 'child-a', 'run_id': 'r', 'context_epoch': 1,
        'kind': 'tool_result', 'data': {'call_id': 'read', 'name': 'read_file', 'arguments': {'path': 'README.md'}, 'result': result}})
    request = {'source_ref': 'seq:' + str(event.seq), 'max_bytes': 65536}
    own = build_observation_tool(store, agent_id='child-a').handler(request)
    assert own.ok and 'PRIVATE_MIDDLE' in own.content
    assert not build_observation_tool(store).handler(request).ok
    assert not build_observation_tool(store, agent_id='child-b').handler(request).ok


def test_child_report_producer_keeps_complete_body(repo_store):
    _, store = repo_store
    adapter = ChildAdapter.__new__(ChildAdapter)
    adapter.store, adapter.run_id = store, 'run-long-report'
    from codeagent.config import ModelConfig
    adapter.max_tokens = ModelConfig().resolved_max_tokens
    adapter.history = lambda *args, **kwargs: None
    text = '完整报告' * 3000 + 'REPORT_END'
    result = adapter.record_response(LLMResponse(text=text), 0)
    assert result['output']['findings'] == text


def test_report_reference_survives_further_requests_and_restart(repo_store):
    _, store = repo_store
    store.append_event('user_message', {'message': 'ORIGINAL_REQUIREMENT'})
    report = store.append_event('agent_run_finished', {'run_id': 'run-a', 'agent_id': 'child-a',
        'context_epoch': 1, 'status': 'completed', 'findings': 'REPORT_ONLY_FACT', 'unresolved': [], 'limitations': []})
    reference = store.append_event('child_report_observation', {'run_id': 'run-a', 'report_source_ref': 'seq:' + str(report.seq)})
    assert 'findings' not in reference.payload
    for i in range(2):
        store.append_event('assistant_tool_calls', {'tool_calls': [{'call_id': f'c{i}', 'name': 'list_dir', 'arguments': {}}]})
        store.append_event('tool_result', {'call_id': f'c{i}', 'name': 'list_dir', 'result': {'ok': True, 'content': 'read'}})
        messages = ContextManager(store).build().messages
        assert 'REPORT_ONLY_FACT' in json.dumps(messages)
        assert [m for m in messages if m['role'] == 'user'][-1]['content'] == 'ORIGINAL_REQUIREMENT'
    restored = SessionStore(store.workspace_root, store.session_id, store.session_root).load()
    assert 'REPORT_ONLY_FACT' in json.dumps(ContextManager(restored).build().messages)
    source = build_observation_tool(restored).handler({'source_ref': 'seq:' + str(report.seq)})
    assert source.ok and 'REPORT_ONLY_FACT' in source.content


def test_observation_reader_checks_scope_path_and_range(repo_store, monkeypatch):
    root, store = repo_store
    store.append_event('user_message', {'message': 'not an observation'})
    user = store.read_events()[-1]
    event = store.append_event('tool_result', {'name': 'read_file', 'arguments': {'path': 'README.md'},
        'result': {'ok': True, 'content': '中文🙂' * 20}})
    read = build_observation_tool(store).handler
    original = read({'source_ref': 'seq:' + str(user.seq)})
    assert original.ok and original.metadata['source_type'] == 'user_message'
    assert not build_observation_tool(store, agent_id='child-a').handler({'source_ref': 'seq:' + str(user.seq)}).ok
    assert not read({'source_ref': 'command:../secret', 'stream': 'stdout'}).ok
    assert not read({'source_ref': 'seq:' + str(event.seq), 'start_byte': 100000}).ok
    cursor, parts = 0, []
    while True:
        page = read({'source_ref': 'seq:' + str(event.seq), 'start_byte': cursor, 'max_bytes': 17})
        assert page.ok
        parts.append(page.content)
        if not page.metadata['has_more_after']:
            break
        assert page.metadata['next_start_byte'] > cursor
        cursor = page.metadata['next_start_byte']
    assert json.loads(''.join(parts))['result']['content'] == '中文🙂' * 20
    context = replace(store.workspace_context(), denied_paths=('README.md',))
    monkeypatch.setattr(store, 'workspace_context', lambda: context)
    assert not read({'source_ref': 'seq:' + str(event.seq)}).ok


def test_summary_and_original_constraints_remain_readable_after_compaction(repo_store, monkeypatch):
    monkeypatch.setattr("codeagent.context.builder.HISTORY_MAX_EVENTS", 4)
    from codeagent.runtime.runner import AgentRunner
    from codeagent.runtime.approval import AutoApprovalGate
    from codeagent.context.models import ModelCapabilities, ReadyContext
    from test_semantic_condensation import CondensingModel, _append_tool_step
    _, store = repo_store
    original = store.append_event('user_message', {'message': '必须保留的原始限制'})
    for index in range(8):
        _append_tool_step(store, index, size=500)
    store.append_event('assistant_message', {'message': '已调查，存在未验证事项'})
    store.append_event('user_message', {'message': '继续当前问题'})
    runner = AgentRunner(store, CondensingModel(), ToolRegistry(), AutoApprovalGate(False))
    prepared = runner._prepare_context(ModelCapabilities(), ())
    assert isinstance(prepared, ReadyContext)
    summary = next(e for e in store.read_events() if e.type == 'context_condensed')
    assert f'摘要来源 seq:{summary.seq}' in json.dumps(prepared.messages, ensure_ascii=False)
    restored = SessionStore(store.workspace_root, store.session_id, store.session_root).load()
    read = build_observation_tool(restored).handler
    source = read({'source_ref': f'seq:{summary.seq}'})
    assert source.ok and f'seq:{original.seq}' in source.content
    raw = read({'source_ref': f'seq:{original.seq}'})
    assert raw.ok and '必须保留的原始限制' in raw.content
    assert not build_observation_tool(restored, agent_id='child').handler({'source_ref': f'seq:{summary.seq}'}).ok


def test_large_report_uses_one_common_projection_and_source(repo_store):
    _, store = repo_store
    store.append_event('user_message', {'message': '当前任务'})
    report = store.append_event('agent_run_finished', {'run_id': 'large', 'agent_id': 'child',
        'findings': 'h' * 20000 + 'MIDDLE_REPORT_FACT' + 't' * 20000, 'status': 'completed'})
    store.append_event('child_report_observation', {'run_id': 'large', 'report_source_ref': f'seq:{report.seq}'})
    manager = ContextManager(store)
    from codeagent.context.models import ModelCapabilities
    messages = manager.build(model_capabilities=ModelCapabilities(context_limit=128_000)).messages
    rendered = json.dumps(messages, ensure_ascii=False)
    assert 'MIDDLE_REPORT_FACT' in rendered and 'context_truncated' not in rendered
    assert f'seq:{report.seq}' in rendered
    source = build_observation_tool(store).handler({'source_ref': f'seq:{report.seq}', 'start_byte': 19500, 'max_bytes': 1500})
    assert source.ok and 'MIDDLE_REPORT_FACT' in source.content


def test_scoped_history_filters_parent_and_other_child_summaries(repo_store):
    from codeagent.orchestration.context import ScopedHistoryStore
    _, store = repo_store
    store.append_event('user_message', {'message': 'PARENT_SECRET'})
    store.append_event('context_condensed', {'summary': 'PARENT_SUMMARY_SECRET'})
    for owner in ('a', 'b'):
        store.append_event('agent_history', {'agent_id': owner, 'context_epoch': 1, 'run_id': owner,
            'kind': 'user_message', 'data': {'message': owner + '_PRIVATE'}})
        store.append_event('agent_context_condensed', {'agent_id': owner, 'context_epoch': 1,
            'summary': owner + '_SUMMARY_PRIVATE'})
    scope = ScopedHistoryStore(store, {'agent_id': 'a', 'context_epoch': 1, 'run_id': 'a'}, lambda *args: None)
    rendered = json.dumps([e.model_dump() for e in scope.read_events()])
    assert 'a_PRIVATE' in rendered and 'a_SUMMARY_PRIVATE' in rendered
    assert 'PARENT' not in rendered and 'b_PRIVATE' not in rendered and 'b_SUMMARY_PRIVATE' not in rendered
    own_summary = next(e for e in store.read_events() if e.type == 'agent_context_condensed' and e.payload['agent_id'] == 'a')
    read = build_observation_tool(store, agent_id='a').handler
    assert read({'source_ref': f'seq:{own_summary.seq}'}).ok
    assert not build_observation_tool(store).handler({'source_ref': f'seq:{own_summary.seq}'}).ok
