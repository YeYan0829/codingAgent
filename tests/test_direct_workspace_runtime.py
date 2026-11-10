"""直接项目入口及真实隔离命令，不访问模型Provider。"""
import json
import subprocess
import threading
import time
from pathlib import Path

from typer.testing import CliRunner

from codeagent.application.session_runtime import build_agent_runner
from codeagent.cli import app
from codeagent.config import ModelConfig
from codeagent.model_gateway.base import BaseModelClient, LLMResponse, LLMToolCall
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.atomic_edit import AtomicEditService
from codeagent.runtime.command_service import CommandService
from codeagent.runtime.sandbox_executor import SandboxedCommandExecutor
from codeagent.session.store import SessionStore
from test_direct_edit_undo import setup


class EditCurrent(BaseModelClient):
    def complete(self, request):
        if not any(m['role'] == 'tool' for m in request.messages):
            return LLMResponse(tool_calls=[LLMToolCall(call_id='edit', name='edit_file', arguments={
                'path': 'a.py', 'edits': [{'old_text': 'original', 'new_text': 'current'}]})])
        return LLMResponse(text='done', finish_reason='stop')


def edit_factory(config):
    return EditCurrent()


def commands(store, *, cancelled=None):
    return CommandService(store.workspace_context(), AutoApprovalGate(True),
        SandboxedCommandExecutor(cancelled=cancelled), store, CommandArtifactStore(store))


def test_runtime_edits_current_project_without_upgrade_candidate_or_accept(tmp_path):
    root, store, _ = setup(tmp_path, git=True)
    index = (root / '.git/index').read_bytes()
    runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False), model_factory=edit_factory)
    result = runner.run_turn('修复')
    assert result.status == 'completed', result
    assert (root / 'a.py').read_text() == 'current\n'
    assert store.workspace_context().active_root == root
    assert (root / '.git/index').read_bytes() == index
    assert not any(e.type in {'workspace_preparing', 'workspace_activated', 'validation_completed', 'changes_frozen', 'changes_accepted'} for e in store.read_events())
    assert not any(k in store.read_meta() for k in ('candidate_revision', 'base_commit', 'snapshot_ref'))
    cli = CliRunner().invoke(app, ['undo', store.session_id, '--session-root', str(store.session_root)])
    assert cli.exit_code == 0, cli.output
    assert (root / 'a.py').read_text() == 'original\n'


def test_old_session_runs_current_project_without_missing_worktree_or_old_test_identity(tmp_path):
    root, store, _ = setup(tmp_path)
    store.append_event('user_message', {'message': '旧要求'})
    store.append_event('assistant_message', {'message': '旧答复'})
    old_log = store.events_path.read_bytes()
    meta = store.read_meta()
    meta.pop('workspace_mode')
    old = tmp_path / 'missing-worktree'
    meta.update(workspace_kind='git_worktree', active_workspace=str(old), base_commit='old-hash',
                source_head='old-head', candidate_revision=4, workspace_revision='obsolete-format', workspace_state='changes_active')
    store._write_state(meta)
    restored = SessionStore(root, store.session_id, store.session_root).load()
    runner = build_agent_runner(restored, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False), model_factory=edit_factory)
    assert runner.run_turn('按当前文件修复').status == 'completed'
    assert (root / 'a.py').read_text() == 'current\n'
    assert not old.exists()
    assert restored.events_path.read_bytes().startswith(old_log)
    assert any(e.type == 'legacy_workspace_detached' for e in restored.read_events())


def test_bubblewrap_direct_project_writable_git_and_host_protected(tmp_path):
    root, store, _ = setup(tmp_path, git=True)
    host = tmp_path / 'host-secret'; host.write_text('unchanged')
    index = (root / '.git/index').read_bytes()
    command = ('printf direct > command.out; '
        'if printf changed > /etc/hostname; then exit 31; fi; '
        'if printf changed > .git/config; then exit 32; fi; '
        'test "$(cat command.out)" = direct')
    result = commands(store).run_command({'command': command})
    assert result.ok, result
    assert (root / 'command.out').read_text() == 'direct'
    assert host.read_text() == 'unchanged'
    assert (root / '.git/index').read_bytes() == index
    assert not {'before_subject_tree', 'after_subject_tree', 'before_candidate_revision', 'after_candidate_revision'} & result.metadata.keys()
    assert not any(e.type in {'validation_completed', 'workspace_tainted'} for e in store.read_events())
    edited = AtomicEditService(store.workspace_context(), store).edit_file({'path': 'a.py', 'edits': [{'old_text': 'original', 'new_text': 'after-command'}]})
    assert edited.ok


def test_bubblewrap_stop_terminates_command_before_return_and_no_late_writes(tmp_path):
    root, store, _ = setup(tmp_path)
    stopped = threading.Event()
    timer = threading.Timer(.6, stopped.set); timer.start()
    start = time.monotonic()
    try:
        result = commands(store, cancelled=stopped.is_set).run_command({
            'command': 'while true; do printf x >> running.out; sleep .05; done', 'timeout_seconds': 30})
    finally:
        timer.cancel(); timer.join()
    assert not result.ok and result.error_code == 'execution_cancelled', result
    assert time.monotonic() - start < 5
    assert next(e for e in store.read_events() if e.type == 'command_completed').payload['process_tree_stopped']
    before = (root / 'running.out').read_bytes()
    time.sleep(.15)
    assert (root / 'running.out').read_bytes() == before


def test_bubblewrap_shell_background_writer_cannot_outlive_command(tmp_path):
    root, store, _ = setup(tmp_path)
    result = commands(store).run_command({'command': '(sleep 1; printf late > late.out) & printf done', 'timeout_seconds': 10})
    assert result.ok, result
    time.sleep(1.2)
    assert not (root / 'late.out').exists()


def test_keyboard_stop_closes_turn_and_keeps_prior_changes(tmp_path):
    from codeagent.runtime.runner import AgentRunner
    from codeagent.tools.registry import ToolRegistry
    root, store, _ = setup(tmp_path)
    class Interrupted(BaseModelClient):
        def complete(self, request):
            raise KeyboardInterrupt()
    runner = AgentRunner(store, Interrupted(), ToolRegistry(store.workspace_context()), AutoApprovalGate(False))
    result = runner.run_turn('停止')
    assert result.status == 'stopped'
    assert (root / 'a.py').read_text() == 'original\n'
    assert any(e.type == 'turn_terminated' and e.payload['reason'] == 'user_stop' for e in store.read_events())


def test_new_user_message_can_replace_idle_unfinished_history_without_forced_continue(tmp_path):
    from codeagent.runtime.runner import AgentRunner
    from codeagent.tools.registry import ToolRegistry
    root, store, _ = setup(tmp_path)
    store.append_event('user_message', {'message': '旧要求'})
    store.append_event('assistant_tool_calls', {'tool_calls': [
        {'call_id': 'old-unfinished', 'name': 'edit_file', 'arguments': {'path': 'a.py'}}]})
    store.append_event('turn_budget_exhausted', {'limit': 1})
    class Answer(BaseModelClient):
        def complete(self, request):
            assert [m for m in request.messages if m['role'] == 'user'][-1]['content'] == '新指令'
            return LLMResponse(text='按新指令工作', finish_reason='stop')
    runner = AgentRunner(store, Answer(), ToolRegistry(store.workspace_context()), AutoApprovalGate(False))
    assert runner.run_turn('新指令').status == 'completed'
    assert (root / 'a.py').read_text() == 'original\n'
    closed = [e for e in store.read_events() if e.type == 'tool_denied']
    assert len(closed) == 1 and closed[0].payload['result']['error_code'] == 'new_user_message'


def test_old_snapshot_records_cannot_gate_current_work_or_delete_old_files(tmp_path):
    root, store, _ = setup(tmp_path)
    old = tmp_path / 'legacy-copy'; old.mkdir()
    (old / 'unaccepted.py').write_text('keep old work\n')
    store.append_event('agent_snapshot_created', {'run_id': 'old', 'path': str(old)})
    runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False), model_factory=edit_factory)
    assert runner.run_turn('修改当前项目').status == 'completed'
    assert (root / 'a.py').read_text() == 'current\n'
    assert (old / 'unaccepted.py').read_text() == 'keep old work\n'
    assert not any(e.type == 'agent_snapshot_reclaimed' for e in store.read_events())
