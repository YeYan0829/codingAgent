"""C3 协议验收：真实 supervisor/图/文件/transport；不冒充 GUI 或模型质量。"""
import hashlib
import json
import multiprocessing
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from codeagent.application.session_runtime import build_agent_runner
from codeagent.config import RuntimeConfig
from codeagent.model_gateway.base import LLMResponse, LLMToolCall
from codeagent.product.execution import ExecutionSupervisor
from codeagent.product.orchestration_view import orchestration_view, usage_view
from codeagent.product.rpc import JsonRpcServer
from codeagent.product.service import ProductApplicationService, ProductServiceError
from codeagent.session.events import SessionEvent
from codeagent.session.store import SessionStore
from test_background_orchestration import DynamicModel, dynamic_factory, early_final_factory, until, BackgroundModel
from test_orchestration import repo_store, events, service_for, spawn_args
from runtime_fixtures import dirty_session, state
from runtime_fixtures import edit_sort, validate
from codeagent.orchestration.lease import session_lease


def product(store, factory):
    notifications = []
    def runner_factory(store, **kwargs):
        return build_agent_runner(store, model_factory=factory, **kwargs)
    meta = store.read_meta()
    meta['runtime_options'] = asdict(RuntimeConfig(delegation_enabled=True))
    store.meta_path.write_text(json.dumps(meta))
    supervisor = ExecutionSupervisor(lambda m, p: notifications.append((m, p)), runner_factory=runner_factory)
    return JsonRpcServer(ProductApplicationService(store.session_root, supervisor=supervisor)), notifications


def call(server, method, **params):
    response = server.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
    assert 'error' not in response, response
    return response['result']


def idle(server, sid, seconds=60):
    until(lambda: server.service.supervisor.state(sid) == 'idle', seconds)
    return call(server, 'session/get', sessionId=sid)


@pytest.mark.parametrize('status', ['completed', 'failed'])
@pytest.mark.parametrize('unresolved', [[], ['done_when 尚无独立 grader 验证'],
    ['done_when 尚无独立 grader 验证', '未找到验证入口'], ['未找到验证入口']])
def test_legacy_grader_notice_is_diagnostic_not_problem(status, unresolved):
    root, rid = 'turn', 'run'
    facts = [
        SessionEvent(type='user_message', turn_id=root),
        SessionEvent(type='agent_command_intent', turn_id=root, payload={
            'action': 'spawn', 'command_id': 'command', 'agent': {'agent_id': 'child', 'spec': {'name': 'review'}},
            'run': {'agent_id': 'child', 'run_id': rid, 'root_turn_id': root, 'message': '核对验证入口',
                    'context_epoch': 1, 'execution_epoch': 1, 'identity': {'tree': 'tree', 'active_root': '/repo'}}}),
        SessionEvent(type='agent_run_finished', turn_id=root, payload={
            'run_id': rid, 'status': status, 'unresolved': unresolved,
            'limitations': ['只读快照'], 'findings': '调查报告'}),
        SessionEvent(type='agent_run_ack', turn_id=root, payload={'run_id': rid}),
        SessionEvent(type='assistant_message', turn_id=root),
    ]
    original = [e.model_dump() for e in facts]
    task = orchestration_view(facts, {'workspace': '/repo'})['tasks'][0]
    run = task['runs'][0]
    assert 'unresolved' not in run
    notes = [reason for reason in unresolved if reason != 'done_when 尚无独立 grader 验证']
    if status == 'completed':
        assert all(note in run['limitations'] for note in notes)
        assert run['error'] is None
    else:
        assert run['error'] == ('; '.join(notes) or None)
    assert ('子任务结论未经独立核验' in run['limitations']) == ('done_when 尚无独立 grader 验证' in unresolved)
    assert run['limitations'][0] == '只读快照'
    assert not run['verified'] and run['status'] == status
    assert task['status'] == ('completed' if status == 'completed' else 'completed_with_warnings')
    assert task['unhandledRuns'] == ([] if status == 'completed' else [rid])
    assert [e.model_dump() for e in facts] == original


def test_rpc_sync_task_followup_and_reopen(repo_store, tmp_path):
    repo, store = repo_store
    server, notifications = product(store, dynamic_factory)
    sid = store.session_id
    call(server, 'session/run', sessionId=sid, message=json.dumps({'fixture_dir': str(tmp_path)}))
    # 主方同步等待时，产品轮询和通知仍可观察到真实子任务。
    until(lambda: bool(events(store, 'agent_run_running')))
    live = call(server, 'session/get', sessionId=sid)
    assert live['executionState'] == 'running'
    assert live['orchestration']['tasks'][0]['runs']
    assert not live['availableActions']['canAcceptChanges']
    # 7次主请求、4轮子任务各3次请求；同步结果不另发迟到报告整合请求。
    detail = idle(server, sid, seconds=90)
    task = detail['turns'][0]['delegation']
    assert task['status'] == 'completed'
    assert len(task['runs']) == 4 and len(detail['orchestration']['agents']) == 2
    assert len({r['agentId'] for r in task['runs'][1:]}) == 1
    assert task['runs'][-1]['action'] == 'send' and task['runs'][-1]['contextEpoch'] is None
    assert all(not r['stale'] for r in task['runs'])
    assert all(r['terminalAck'] and r['resourceState'] == 'reclaimed' and not r['verified'] for r in task['runs'])
    assert not task['waits']
    assert not events(store, 'graph_final_deferred')
    assert not events(store, 'child_report_observation')
    assert task['usage']['requests'] == len(events(store, 'model_request_reserved')) == 19
    assert detail['orchestration']['totalUsage']['requests'] == 19
    commands = [t for item in detail['turns'][0]['items'] if item['type'] == 'activityGroup' for t in item['tools']
                if t['tool'] == 'task']
    assert len(commands) == 4 and all(t['status'] == 'succeeded' and t.get('runId') for t in commands)
    assert any(m == 'session/event' and p['type'] == 'agent_run_running' for m, p in notifications)
    assert (repo / 'README.md').read_text().startswith('# R2')
    assert not detail['validationSummary'] and not detail['availableActions']['canAcceptChanges']
    before = len(events(store, 'model_request_reserved'))
    reopened = ProductApplicationService(store.session_root).get_session(sid)
    assert reopened['orchestration']['tasks'] == detail['orchestration']['tasks']
    assert reopened['pendingApproval'] is None and len(events(store, 'model_request_reserved')) == before


@pytest.mark.parametrize('action', ['stop', 'shutdown'])
def test_stop_or_disconnect_cancels_inflight_parent_children_and_keeps_unknown(repo_store, action):
    _, store = repo_store
    server, _ = product(store, early_final_factory)
    sid = store.session_id
    call(server, 'session/run', sessionId=sid, message='slow stop')
    until(lambda: any(e.payload['agent_id'] != 'main' for e in events(store, 'model_request_reserved')))
    if action == 'stop':
        assert call(server, 'session/stop', sessionId=sid)['executionState'] == 'stopping'
    else:
        server.service.supervisor.shutdown()
    detail = idle(server, sid)
    run = detail['turns'][0]['delegation']['runs'][0]
    assert run['status'] == 'cancelled' and run['terminalAck']
    assert not multiprocessing.active_children()
    assert detail['pendingApproval'] is None and detail['turnBudget']['closed']
    assert detail['orchestration']['totalUsage']['fees'][0]['unknownRequests'] >= 1
    assert events(store, 'execution_stop_acknowledged')[-1].seq > events(store, 'agent_run_ack')[-1].seq
    assert not any(t.name.startswith(('codeagent-graph', 'codeagent-root-watch', 'codeagent-product-events')) for t in threading.enumerate())


@pytest.mark.parametrize('handle', [False, True])
def test_child_failure_is_warning_and_tolerance_is_explicit(repo_store, handle):
    _, store = repo_store
    server, _ = product(store, tolerant_factory if handle else early_final_factory)
    call(server, 'session/run', sessionId=store.session_id, message='failure')
    detail = idle(server, store.session_id)
    task = detail['turns'][0]['delegation']
    assert task['status'] == 'completed_with_warnings'
    assert task['runs'][0]['status'] == 'failed' and not task['runs'][0]['verified']
    assert task['unhandledRuns'] and not task['failureDecisions']
    assert events(store, 'execution_job_finished')[-1].payload['status'] == 'completed'
    assert not detail['validationSummary']


class TolerantModel(BackgroundModel):
    def complete(self, request):
        if request.purpose != 'main_agent':
            return super().complete(request)
        obs = {m['tool_call_id']: json.loads(m['content']) for m in request.messages if m['role'] == 'tool'}
        if 'spawn' not in obs:
            return LLMResponse(tool_calls=[LLMToolCall(call_id='spawn', name='task', arguments={'subagent_type': 'review', 'prompt': 'fail'})])
        report = json.loads(obs['spawn']['content'])
        return LLMResponse(text='Optional review unavailable; no check passed.')


def tolerant_factory(config):
    return TolerantModel()


def test_product_recovery_marks_orphans_without_requests_or_permission(repo_store):
    _, store = repo_store
    server, _ = product(store, early_final_factory)
    # 在 intent 留账后注入进程死亡的持久现场；本测试不声称真实 kill。
    service = service_for(store)
    with session_lease(store) as epoch:
        from codeagent.orchestration.agents import SpawnCommand
        payload = service._submit('spawn', SpawnCommand(**{**spawn_args(), 'execution': 'background'}), epoch)
        store.append_event('execution_job_started', {'job_id': 'lost-job'})
        store.append_event('approval_requested', {'approval_id': 'old', 'job_id': 'lost-job', 'kind': 'tool'})
        store.append_event('execution_stop_requested', {'job_id': 'lost-job'})
    before = len(events(store, 'model_request_reserved'))
    detail = ProductApplicationService(store.session_root).get_session(store.session_id)
    task = detail['turns'][0]['delegation']
    assert task['runs'][0]['runId'] == payload['run']['run_id']
    assert task['runs'][0]['status'] == 'interrupted' and task['runs'][0]['terminalAck']
    assert detail['turnBudget']['closed'] and detail['pendingApproval'] is None
    assert 'permission was not granted' in detail['interruptedControl']
    assert not detail['availableActions']['canContinue']
    assert len(events(store, 'model_request_reserved')) == before
    assert not server.service.supervisor.pending_approval(store.session_id)
    with pytest.raises(ProductServiceError):
        server.service.resolve_approval(store.session_id, 'old', True)


def test_external_owner_blocks_read_model_actions_and_all_product_mutations(repo_store):
    _, store = repo_store
    server, _ = product(store, early_final_factory)
    entered, release = threading.Event(), threading.Event()
    def owner():
        with session_lease(store):
            entered.set()
            assert release.wait(10)
    thread = threading.Thread(target=owner)
    thread.start()
    assert entered.wait(2)
    try:
        detail = call(server, 'session/get', sessionId=store.session_id)
        assert detail['executionState'] == 'external_running'
        assert call(server, 'session/list')['sessions'][0]['executionState'] == 'external_running'
        assert not any(detail['availableActions'][a] for a in ['canRun', 'canContinue', 'canAcceptChanges', 'canDiscardChanges'])
        for action in ['session/run', 'session/stop', 'changes/accept', 'changes/discard']:
            response = server.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': action, 'params': {'sessionId': store.session_id, 'message': 'no'}})
            assert 'error' in response, (action, response)
        assert not events(store, 'model_request_reserved')
    finally:
        release.set()
        thread.join()




def test_usage_deduplicates_main_mirrors_unknown_and_separate_currencies():
    def event(kind, **p):
        return SessionEvent(type=kind, payload=p, turn_id='t')
    facts = [event('model_request_reserved', request_id='r', fee_currency='USD', fee_reserved_microunits=100),
             event('model_usage', request_id='r', usage={'total_tokens': 7}),
             event('model_request_settled', request_id='r', fee_state='settled_known', fee_settled_microunits=7, usage={'total_tokens': 7}),
             event('model_request_reserved', request_id='u', fee_currency='CNY', fee_reserved_microunits=50),
             event('model_request_settled', request_id='u', fee_state='unknown', usage=None)]
    view = usage_view(facts + facts)
    assert view['requests'] == 2 and view['knownTokens'] == 7 and view['unknownUsageRequests'] == 1
    assert view['fees'] == [{'currency': 'USD', 'settledMicrounits': 7, 'reservedMicrounits': 0, 'unknownMicrounits': 0, 'unknownRequests': 0},
                            {'currency': 'CNY', 'settledMicrounits': 0, 'reservedMicrounits': 0, 'unknownMicrounits': 50, 'unknownRequests': 1}]

class StdioHarness:
    def __init__(self, root):
        self.process = subprocess.Popen([sys.executable, '-m', 'codeagent.product.rpc', '--session-root', str(root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.messages = queue.Queue()
        self.notifications = []
        self.next_id = 0
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self):
        for line in self.process.stdout:
            message = json.loads(line)
            if 'id' in message:
                self.messages.put(message)
            else:
                self.notifications.append(message)

    def request(self, method, **params):
        self.next_id += 1
        self.process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': self.next_id, 'method': method, 'params': params}) + '\n')
        self.process.stdin.flush()
        message = self.messages.get(timeout=10)
        assert message['id'] == self.next_id and 'error' not in message, message
        return message['result']

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            self.process.wait(timeout=20)
        self.reader.join(timeout=2)
        self.process.stdout.close()
        self.process.stderr.close()


@pytest.mark.parametrize('shutdown', ['rpc_stop', 'eof', 'sigterm', 'sigkill'])
def test_real_stdio_shutdown_and_process_loss_do_not_resume_authority(tmp_path, shutdown):
    from codeagent.product.demo import create_demo
    repo = create_demo(tmp_path / 'demo')
    session_root = tmp_path / 'sessions'
    rpc = StdioHarness(session_root)
    try:
        caps = rpc.request('initialize')['capabilities']
        assert caps['persistentDelegation'] and caps['shutdownDrainsExecution']
        detail = rpc.request('session/create', workspace=str(repo), provider='fake', model='c3-fixture', orchestrationBackend='langgraph')
        sid = detail['sessionId']
        rpc.request('session/run', sessionId=sid, message='C3 stop')
        store = SessionStore(repo, sid, session_root).load()
        until(lambda: any(e.payload['agent_id'] != 'main' for e in events(store, 'model_request_reserved')), 20)
        before = len(events(store, 'model_request_reserved'))
        transport_pids = set()
        for task in Path('/proc', str(rpc.process.pid), 'task').iterdir():
            try:
                transport_pids.update(int(p) for p in (task / 'children').read_text().split())
            except FileNotFoundError:
                pass
        assert transport_pids
        if shutdown == 'rpc_stop':
            assert rpc.request('session/stop', sessionId=sid)['executionState'] == 'stopping'
        elif shutdown == 'sigterm':
            rpc.process.terminate()
        elif shutdown == 'sigkill':
            rpc.process.kill()
        rpc.close()
        def stopped(pid):
            try:
                return Path('/proc', str(pid), 'stat').read_text().split(') ', 1)[1].startswith('Z')
            except FileNotFoundError:
                return True
        until(lambda: all(stopped(pid) for pid in transport_pids), 5)
        detail = ProductApplicationService(session_root).get_session(sid)
        task = detail['orchestration']['tasks'][0]
        assert detail['executionState'] == 'idle' and detail['pendingApproval'] is None
        assert detail['turnBudget']['closed'] and not detail['availableActions']['canContinue']
        assert task['runs'][0]['status'] in {'cancelled', 'interrupted'} and task['runs'][0]['terminalAck']
        assert len(events(store, 'model_request_reserved')) == before  # 重开没有额外模型请求。
        assert any(e.payload['fee_state'] == 'unknown' for e in events(store, 'model_request_settled'))
        assert not events(store, 'approval_decision')
    finally:
        if rpc.process.poll() is None:
            rpc.process.kill()
        rpc.close()


def test_builtin_fixture_edits_validates_and_delivers_through_rpc(tmp_path):
    from codeagent.product.demo import create_demo
    from runtime_fixtures import HostFixtureBackend
    from codeagent.runtime.sandbox_executor import SandboxedCommandExecutor
    repo = create_demo(tmp_path / 'demo')
    (repo / 'README.md').write_text((repo / 'README.md').read_text() + '\nuser staged\n')
    subprocess.run(['git', 'add', 'README.md'], cwd=repo, check=True)
    (repo / 'README.md').write_text((repo / 'README.md').read_text() + '\nuser unstaged\n')
    original = state(repo)
    command_started, release_command = threading.Event(), threading.Event()
    class GatedBackend(HostFixtureBackend):
        def invocation(self, *args):
            command_started.set()
            if not release_command.wait(30):
                raise RuntimeError('test did not release verification command')
            return super().invocation(*args)
    def factory(store, **kwargs):
        runner = build_agent_runner(store, **kwargs)
        runner.command_executor = SandboxedCommandExecutor(GatedBackend())
        runner.command_service.executor = runner.command_executor
        return runner
    server = JsonRpcServer(ProductApplicationService(tmp_path / 'sessions', supervisor=ExecutionSupervisor(runner_factory=factory)))
    detail = call(server, 'session/create', workspace=str(repo), provider='fake', model='c3-fixture', orchestrationBackend='langgraph')
    sid = detail['sessionId']
    call(server, 'session/run', sessionId=sid, message='C3 edit')
    try:
        assert command_started.wait(60), 'fixture never reached verification'
        live = call(server, 'session/get', sessionId=sid)
        assert live['executionState'] == 'running'
        assert not live['availableActions']['canAcceptChanges']
        assert state(repo)[:3] == original[:3]
        items = live['turns'][0]['items']
        text = [item['text'] for item in items if item['type'] == 'markdown']
        assert text[0] == '我先核对项目说明，再检查实现与验证方式。'
        assert text[-1] == '修改已写入当前项目。现在运行 `python3 verify.py`。'
        assert not any(item.get('final') for item in items)
        tools = [tool for item in items if item['type'] == 'activityGroup' for tool in item['tools']]
        assert next(t for t in tools if t['tool'] == 'run_command')['status'] == 'running'
        assert next(t for t in tools if t['tool'] == 'apply_workspace_edit')['changedFiles'] == ['calculator.py']
        spawn = next(t for t in tools if t['tool'] == 'task')
        assert spawn['status'] == 'succeeded'
        assert spawn['runId'] == live['turns'][0]['delegation']['runs'][0]['runId']
    finally:
        release_command.set()
    detail = idle(server, sid)
    assert state(repo)[:3] == original[:3]
    assert not detail['validationSummary']['appliesToCurrentChanges']
    assert not detail['availableActions']['canAcceptChanges']
    task = detail['turns'][0]['delegation']
    assert len(task['runs']) == 3 and all(r['terminalAck'] for r in task['runs'])
    assert task['runs'][-1]['contextEpoch'] is None
    assert 'return 0 if count' in (repo / 'calculator.py').read_text()
    assert state(repo)[:3] == original[:3]


def test_late_run_event_cannot_reopen_a_terminal_run(repo_store):
    _, store = repo_store
    service = service_for(store)
    report = json.loads(service.spawn(spawn_args()).content)
    facts = store.read_events()
    facts += [SessionEvent(type='agent_run_running', turn_id=service.executor.turn_id, payload={'run_id': report['run_id']}),
              SessionEvent(type='agent_run_finished', turn_id=service.executor.turn_id, payload={'run_id': report['run_id'], 'status': 'failed'})]
    run = orchestration_view(facts, store.read_meta())['tasks'][0]['runs'][0]
    assert run['status'] == 'completed' and run['terminalAck']




@pytest.mark.parametrize('tool,readonly', [('apply_workspace_edit', False), ('run_command', True)])
def test_reopen_unknown_main_side_effect_preserves_scene_and_blocks_delivery(tmp_path, tool, readonly):
    repo, store, context = dirty_session(tmp_path)
    original = state(repo)
    store.append_event('user_message', {'message': 'interrupted write'})
    with session_lease(store):
        store.append_event('tool_execution_intent', {'operation_key': 'main:' + store.read_meta()['current_turn_id'] + ':tool:0',
            'call': {'call_id': 'unknown-write', 'name': tool, 'arguments': {}}, 'readonly': readonly})
        (context.active_root / 'unknown-partial.txt').write_text('preserve this scene')
    service = ProductApplicationService(store.session_root)
    detail = service.get_session(store.session_id)
    assert store.workspace_state().value == 'recovery_required'
    assert detail['globalAttention']['kind'] == 'recovery'
    assert not detail['availableActions']['canAcceptChanges'] and detail['availableActions']['canRun']
    from codeagent.config import ModelConfig
    from codeagent.runtime.approval import AutoApprovalGate
    from codeagent.runtime.atomic_edit import AtomicEditService
    from test_delegation import final_factory
    runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False), model_factory=final_factory)
    assert runner.run_turn('只读调查，不重放旧操作').status == 'completed'
    denied = AtomicEditService(context, store).apply_workspace_edit({'operations': [{'op': 'create_file', 'path': 'new.txt', 'content': 'x'}]})
    assert denied.error_code == 'recovery_required' and not (repo / 'new.txt').exists()
    assert (context.active_root / 'unknown-partial.txt').read_text() == 'preserve this scene'
    assert state(repo)[:3] == original[:3]
    assert len(events(store, 'model_request_reserved')) == 1


def test_stop_after_durable_final_keeps_terminal_result(tmp_path):
    from codeagent.runtime.runner import RunnerOutput
    from test_product_execution import _wait_until
    final, release = threading.Event(), threading.Event()
    class Runner:
        def __init__(self, store, **kwargs):
            self.store = store
        def run_turn(self, message):
            self.store.append_event('user_message', {'message': message})
            self.store.append_event('assistant_message', {'message': 'already final'})
            final.set()
            assert release.wait(5)
            return RunnerOutput(status='completed', final_text='already final')
    workspace = tmp_path / 'repo'
    workspace.mkdir()
    service = ProductApplicationService(tmp_path / 'sessions', supervisor=ExecutionSupervisor(runner_factory=Runner))
    sid = service.create_session(workspace)['sessionId']
    service.run_session(sid, 'task')
    assert final.wait(2)
    service.stop_session(sid)
    release.set()
    assert _wait_until(lambda: service.supervisor.state(sid) == 'idle')
    store = service._load(sid)
    assert events(store, 'execution_job_finished')[-1].payload['status'] == 'completed'
    assert not events(store, 'turn_terminated')


def test_repeated_sigterm_during_eof_cleanup_does_not_interrupt_join(tmp_path):
    script = tmp_path / 'slow_shutdown.py'
    script.write_text('''import sys, time
from pathlib import Path
from codeagent.product.execution import ExecutionSupervisor
from codeagent.product.rpc import serve
original = ExecutionSupervisor.shutdown
def shutdown(self):
    Path(sys.argv[1]).touch()
    time.sleep(.4)
    original(self)
    Path(sys.argv[2]).touch()
ExecutionSupervisor.shutdown = shutdown
serve(session_root=sys.argv[3])
''')
    entered, completed = tmp_path / 'entered', tmp_path / 'completed'
    process = subprocess.Popen([sys.executable, str(script), str(entered), str(completed), str(tmp_path / 'sessions')], stdin=subprocess.PIPE)
    try:
        process.stdin.close()  # EOF 启动清理。
        until(entered.exists)
        process.terminate()  # 客户端追加的 SIGTERM 不得打断 join。
        process.wait(timeout=5)
        assert process.returncode == 0 and completed.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.parametrize('tolerate', [False, True])
def test_builtin_fixture_failure_retains_parent_limitations(repo_store, tolerate):
    root, store = repo_store
    service = ProductApplicationService(store.session_root)
    detail = service.create_session(root, provider='fake', model='c3-fixture', delegation_enabled=True)
    sid = detail['sessionId']
    service.run_session(sid, 'C3 failure' + (' tolerate' if tolerate else ''))
    until(lambda: service.supervisor.state(sid) == 'idle', 30)
    task = service.get_session(sid)['orchestration']['tasks'][0]
    assert task['status'] == 'completed_with_warnings'
    assert not task['failureDecisions'] and task['unhandledRuns']


@pytest.mark.parametrize('reason,status', [('runtime_error', 'failed'), ('deadline', 'timed_out'), ('cancelled', 'stopped')])
def test_parent_terminal_projection_preserves_failure_and_stop_reasons(reason, status):
    facts = [SessionEvent(type='user_message', turn_id='t', payload={'message': 'task'}),
             SessionEvent(type='agent_wait_started', turn_id='t', payload={'wait_id': 'w', 'run_ids': ['r'], 'mode': 'all'}),
             SessionEvent(type='turn_terminated', turn_id='t', payload={'reason': reason})]
    task = orchestration_view(facts, {})['tasks'][0]
    assert task['status'] == status and task['reason'] == reason
    assert task['waits'][0]['status'] == 'interrupted'


def test_slice_cleanup_and_explicit_budget_increase_restore_parent_progress():
    facts = [SessionEvent(type='user_message', turn_id='t', payload={'message': 'task'}),
             SessionEvent(type='agent_wait_started', turn_id='t', payload={'wait_id': 'w', 'run_ids': ['r'], 'mode': 'all'}),
             SessionEvent(type='agent_root_stopping', turn_id='t', payload={'reason': 'slice_exhausted'}),
             SessionEvent(type='execution_slice_exhausted', turn_id='t')]
    task = orchestration_view(facts, {})['tasks'][0]
    assert task['status'] == 'running' and 'reason' not in task
    assert task['waits'][0]['status'] == 'interrupted'
    facts += [SessionEvent(type='turn_budget_exhausted', turn_id='t')]
    assert orchestration_view(facts, {})['tasks'][0]['status'] == 'budget_waiting'
    facts += [SessionEvent(type='turn_budget_increased', turn_id='t')]
    assert orchestration_view(facts, {})['tasks'][0]['status'] == 'running'


def test_transport_reads_response_arriving_between_poll_and_exit_check(repo_store, monkeypatch):
    from codeagent.runtime.request_executor import RequestExecutor
    from codeagent.model_gateway.base import ModelRequest, TokenUsage
    from codeagent.config import ModelConfig
    from codeagent.model_gateway.factory import build_model_client
    _, store = repo_store
    store.append_event('user_message', {'message': 'transport race'})
    budget = RequestExecutor(store, RuntimeConfig(cost_per_token_microunits=1), ModelConfig(), build_model_client, lambda: False)
    class Receiver:
        polls = 0
        def poll(self, timeout):
            self.polls += 1
            # 首次 poll 后 response 已入管道，紧接着进程正常退出。
            return self.polls > 1
        def recv(self):
            return 'response', LLMResponse(text='arrived', usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2)).model_dump()
        def close(self):
            pass
    class Sender:
        def close(self):
            pass
    class Process:
        pid = 1
        joined = False
        def start(self):
            pass
        def is_alive(self):
            return False
        def join(self, timeout=None):
            self.joined = True
        def close(self):
            pass
    receiver, process = Receiver(), Process()
    class Context:
        def Pipe(self, duplex=False):
            return receiver, Sender()
        def Process(self, **kwargs):
            return process
    monkeypatch.setattr('codeagent.runtime.request_executor.multiprocessing.get_context', lambda _: Context())
    response = budget.complete(ModelRequest(messages=[], max_tokens=10), operation_key='same-request')
    assert response.text == 'arrived' and process.joined
    assert len(events(store, 'model_request_reserved')) == 1
    assert len(events(store, 'model_response')) == 1
    assert events(store, 'model_request_settled')[0].payload['fee_state'] == 'settled_known'


@pytest.mark.parametrize('edit_ok,validation_ok,expected', [
    (True, True, '已修复除零情况，`python3 verify.py` 验证通过。'),
    (True, False, '已生成修改，但验证未通过。'),
    (False, True, '未能修改 calculator.py，错误已保留在工作记录中。'),
])
def test_fixture_response_reports_observed_edit_and_validation_result(edit_ok, validation_ok, expected):
    import hashlib
    from codeagent.model_gateway.base import ModelRequest, ModelTool
    from codeagent.model_gateway.orchestration_fixture import OrchestrationFixture
    users = [{'role': 'user', 'content': 'C3 edit'}]
    prefix = 'c3-' + hashlib.sha256(json.dumps(users, sort_keys=True).encode()).hexdigest()[:10] + '-'
    outputs = {
        'spawn': {'task_id': 'a', 'run_id': 'r1', 'status': 'completed'}, 'parent-read': {},
        'follow': {'run_id': 'r2'}, 'read-code': {}, 'edit': {}, 'validate': {}, 'refresh': {},
    }
    messages = users + [{'role': 'tool', 'tool_call_id': prefix + name, 'content': json.dumps({
        'ok': edit_ok if name == 'edit' else validation_ok if name == 'validate' else True,
        'content': json.dumps(output),
    })} for name, output in outputs.items()]
    result = OrchestrationFixture().complete(ModelRequest(messages=messages, tools=[
        ModelTool(name='task', description='', parameters={}),
    ]))
    assert result.text == expected
    assert 'protocol fixture' not in result.text and 'refresh' not in result.text


def test_rpc_foreground_child_is_inspectable_before_tool_receipt(repo_store, monkeypatch):
    from codeagent.orchestration.agents import ChildAdapter
    from test_orchestration import conversation_factory
    _, store = repo_store
    started, release = threading.Event(), threading.Event()
    original = ChildAdapter._call

    def gated(adapter, tool):
        if tool.name == 'read_file' and not started.is_set():
            started.set()
            if not release.wait(40):
                raise RuntimeError('test did not release child tool')
        return original(adapter, tool)

    monkeypatch.setattr(ChildAdapter, '_call', gated)
    server, _ = product(store, conversation_factory)
    sid = store.session_id
    call(server, 'session/run', sessionId=sid, message='PARENT_SECRET inspect child')
    try:
        assert started.wait(40)
        live = call(server, 'session/get', sessionId=sid)
        run = live['turns'][0]['delegation']['runs'][0]
        parent = next(tool for item in live['turns'][0]['items'] for tool in item.get('tools', []) if tool['tool'] == 'task')
        assert parent['runId'] == run['runId'] and parent['status'] == 'running' and 'result' not in parent
        assert run['execution'] == 'foreground' and run['status'] == 'running'
        assert run['currentActivity'] == {'tool': 'read_file', 'argumentSummary': 'README.md · lines 1–3', 'status': 'running'}
        assert run['items'][0]['tools'][0]['status'] == 'running'
        assert not live['availableActions']['canAcceptChanges'] and live['pendingApproval'] is None
        assert 'PARENT_SECRET' not in json.dumps(run)
    finally:
        release.set()
        final = idle(server, sid, seconds=90)
    first, follow = final['turns'][0]['delegation']['runs']
    assert first['runId'] == run['runId'] and first['agentLabel'] == follow['agentLabel']
    assert first['items'][-1]['final'] and follow['items'][-1]['final']
    before = len(events(store, 'model_request_reserved'))
    reopened = ProductApplicationService(store.session_root).get_session(sid)
    assert reopened['turns'][0]['delegation'] == final['turns'][0]['delegation']
    assert len(events(store, 'model_request_reserved')) == before
