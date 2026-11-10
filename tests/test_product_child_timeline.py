"""父子可见过程：纯事件投影不启动任务，公开内容、因果位置与各次执行隔离。"""
import copy
import json

import pytest

from codeagent.product.read_model import attach_child_timelines, timeline_turns
from codeagent.product.orchestration_view import orchestration_view
from codeagent.session.events import SessionEvent


class Scene:
    def __init__(self):
        self.events = []
        self.root = 'root'
        self.emit('user_message', {'message': 'inspect'})

    def emit(self, kind, payload, root=None):
        event = SessionEvent(seq=len(self.events) + 1, type=kind, payload=payload, turn_id=root or self.root)
        self.events.append(event)
        return event

    def launch(self, run='one', action='spawn', execution='foreground', epoch=1, key=None, agent='a'):
        key = key or run
        tool = 'spawn_agent' if action == 'spawn' else 'send_agent'
        self.emit('assistant_tool_calls', {'message': '主说明 ' + run, 'tool_calls': [
            {'call_id': 'main-' + run, 'name': tool, 'arguments': {'request_key': key, 'goal': '核对 ' + run}}]})
        self.emit('tool_requested', {'call_id': 'main-' + run, 'name': tool})
        self.emit('agent_command_intent', {'action': action, 'request_key': key, 'command_id': 'cmd-' + run,
            'execution': execution, 'agent': {'agent_id': agent, 'spec': {'name': 'review'}},
            'run': {'run_id': run, 'agent_id': agent, 'root_turn_id': self.root, 'message': '核对 ' + run,
                    'context_epoch': epoch, 'execution_epoch': 1, 'identity': {'tree': 'tree', 'active_root': '/repo'}}})
        self.emit('agent_run_running', {'run_id': run})

    def history(self, run, kind, data, epoch=1, agent='a'):
        return self.emit('agent_history', {'run_id': run, 'agent_id': agent, 'context_epoch': epoch,
            'execution_epoch': 1, 'kind': kind, 'data': data})

    def read(self, run='one', path='README.md', epoch=1):
        call = {'call_id': 'same-id', 'name': 'read_file', 'arguments': {'path': path}}
        self.history(run, 'user_message', {'message': 'PRIVATE ANCHOR'}, epoch)
        self.history(run, 'assistant_tool_calls', {'message': '子说明 ' + run,
            'reasoning_content': 'PRIVATE REASONING', 'tool_calls': [call]}, epoch)
        self.emit('tool_execution_intent', {'operation_key': run + ':0:tool:0', 'call': call, 'readonly': True})
        return call

    def finish(self, run='one', status='completed'):
        self.emit('agent_run_finished', {'run_id': run, 'status': status, 'findings': '报告 ' + run, 'unresolved': []})
        self.emit('agent_run_ack', {'run_id': run})

    def view(self):
        tasks = orchestration_view(self.events, {'workspace': '/repo'})
        attach_child_timelines(tasks, self.events)
        turns = timeline_turns(self.events, {})
        for turn in turns:
            turn['delegation'] = next(t for t in tasks['tasks'] if t['rootTurnId'] == turn['turnId'])
        return turns


def tools(turn):
    return [tool for item in turn['items'] for tool in item.get('tools', [])]


def test_foreground_intent_links_before_receipt_and_shows_running_public_child_tool():
    scene = Scene()
    scene.launch()
    scene.read()
    before = copy.deepcopy([e.model_dump() for e in scene.events])
    turn = scene.view()[0]
    call = tools(turn)[0]
    run = turn['delegation']['runs'][0]
    assert call['runId'] == 'one' and call['status'] == 'running' and 'result' not in call
    assert run['currentActivity'] == {'tool': 'read_file', 'argumentSummary': 'README.md', 'status': 'running'}
    assert [i['type'] for i in run['items']] == ['markdown', 'activityGroup']
    assert run['items'][0]['text'] == '子说明 one'
    assert 'PRIVATE' not in json.dumps(turn, ensure_ascii=False)
    assert [e.model_dump() for e in scene.events] == before


@pytest.mark.parametrize('terminal,expected', [('completed', 'succeeded'), ('cancelled', 'cancelled'), ('failed', 'interrupted')])
def test_run_scopes_duplicate_call_ids_refresh_and_late_results(terminal, expected):
    scene = Scene()
    scene.launch()
    first = scene.read()
    if terminal == 'completed':
        scene.history('one', 'tool_result', {'call_id': 'same-id', 'name': 'read_file',
            'result': {'ok': True, 'content': 'old evidence'}})
        scene.history('one', 'assistant_message', {'message': '报告 one'})
    scene.finish(status=terminal)
    scene.launch('two', action='send', epoch=2)
    second = scene.read('two', 'verify.py', epoch=2)
    # 同一工具 ID 可在不同 run 复用；旧终态后迟到的历史不得复活或污染新 run。
    scene.history('one', 'tool_result', {'call_id': 'same-id', 'name': 'read_file', 'result': {'ok': False, 'error': 'LATE'}})
    scene.emit('tool_execution_result', {'operation_key': 'two:0:tool:0', 'call': second,
        'output': {'ok': True, 'content': 'x' * 20000}})
    turn = scene.view()[0]
    old, new = turn['delegation']['runs']
    assert tools({'items': old['items']})[0]['status'] == expected
    assert tools({'items': new['items']})[0]['status'] == 'succeeded'
    assert tools({'items': new['items']})[0]['outputTruncated']
    assert len(tools({'items': new['items']})[0]['output']) <= 12000
    assert not old['stale'] and not new['stale'] and new['revisionMode'] == 'refresh'
    assert old['agentLabel'] == new['agentLabel'] == '调查 A' and new['runOrdinal'] == 2
    assert 'LATE' not in json.dumps(turn, ensure_ascii=False)
    assert [t['argumentSummary'] for t in tools({'items': new['items']})] == ['verify.py']


def test_background_arrival_and_wait_are_in_parent_event_order_and_deduplicated():
    scene = Scene()
    scene.launch(execution='background')
    scene.emit('assistant_tool_calls', {'tool_calls': [{'call_id': 'parent-read', 'name': 'list_dir', 'arguments': {}}]})
    scene.read()
    scene.emit('assistant_tool_calls', {'tool_calls': [{'call_id': 'wait', 'name': 'wait_agents',
        'arguments': {'run_ids': ['one'], 'mode': 'all'}}]})
    scene.emit('tool_requested', {'call_id': 'wait', 'name': 'wait_agents'})
    scene.emit('agent_wait_started', {'wait_id': 'w', 'run_ids': ['one'], 'mode': 'all'})
    live = scene.view()[0]
    assert tools(live)[-1]['waitRunIds'] == ['one'] and tools(live)[-1]['status'] == 'running'
    scene.finish()
    scene.emit('agent_wait_finished', {'wait_id': 'w', 'satisfied': True, 'pending': []})
    scene.emit('tool_result', {'call_id': 'wait', 'result': {'ok': True}})
    scene.emit('assistant_message', {'message': '主任务汇总'})
    scene.emit('agent_run_finished', {'run_id': 'one', 'status': 'failed', 'unresolved': ['LATE']})
    turn = scene.view()[0]
    notice = next(i for i in turn['items'] if i['type'] == 'taskNotice')
    assert notice['status'] == 'completed'
    assert [i['type'] for i in turn['items']] == ['markdown', 'activityGroup', 'taskNotice', 'markdown']
    assert tools(turn)[-1]['waitSatisfied'] and tools(turn)[-1]['status'] == 'submitted'


def test_same_request_key_never_cross_links_parent_turns():
    scene = Scene()
    scene.launch(key='same')
    scene.finish()
    scene.root = 'next-root'
    scene.emit('user_message', {'message': 'next'})
    scene.launch('two', key='same', agent='b')
    first, second = scene.view()
    assert tools(first)[0]['runId'] == 'one'
    assert tools(second)[0]['runId'] == 'two'
    assert first['delegation']['runs'][0]['agentLabel'] == '调查 A'
    assert second['delegation']['runs'][0]['agentLabel'] == '调查 B'


@pytest.mark.parametrize('ok', [True, False])
def test_sync_task_links_by_operation_before_receipt_and_keeps_terminal_status(ok):
    scene = Scene()
    operation = 'main:root:tool:0:0'
    task = {'call_id': 'task-one', 'name': 'task',
            'arguments': {'subagent_type': 'review', 'prompt': '核对README'}}
    scene.emit('assistant_tool_calls', {'tool_calls': [task]})
    scene.emit('tool_requested', task)
    scene.emit('tool_execution_intent', {'operation_key': operation, 'call': task, 'readonly': True})
    scene.emit('agent_command_intent', {'action': 'spawn', 'request_key': operation,
        'command_id': 'command-task', 'execution': 'foreground',
        'agent': {'agent_id': 'a', 'spec': {'name': 'review'}},
        'run': {'run_id': 'one', 'agent_id': 'a', 'root_turn_id': 'root', 'message': '核对README', 'execution_epoch': 1}})
    scene.emit('agent_run_running', {'run_id': 'one'})
    scene.read(epoch=None)
    call = tools(scene.view()[0])[0]
    assert call['tool'] == 'task' and call['runId'] == 'one' and call['agentId'] == 'a'
    assert call['status'] == 'running' and 'result' not in call
    scene.finish(status='completed' if ok else 'failed')
    scene.emit('tool_result', {'call_id': 'task-one', 'name': 'task', 'result': {
        'ok': ok, 'content': json.dumps({'task_id': 'a', 'run_id': 'one', 'status': 'completed' if ok else 'error', 'text': 'report'}),
        'metadata': {'task_id': 'a', 'run_id': 'one'}}})
    turn = scene.view()[0]
    call = tools(turn)[0]
    assert call['runId'] == 'one' and call['status'] == ('succeeded' if ok else 'failed')
    assert turn['delegation']['runs'][0]['terminalAck']
