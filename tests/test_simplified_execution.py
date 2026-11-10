"""简化后的真实入口：共享循环/上下文、当前文件、取消回收、计量与日志恢复。"""
import builtins
import json
import multiprocessing
import time
from dataclasses import replace

import pytest

from codeagent.application.session_runtime import build_agent_runner
from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.context.builder import ContextManager
from codeagent.model_gateway.base import BaseModelClient, LLMResponse, LLMToolCall
from codeagent.orchestration.agents import AgentSessions
from codeagent.orchestration.lease import session_lease
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.delegation import DelegationService
from codeagent.runtime.request_executor import RequestExecutor
from test_orchestration import repo_store


class CurrentFilesModel(BaseModelClient):
    def complete(self, request):
        if request.purpose == 'context_condenser':
            return LLMResponse(text='USER_REQUIREMENTS: 只读当前README，继续调查并保留未确认项。', finish_reason='stop')
        users = [m for m in request.messages if m['role'] == 'user']
        anchor = users[-1]
        anchor_index = next(i for i, message in enumerate(request.messages) if message is anchor)
        tail = request.messages[anchor_index + 1:]
        observations = [m for m in tail if m['role'] == 'tool']
        if request.purpose == 'readonly_child':
            assert 'PARENT_SECRET' not in json.dumps(request.messages)
            goal = json.loads(anchor['content'])['goal']
            if goal == 'sleep':
                time.sleep(20)
            if not observations:
                return LLMResponse(tool_calls=[LLMToolCall(call_id='read', name='read_file', arguments={'path': 'README.md'})])
            return LLMResponse(text=json.dumps({'latest': observations[-1]['content'],
                'prior_history': any(m['role'] == 'tool' for m in request.messages[:anchor_index])}), finish_reason='stop')
        if not observations:
            return LLMResponse(tool_calls=[LLMToolCall(call_id='spawn', name='task', arguments={'subagent_type': 'explore', 'prompt': 'read'})])
        return LLMResponse(text='主任务结束', finish_reason='stop')


def current_factory(config):
    return CurrentFilesModel()


def services(store, config=None):
    executor = RequestExecutor(store, config or RuntimeConfig(delegation_enabled=True), ModelConfig(), current_factory, lambda: False)
    return AgentSessions(store, executor, DelegationService(store, executor, store.workspace_context))


def unpack(result):
    assert result.ok, result
    return json.loads(result.content)


def records(store, kind):
    return [e for e in store.read_events() if e.type == kind]


def test_main_child_use_current_files_without_framework_or_snapshot(repo_store, monkeypatch):
    _, store = repo_store
    importing = builtins.__import__
    def no_framework(name, *args, **kwargs):
        if name.startswith(('langgraph', 'langsmith')):
            raise AssertionError('普通Runtime不能导入图框架')
        return importing(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', no_framework)
    runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
        runtime_config=RuntimeConfig(delegation_enabled=True), model_factory=current_factory)
    output = runner.run_turn('PARENT_SECRET')
    assert output.status == 'completed', output
    assert [s['tool'] for s in output.steps] == ['task']
    assert len(records(store, 'model_request_reserved')) == 4
    assert len(records(store, 'model_request_settled')) == 4
    assert len(records(store, 'agent_run_finished')) == 1
    assert not (store.session_dir / 'graph.sqlite').exists()
    assert not any('snapshot' in e.type or e.type == 'graph_node' for e in store.read_events())
    assert 'subject_tree' not in records(store, 'agent_run_finished')[0].payload


def test_followup_keeps_history_but_reads_changed_project_and_price_does_not_gate(repo_store):
    root, store = repo_store
    store.append_event('user_message', {'message': 'PARENT_SECRET'})
    first = unpack(services(store).spawn({'goal': 'read', 'done_when': 'read', 'request_key': 'one'}))
    (root / 'README.md').write_text('CURRENT_EXTERNAL_CHANGE\n')
    # 价格、提示/角色结构可演进；不因历史spec哈希、树身份或旧快照失效而锁住续问。
    newer = services(store, RuntimeConfig(delegation_enabled=True, cost_per_token_microunits=99999999))
    second = unpack(newer.send({'agent_id': first['agent_id'], 'message': 'read', 'done_when': 'read', 'request_key': 'two'}))
    body = json.loads(second['findings'])
    assert body['prior_history'] and 'CURRENT_EXTERNAL_CHANGE' in body['latest']
    assert second['agent_id'] == first['agent_id'] and second['run_id'] != first['run_id']
    assert not {'subject_tree', 'context_epoch', 'stale', 'verified'} & second.keys()
    count = len(records(store, 'model_request_reserved'))
    assert unpack(newer.send({'agent_id': first['agent_id'], 'message': 'read', 'done_when': 'read', 'request_key': 'two'}))['run_id'] == second['run_id']
    assert len(records(store, 'model_request_reserved')) == count


def test_main_final_follows_sync_child_without_forced_integration_request(repo_store):
    _, store = repo_store
    before = {p.pid for p in multiprocessing.active_children()}
    runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
        runtime_config=RuntimeConfig(delegation_enabled=True), model_factory=current_factory)
    output = runner.run_turn('sync-final')
    assert output.status == 'completed' and output.final_text == '主任务结束'
    assert sum(e.payload['agent_id'] == 'main' for e in records(store, 'model_request_reserved')) == 2
    assert records(store, 'agent_run_finished')[0].payload['status'] == 'completed'
    assert records(store, 'agent_run_ack')[0].seq < records(store, 'assistant_message')[-1].seq
    assert len(records(store, 'agent_run_ack')) == 1
    assert {p.pid for p in multiprocessing.active_children()} == before
    assert not records(store, 'graph_final_deferred')


def test_child_context_reuses_same_build_and_compression(repo_store, monkeypatch):
    from codeagent.context.models import ModelCapabilities
    _, store = repo_store
    store.append_event('user_message', {'message': 'PARENT_SECRET'})
    build = ContextManager.build
    scopes = []
    def small(self, **kwargs):
        scopes.append(type(self.session_store).__name__)
        monkeypatch.setattr("codeagent.context.builder.HISTORY_MAX_EVENTS", 3)
        return build(self, **kwargs)
    monkeypatch.setattr(ContextManager, 'build', small)
    service = services(store)
    first = unpack(service.spawn({'goal': 'read', 'done_when': 'read', 'request_key': 'one'}))
    unpack(service.send({'agent_id': first['agent_id'], 'message': 'read', 'done_when': 'read', 'request_key': 'two'}))
    assert 'ScopedHistoryStore' in scopes
    summaries = records(store, 'agent_context_condensed')
    assert summaries and summaries[0].payload['newly_covered_event_ids']
    assert records(store, 'agent_context_condensation_attempt')[0].payload['status'] == 'valid_completion'
    assert sum(e.payload['purpose'] == 'context_condenser' for e in records(store, 'model_request_reserved')) >= 1
    assert not records(store, 'context_condensed')  # 私有摘要不进入父历史。


def test_child_commands_need_only_task_and_retry_uses_tool_operation_key(repo_store):
    from codeagent.orchestration.agents import ACTIVE_TOOL_OPERATION
    _, store = repo_store
    store.append_event('user_message', {'message': 'minimal arguments'})
    service = services(store)
    token = ACTIVE_TOOL_OPERATION.set('main:current:tool:0:0')
    try:
        first = unpack(service.spawn({'goal': 'read'}))
        count = len(records(store, 'model_request_reserved'))
        repeated = unpack(service.spawn({'goal': 'read'}))
        assert repeated['run_id'] == first['run_id']
        assert len(records(store, 'model_request_reserved')) == count
    finally:
        ACTIVE_TOOL_OPERATION.reset(token)
    token = ACTIVE_TOOL_OPERATION.set('main:current:tool:1:0')
    try:
        second = unpack(service.send({'agent_id': first['agent_id'], 'message': 'read'}))
        assert second['status'] == 'completed' and second['run_id'] != first['run_id']
    finally:
        ACTIVE_TOOL_OPERATION.reset(token)
