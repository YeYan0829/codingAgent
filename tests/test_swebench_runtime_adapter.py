"""新Runtime测评接入：独立输入、真实工具、完整预测补丁与历史保全。"""
import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from codeagent.benchmark.swebench_harness import SWEbenchHarness, SWEbenchTask
from codeagent.benchmark.swebench_source import SWEbenchTaskSource
from codeagent.benchmark.workspace import prepare_swebench_workspace, prediction_patch
from codeagent.benchmark.trajectory import analyze_trajectory, load_events
from codeagent.benchmark.limits import ExperimentLimits
from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.model_gateway.base import BaseModelClient, LLMResponse, LLMToolCall
from test_swebench_harness import SourcePreparer, Grader
from test_swebench_batch import make_runner, FakeHarness
from runtime_fixtures import git


def repository(root):
    root.mkdir()
    git(root, 'init', '-q')
    git(root, 'config', 'user.name', 'Adapter Test')
    git(root, 'config', 'user.email', 'test@example.invalid')
    return root


def commit(root):
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'fixture input')


@pytest.fixture
def prepared(tmp_path):
    dependency = repository(tmp_path / 'dependency')
    (dependency / 'data.py').write_text('VALUE = 7\n')
    commit(dependency)
    source = repository(tmp_path / 'source')
    (source / '.gitignore').write_text('*.so\n_version.py\n.env\n')
    (source / 'bug.py').write_text('BROKEN = True\n')
    (source / 'removed.txt').write_text('old\n')
    git(source, '-c', 'protocol.file.allow=always', 'submodule', 'add', str(dependency), 'vendor/dependency')
    commit(source)
    (source / 'native.so').write_bytes(b'\x7fELF\x00same-image-extension')
    (source / '_version.py').write_text('VERSION = "prepared"\n')
    (source / '.env').write_text('PRIVATE=must-not-copy\n')
    return SourcePreparer(source).prepare(SWEbenchTaskSource('owner__repo-1', 'fixture/image', git(source, 'rev-parse', 'HEAD')))


def test_workspace_preserves_submodules_native_dependencies_and_source_index(prepared, tmp_path):
    source = prepared.source_root
    index = (source / '.git/index').read_bytes()
    destination = tmp_path / 'attempt'
    artifacts = prepare_swebench_workspace(prepared, destination)
    assert (destination / '.git').is_dir() and not (destination / '.git/worktrees').exists()
    assert (destination / 'vendor/dependency/data.py').read_text() == 'VALUE = 7\n'
    assert (destination / 'native.so').read_bytes() == (source / 'native.so').read_bytes()
    assert (destination / '_version.py').read_bytes() == (source / '_version.py').read_bytes()
    assert not (destination / '.env').exists()
    assert {r['path'] for r in artifacts['copied']} == {'native.so', '_version.py'}
    assert (source / '.git/index').read_bytes() == index
    assert git(destination, 'rev-parse', 'HEAD') == prepared.prepared_head
    assert not git(destination, 'status', '--porcelain')


def add_private_objects(root):
    blob = subprocess.check_output(['git', 'hash-object', '-w', '--stdin'], cwd=root, input=b'private answer').decode().strip()
    tree = subprocess.check_output(['git', 'mktree'], cwd=root, input=f'100644 blob {blob}\tanswer.txt\n'.encode()).decode().strip()
    private = subprocess.check_output(['git', 'commit-tree', tree, '-p', 'HEAD'], cwd=root, input=b'private\n').decode().strip()
    git(root, 'update-ref', 'refs/heads/private-answer', private)
    return blob


def test_runtime_clone_excludes_other_refs_and_unreachable_objects_including_submodule(prepared, tmp_path):
    source = prepared.source_root
    # 子模块clone没有继承本机提交者配置，只有本测试写私有对象时设置。
    module = source / 'vendor/dependency'
    git(module, 'config', 'user.name', 'Adapter Test')
    git(module, 'config', 'user.email', 'test@example.invalid')
    private = [(source, add_private_objects(source)), (module, add_private_objects(module))]
    destination = tmp_path / 'attempt'
    prepare_swebench_workspace(prepared, destination)
    for original, oid in private:
        clone = destination / original.relative_to(source)
        result = subprocess.run(['git', 'cat-file', '-e', oid], cwd=clone, capture_output=True)
        assert result.returncode != 0
        assert not (clone / '.git/objects/info/alternates').exists()


def test_prediction_roundtrip_includes_long_binary_new_deleted_files_without_index_changes(prepared, tmp_path):
    destination = tmp_path / 'attempt'
    prepare_swebench_workspace(prepared, destination)
    index = (destination / '.git/index').read_bytes()
    (destination / 'bug.py').write_text('BROKEN = False\n')
    (destination / 'removed.txt').unlink()
    (destination / 'long.txt').write_text('新增正文\n' * 20000)
    (destination / 'binary.dat').write_bytes(bytes(range(256)) * 400)
    (destination / 'executable.sh').write_text('#!/bin/sh\nexit 0\n')
    (destination / 'executable.sh').chmod(0o755)
    patch = prediction_patch(destination)
    assert len(patch) > 65536 and b'native.so' not in patch and b'_version.py' not in patch
    assert (destination / '.git/index').read_bytes() == index
    target = tmp_path / 'apply'
    prepare_swebench_workspace(prepared, target)
    subprocess.run(['git', 'apply', '--binary', '-'], cwd=target, input=patch, capture_output=True, check=True)
    for name in ('bug.py', 'long.txt', 'binary.dat', 'executable.sh'):
        assert (target / name).read_bytes() == (destination / name).read_bytes()
    assert (target / 'executable.sh').stat().st_mode & 0o111
    assert not (target / 'removed.txt').exists()


def test_workspace_rejects_mismatching_prepared_tree_before_model(prepared, tmp_path):
    with pytest.raises(ValueError, match='输入树'):
        prepare_swebench_workspace(replace(prepared, prepared_tree='0' * 40), tmp_path / 'attempt')


class AdapterModel(BaseModelClient):
    def complete(self, request):
        observations = [m for m in request.messages if m['role'] == 'tool']
        def call(name, args):
            return LLMResponse(tool_calls=[LLMToolCall(call_id=f'call-{len(observations)}', name=name, arguments=args)])
        if request.purpose == 'readonly_child':
            assert {t.name for t in request.tools} == {'list_dir', 'show_tree', 'read_file', 'find_files', 'search_text', 'read_observation'}
            if not observations:
                return call('read_file', {'path': 'vendor/dependency/data.py'})
            assert 'VALUE = 7' in observations[-1]['content']
            return LLMResponse(text='dependency available')
        if not observations:
            return call('task', {'subagent_type': 'explore', 'prompt': '读取vendor/dependency/data.py'})
        if len(observations) == 1:
            return call('edit_file', {'path': 'bug.py', 'edits': [{'old_text': 'True', 'new_text': 'False'}]})
        if len(observations) == 2:
            return call('run_command', {'command': 'fixture command', 'purpose': 'validation'})
        assert all(json.loads(m['content'])['ok'] for m in observations)
        return LLMResponse(text='done')


def adapter_factory(config):
    assert config.provider == 'fake'
    return AdapterModel()


@pytest.mark.parametrize('failure_stage', [None, 'patch', 'grader'])
def test_harness_runs_new_main_child_tools_and_command_then_exports_current_patch(prepared, tmp_path, monkeypatch, failure_stage):
    from test_swebench_harness import LocalWorkspace
    created = []
    original = SourcePreparer.open_workspace
    def open_workspace(self, task, root):
        workspace = original(self, task, root)
        created.append(workspace)
        return workspace
    monkeypatch.setattr(SourcePreparer, 'open_workspace', open_workspace)
    grader = Grader()
    task = SWEbenchTask('owner__repo-1', 'fixture/image', prepared.prepared_head, 'fix bug')
    if failure_stage:
        def fail(*args):
            if failure_stage == 'grader':
                assert created[0].closed
            raise RuntimeError('injected ' + failure_stage + ' failure')
        if failure_stage == 'patch':
            monkeypatch.setattr(LocalWorkspace, 'prediction_patch', fail)
        else:
            monkeypatch.setattr(grader, 'grade', fail)
        with pytest.raises(RuntimeError, match='injected'):
            SWEbenchHarness(SourcePreparer(prepared.source_root), grader, model_factory=adapter_factory).run(
                task, AdapterModel(), ModelConfig(), tmp_path / 'runs', runtime_config=RuntimeConfig(delegation_enabled=True))
        assert created[0].closed
        assert list((tmp_path / 'runs').glob('*/sessions/**/events.jsonl'))
        return
    result = SWEbenchHarness(SourcePreparer(prepared.source_root), grader, model_factory=adapter_factory).run(
        task, AdapterModel(), ModelConfig(), tmp_path / 'runs', runtime_config=RuntimeConfig(delegation_enabled=True))
    assert result.agent_status == 'completed' and result.oracle_passed
    assert result.patch_present
    assert 'BROKEN = False' in grader.prediction['model_patch']
    assert 'from-container.txt' in grader.prediction['model_patch']
    assert len(created) == 1 and created[0].closed
    root = Path(result.prediction_path).parent
    assert 'validation_passed' not in json.loads((root / 'result.json').read_text())
    assert (prepared.source_root / 'bug.py').read_text() == 'BROKEN = True\n'
    assert not (prepared.source_root / 'from-container.txt').exists()
    events = load_events(next((root / 'sessions').rglob('events.jsonl')))
    trajectory = analyze_trajectory(events, {'agent_status': result.agent_status, 'patch_present': True})
    assert trajectory['edit_count'] == 1 and trajectory['first_edit_step'] == 2
    assert trajectory['validation_count'] == trajectory['validation_success_count'] == 1
    assert trajectory['child_tool_result_count'] == 1
    assert not any(e.type in {'candidate_frozen', 'changes_applied', 'validation_completed'} for e in events)


def test_limited_delegation_counts_child_dispatches_in_same_experiment(prepared, tmp_path):
    task = SWEbenchTask('owner__repo-1', 'fixture/image', prepared.prepared_head, 'fix bug')
    result = SWEbenchHarness(SourcePreparer(prepared.source_root), Grader(), model_factory=adapter_factory).run(
        task, AdapterModel(), ModelConfig(), tmp_path / 'runs', runtime_config=RuntimeConfig(delegation_enabled=True),
        experiment_limits=ExperimentLimits(max_requests=2))
    assert result.agent_status == 'experiment_request_limit'
    state = json.loads((Path(result.prediction_path).parent / 'experiment-limits.json').read_text())
    assert len(state['requests']) == 2
    # 一个主请求、一个子请求已发出；下一子请求在SDK前被阻止。
    assert not result.patch_present


def test_injected_main_cannot_leave_experimental_children_uncontrolled(prepared, tmp_path):
    task = SWEbenchTask('owner__repo-1', 'fixture/image', prepared.prepared_head, 'fix bug')
    with pytest.raises(ValueError, match='model_factory'):
        SWEbenchHarness(SourcePreparer(prepared.source_root), Grader()).run(
            task, AdapterModel(), ModelConfig(), tmp_path / 'runs', runtime_config=RuntimeConfig(delegation_enabled=True),
            experiment_limits=ExperimentLimits(max_requests=2))
    assert not (tmp_path / 'runs').exists()


def test_resume_keeps_existing_trajectory_instead_of_reanalyzing_it(tmp_path):
    first = make_runner(tmp_path, FakeHarness()).run('preserve')
    state = next((tmp_path / 'out/preserve/tasks').glob('*/task-state.json'))
    trajectory = Path(json.loads(state.read_text())['result']['trajectory_path'])
    original = json.loads(trajectory.read_text())
    original['historical_analysis_marker'] = 'must stay byte-identical'
    trajectory.write_text(json.dumps(original, indent=3))
    digest = hashlib.sha256(trajectory.read_bytes()).hexdigest()
    harness = FakeHarness()
    second = make_runner(tmp_path, harness).run('preserve')
    assert not harness.calls and first['completed_tasks'] == second['completed_tasks']
    assert hashlib.sha256(trajectory.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize('failure', ['timeout', 'malformed', 'exec_error'])
def test_container_tool_unknown_transport_closes_task_without_replay(monkeypatch, failure):
    import threading
    from types import SimpleNamespace
    from codeagent.benchmark.swebench_container import SWEbenchContainerWorkspace
    workspace = SWEbenchContainerWorkspace.__new__(SWEbenchContainerWorkspace)
    workspace.lock = threading.RLock()
    workspace.closed = False
    workspace.container_id = 'owned-fixture-container'
    workspace.egress = None
    removed, dispatched = [], []
    workspace.backend = SimpleNamespace(_call=lambda args, timeout: removed.append(args))
    def transport(argv, **kwargs):
        dispatched.append(argv)
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(argv, 150)
        return subprocess.CompletedProcess(argv, 1 if failure == 'exec_error' else 0,
                                           stdout=b'{broken', stderr=b'fixture transport failed')
    monkeypatch.setattr('codeagent.benchmark.swebench_container.subprocess.run', transport)
    with pytest.raises(RuntimeError, match='未自动重试'):
        workspace.rpc('write', {'path':'probe.txt','content':''})
    assert workspace.closed and workspace.container_id is None
    assert len(dispatched) == 1
    assert removed == [['rm','--force','--volumes','owned-fixture-container']]


def test_native_command_cleanup_failure_is_not_reported_as_success(tmp_path):
    import stat
    import threading
    from types import SimpleNamespace
    from runtime_fixtures import execution_session
    from codeagent.benchmark.swebench_container import ContainerCommandExecutor
    from codeagent.benchmark.swebench_executor import DockerExecutionOutcome
    from codeagent.runtime.command_service import CommandService
    from codeagent.runtime.approval import AutoApprovalGate
    from codeagent.runtime.artifacts import CommandArtifactStore
    _, store, context = execution_session(tmp_path)
    workspace = SimpleNamespace(lock=threading.RLock(), closed=False, container_id='fixture',
        image=SimpleNamespace(instance_id='owner__repo-1'), package_import=None,
        rpc=lambda *args, **kwargs: {'st_mode':stat.S_IFDIR|0o755})
    def close():
        workspace.closed = True
        workspace.container_id = None
    workspace.close = close
    def execute(container, argv, stdout, stderr, *args):
        stdout.write_text('payload returned zero\n')
        stderr.write_text('')
        return DockerExecutionOutcome(0,False,False,False,False)
    workspace.backend = SimpleNamespace(execute=execute)
    service = CommandService(context,AutoApprovalGate(False),ContainerCommandExecutor(workspace),store,CommandArtifactStore(store))
    result = service.run_command({'command':'fixture'})
    assert not result.ok and result.error_code == 'sandbox_setup_failed', result.model_dump()
    assert workspace.closed and workspace.container_id is None
    assert '回收未确认' in result.metadata['diagnostic']
