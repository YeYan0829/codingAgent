"""子方完成由正常响应决定，穿过旧步骤上限仍保留工具，不强制最后一次汇报。"""
import json
import os
from pathlib import Path

import pytest

from codeagent.config import RuntimeConfig
from codeagent.model_gateway.base import LLMResponse, LLMToolCall
from codeagent.model_gateway.fake import FakeLLM
from test_orchestration import events, repo_store, service_for, spawn_args


class ReportBoundaryModel(FakeLLM):
    def complete(self, request):
        folder = Path(os.environ['CHILD_REPORT_TEST'])
        records = list(folder.glob('request-*.json'))
        (folder / f'request-{len(records)}.json').write_text(json.dumps(request.model_dump(), ensure_ascii=False))
        target = int((folder / 'target').read_text())
        observations = [m for m in request.messages if m['role'] == 'tool']
        assert request.tools and request.tool_choice == 'auto'
        assert not any('最后一次' in str(m.get('content')) or '还可请求模型' in str(m.get('content')) for m in request.messages)
        if len(observations) >= target:
            return LLMResponse(text='README 已读取；尚未验证代码行为。', finish_reason='stop')
        return LLMResponse(tool_calls=[LLMToolCall(call_id=f'read-{len(observations)}', name='read_file',
            arguments={'path': 'README.md'})], finish_reason='tool_calls')


def report_boundary_factory(config):
    return ReportBoundaryModel()


@pytest.mark.parametrize('tool_steps', [0, 16])
def test_child_completes_with_tools_available_past_old_step_limit(repo_store, tmp_path, monkeypatch, tool_steps):
    _, store = repo_store
    capture = tmp_path / 'capture'; capture.mkdir()
    (capture / 'target').write_text(str(tool_steps))
    monkeypatch.setenv('CHILD_REPORT_TEST', str(capture))
    service = service_for(store, factory=report_boundary_factory, config=RuntimeConfig())
    result = json.loads(service.spawn(spawn_args()).content)
    assert result['status'] == 'completed', result
    assert result['findings'] == 'README 已读取；尚未验证代码行为。'
    assert 'unresolved' not in result and 'error' not in result
    assert all('unresolved' not in e.payload for e in events(store, 'agent_run_finished'))
    assert len(list(capture.glob('request-*.json'))) == len(events(store, 'model_request_reserved')) == tool_steps + 1
    assert len([e for e in events(store, 'agent_history') if e.payload['kind'] == 'tool_result']) == tool_steps
    assert events(store, 'agent_run_ack')


@pytest.mark.parametrize('status', ['completed', 'failed'])
def test_old_report_is_normalized_at_delivery_without_rewriting_evidence(repo_store, status):
    from codeagent.context.builder import ContextManager
    from codeagent.tools.observations import build_observation_tool

    _, store = repo_store
    service = service_for(store)
    body = '已定位接口；尚未确认兼容行为。正文中的 unresolved: [] 也只是原文。'
    payload = {'agent_id': 'child', 'run_id': 'legacy', 'status': status, 'findings': body,
               'unresolved': ['done_when 尚无独立 grader 验证', '缺少调用方证据'],
               'limitations': ['历史只读调查']}
    source = store.append_event('agent_run_finished', payload)
    store.append_event('child_report_observation', {'run_id': 'legacy', 'report_source_ref': f'seq:{source.seq}'})
    original = [e.model_dump() for e in store.read_events()]
    report = service.terminal('legacy')
    assert 'unresolved' not in report
    assert report['findings'] == body
    assert '子任务结论未经独立核验' in report['limitations']
    if status == 'completed':
        assert '缺少调用方证据' in report['limitations'] and 'error' not in report
    else:
        assert report['error'] == '缺少调用方证据'
    messages = ContextManager(store).build().messages
    observation = next(json.loads(m['content']) for m in messages
                       if m.get('role') == 'user' and '"kind": "child_report_observation"' in m.get('content', ''))
    assert observation['reports'][0] == report
    raw = build_observation_tool(store).handler({'source_ref': f'seq:{source.seq}'})
    assert raw.ok and json.loads(raw.content)['unresolved'] == payload['unresolved']
    assert [e.model_dump() for e in store.read_events()] == original


class EmptyReportModel(FakeLLM):
    def complete(self, request):
        return LLMResponse(text='', finish_reason='stop')


def empty_report_factory(config):
    return EmptyReportModel()


def test_empty_child_response_is_execution_error_not_unsolved_task(repo_store):
    _, store = repo_store
    service = service_for(store, factory=empty_report_factory)
    result = json.loads(service.spawn(spawn_args()).content)
    assert result['status'] == 'failed'
    assert result['error'] == 'empty child response'
    assert 'unresolved' not in result
