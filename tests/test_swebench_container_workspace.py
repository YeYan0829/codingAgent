"""原生SWE容器的免费验收；只用已有镜像，绝不调用模型/下载依赖。"""
import base64
from dataclasses import replace
import json
import os
from pathlib import Path
import threading

import pytest

from codeagent.benchmark.swebench_source import SWEbenchSourcePreparer, SWEbenchTaskSource
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.command_service import CommandService
from codeagent.session.store import SessionStore
from codeagent.tools.fs_read import build_fs_tools
from codeagent.tools.git_read import build_git_tools


pytestmark = pytest.mark.skipif(os.environ.get("CODEAGENT_RUN_SWEBENCH_DOCKER") != "1",
                              reason="设置CODEAGENT_RUN_SWEBENCH_DOCKER=1使用已有镜像免费验收")
IMAGE = "swebench/sweb.eval.x86_64.pylint-dev_1776_pylint-4551:latest"
BASE = "99589b08de8c5a2c6cc61e13a37420a868c80599"


@pytest.fixture
def workspace(tmp_path):
    # prepare_image先inspect已有镜像；测试显式确认存在，禁止回退pull。
    import subprocess
    subprocess.run(["docker", "image", "inspect", IMAGE], check=True, capture_output=True, timeout=15)
    preparer = SWEbenchSourcePreparer(tmp_path / "must-not-create-cache")
    workspace = preparer.open_workspace(SWEbenchTaskSource("pylint-dev__pylint-4551", IMAGE, BASE), tmp_path / "run")
    ident = workspace.container_id
    info = json.loads(workspace.backend._call(["inspect", ident], 15))[0]
    volumes = [m["Name"] for m in info["Mounts"] if m["Type"] == "volume"]
    store = SessionStore(workspace.context.active_root, session_root=tmp_path / "sessions").create()
    commands = CommandService(workspace.context, AutoApprovalGate(False), workspace.command_executor,
                              store, CommandArtifactStore(store))
    try:
        yield workspace, store, commands
    finally:
        workspace.close()
        assert subprocess.run(["docker", "inspect", ident], capture_output=True, timeout=15).returncode != 0
        for volume in volumes:
            assert subprocess.run(["docker", "volume", "inspect", volume], capture_output=True, timeout=15).returncode != 0


def run(commands, command, **kwargs):
    result = commands.run_command({"command": command, **kwargs})
    assert result.ok, result.model_dump()
    return result


def test_original_metadata_cli_and_no_host_source_mount(workspace, monkeypatch):
    w, _, commands = workspace
    info = json.loads(w.backend._call(["inspect", w.container_id], 15))[0]
    assert info["HostConfig"]["NetworkMode"] == "none"
    assert [(m["Destination"], m["RW"]) for m in info["Mounts"] if m["Type"] == "bind"] == [("/opt/codeagent-tools", False)]
    assert not any(w.context.active_root.iterdir())
    assert not (w.root.parent / "must-not-create-cache").exists()
    tools = {t.name: t for t in w.build_read_tools(w.context)}
    def no_host_git(*args):
        pytest.fail('远程项目不能在宿主Session身份目录探测Git祖先')
    monkeypatch.setattr('codeagent.runtime.sandbox_policy._protected_git_paths', no_host_git)
    local = build_fs_tools(w.context) + build_git_tools(w.context)
    assert {t.name: (t.schema, t.description, t.permission_level) for t in local} == {
        t.name: (t.schema, t.description, t.permission_level) for t in tools.values()}
    assert "pyreverse" in tools["read_file"].handler({"path": "pylint.egg-info/entry_points.txt"}).content
    run(commands, "python -c \"from importlib.metadata import distribution; assert distribution('pylint').version\" && pyreverse --help >/tmp/pyreverse-help && test -s /tmp/pyreverse-help")
    assert not w.prediction_patch()


def test_shared_tools_edit_shell_visibility_and_undo_conflict(workspace):
    w, store, commands = workspace
    tools = {t.name: t for t in w.build_read_tools(w.context)}
    editor = w.edit_service(w.context, store)
    result = editor.apply_workspace_edit({"operations": [{"op": "create_file", "path": "probe/demo.py", "content": "VALUE = '原值'\n"}]})
    assert result.ok, result.model_dump()
    result = editor.edit_file({"path": "probe/demo.py", "edits": [{"old_text": "原值", "new_text": "修改"}]})
    assert result.ok, result.model_dump()
    run(commands, "grep 修改 probe/demo.py && printf 'shell-visible\\n' > from-shell.txt")
    assert "shell-visible" in tools["read_file"].handler({"path": "from-shell.txt"}).content
    assert "probe/demo.py" in tools["search_text"].handler({"query": "修改", "path": "probe"}).content
    assert "from-shell.txt" in tools["find_files"].handler({"include": ["*.txt"]}).content
    assert "probe/demo.py" in tools["git_status"].handler({}).content or "probe/" in tools["git_status"].handler({}).content
    # 新服务实例从宿主持久事务日志撤销，项目内容仍只从容器读取。
    editor = w.edit_service(w.context, store)
    assert editor.undo().ok
    assert "原值" in tools["read_file"].handler({"path": "probe/demo.py"}).content
    run(commands, "printf external > probe/demo.py")
    conflict = editor.undo()
    assert conflict.error_code == "undo_conflict"
    assert tools["read_file"].handler({"path": "probe/demo.py"}).content == "external"
    assert list((store.diagnostics_dir / "transactions").glob("*/backup/*"))


def test_long_output_complete_patch_and_index_unchanged(workspace):
    w, store, commands = workspace
    before = w.backend._call(["exec", w.container_id, "sha256sum", "/testbed/.git/index"], 10)
    result = run(commands, "python - <<'PY'\nimport os,sys\nopen('large.txt','w').write('新行\\n'*25000)\nopen('binary.dat','wb').write(bytes(range(256))*400)\nopen('run.sh','w').write('#!/bin/sh\\nexit 0\\n')\nos.chmod('run.sh',0o755)\nsys.stdout.write('X'*90000)\nsys.stderr.write('Y'*80000)\nPY")
    assert result.truncated
    paths = CommandArtifactStore(store).runtime_root
    # 输出文件不取预览，完整内容由观察回读继续提供。
    stdout = next(store.diagnostics_dir.rglob("stdout.log"))
    stderr = next(store.diagnostics_dir.rglob("stderr.log"))
    assert stdout.stat().st_size == 90000 and stderr.stat().st_size == 80000
    patch = w.prediction_patch()
    assert len(patch) > 65536 and b"GIT binary patch" in patch and b"new file mode 100755" in patch
    assert patch.count("新行".encode()) == 25000
    assert w.backend._call(["exec", w.container_id, "sha256sum", "/testbed/.git/index"], 10) == before
    assert (w.root / "patch.diff").read_bytes() == patch
    assert not any(w.context.active_root.iterdir())


def test_timeout_stop_and_background_cleanup_preserve_task_state(workspace):
    w, _, commands = workspace
    ident = w.container_id
    run(commands, "printf persisted > /tmp/retained; setsid bash -c 'sleep 2; echo late > /testbed/late' >/dev/null 2>&1 & printf done")
    run(commands, "sleep 3; test ! -e late; test \"$(cat /tmp/retained)\" = persisted")
    result = commands.run_command({"command": "sleep 30", "timeout_seconds": 1})
    assert result.error_code == "execution_timed_out", result.model_dump()
    stop = threading.Event()
    w.command_executor.cancelled = stop.is_set
    timer = threading.Timer(1, stop.set)
    timer.start()
    try:
        result = commands.run_command({"command": "sleep 20; echo late > cancelled", "timeout_seconds": 30})
    finally:
        timer.cancel(); timer.join()
    assert result.error_code == "execution_cancelled", result.model_dump()
    w.command_executor.cancelled = lambda: False
    run(commands, "test ! -e cancelled; test \"$(cat /tmp/retained)\" = persisted")
    assert w.container_id == ident


def test_paths_git_protection_and_failed_transaction_do_not_write(workspace):
    w, store, commands = workspace
    tools = {t.name: t for t in w.build_read_tools(w.context)}
    editor = w.edit_service(w.context, store)
    assert not tools["read_file"].handler({"path": "../etc/passwd"}).ok
    assert not editor.apply_workspace_edit({"operations": [{"op": "create_file", "path": ".git/evil", "content": "x"}]}).ok
    result = editor.apply_workspace_edit({"operations": [
        {"op": "create_file", "path": "must-not-exist", "content": "x"},
        {"op": "replace_text", "path": "missing", "old_text": "x", "new_text": "y"}]})
    assert not result.ok
    run(commands, "test ! -e must-not-exist; if printf bad >> .git/config 2>/dev/null; then exit 9; fi; ln -s /etc/passwd escape")
    assert not tools["read_file"].handler({"path": "escape"}).ok
    assert not editor.edit_file({"path": "escape", "old_text": "root", "new_text": "bad"}).ok
    result = commands.run_command({"command": "pwd", "cwd": "../"})
    assert not result.ok
    restricted = replace(w.context, denied_paths=("pylint",))
    assert not {t.name: t for t in w.build_read_tools(restricted)}["read_file"].handler({"path": "pylint/__init__.py"}).ok
    # shell没有实现的额外限制明确拒绝，不偷偷回退宿主或放开容器。
    service = CommandService(restricted, AutoApprovalGate(False), w.command_executor, store, CommandArtifactStore(store))
    assert service.run_command({"command": "cat pylint/__init__.py"}).error_code == "path_policy_unsupported"


def test_queued_child_cancel_does_not_wait_for_or_kill_active_parent(workspace):
    from codeagent.runtime.command import ScopedCommandExecutor
    import time
    w, store, commands = workspace
    active = threading.Event()
    original = w.backend.execute
    def execute(*args, **kwargs):
        active.set()
        return original(*args, **kwargs)
    w.backend.execute = execute
    parent = []
    thread = threading.Thread(target=lambda: parent.append(commands.run_command({
        'command':'sleep 3; printf parent > parent-completed.txt'})))
    thread.start()
    try:
        assert active.wait(10)
        child = CommandService(w.context, AutoApprovalGate(False),
            ScopedCommandExecutor(w.command_executor, lambda: True), store, CommandArtifactStore(store))
        started = time.monotonic()
        result = child.run_command({'command':'printf child > cancelled-child.txt'})
        assert result.error_code == 'execution_cancelled', result.model_dump()
        assert time.monotonic() - started < 1.5
    finally:
        thread.join(15)
    assert not thread.is_alive() and parent[0].ok
    run(commands,'test -s parent-completed.txt; test ! -e cancelled-child.txt')


class ContainerWorkflowModel:
    """可控响应覆盖主/只读子/编辑子/命令子，不评价模型解题质量。"""
    def complete(self, request):
        try:
            return self._complete(request)
        except AssertionError:
            import traceback
            traceback.print_exc()
            raise

    def _complete(self, request):
        from codeagent.model_gateway.base import LLMResponse, LLMToolCall, TokenUsage
        observations = [json.loads(m['content']) for m in request.messages if m['role'] == 'tool']
        usage = TokenUsage(input_tokens=10, output_tokens=2, total_tokens=12)
        def call(name, args):
            return LLMResponse(tool_calls=[LLMToolCall(call_id='step-'+str(len(observations)), name=name, arguments=args)],
                               finish_reason='tool_calls', usage=usage)
        assert all(o['ok'] for o in observations), observations
        system = '\n'.join(m['content'] for m in request.messages if m['role'] == 'system')
        assert 'workspace-identity' not in system
        assert '评测取材规则' in system and 'wheel/sdist' in system
        if request.purpose != 'main_agent':
            anchor = next(m['content'] for m in reversed(request.messages) if m['role'] == 'user')
            goal = json.loads(anchor)['goal']
            assert '/testbed' in system
            if not observations:
                if 'READ_PROBE' in goal:
                    assert 'edit_file' not in {t.name for t in request.tools}
                    return call('read_file', {'path':'pylint/__init__.py','end_line':3})
                if 'WRITE_PROBE' in goal:
                    return call('apply_workspace_edit', {'operations':[{
                        'op':'create_file','path':'docker_delegate_probe.py','content':'VALUE = 1\n'}]})
                assert 'RUN_PROBE' in goal
                command = "test -f docker_delegate_probe.py && printf 'VALUE = 2\\n' > docker_delegate_probe.py"
                if '"network_mode": "filtered"' in system:
                    import shlex
                    check = "import urllib.request,urllib.error\ntry: urllib.request.urlopen('https://github.com/',timeout=5)\nexcept urllib.error.URLError as e: assert '403' in str(e),str(e)\nelse: raise AssertionError('child bypassed network policy')"
                    command += ' && python -c ' + shlex.quote(check)
                return call('run_command', {'command': command})
            return LLMResponse(text='实际工具操作成功：'+goal, finish_reason='stop', usage=usage)
        goals = [('code-explorer','READ_PROBE'),('general-purpose','WRITE_PROBE'),('bash-runner','RUN_PROBE')]
        if len(observations) < 3:
            role, goal = goals[len(observations)]
            return call('task', {'subagent_type':role,'prompt':goal})
        if len(observations) == 3:
            return call('read_file', {'path':'docker_delegate_probe.py'})
        assert observations[-1]['content'] == 'VALUE = 2'
        return LLMResponse(text='container workflow complete',finish_reason='stop',usage=usage)


def container_workflow_factory(config):
    assert config.provider == 'fake'
    return ContainerWorkflowModel()


@pytest.mark.parametrize('network', ['off', 'filtered'])
def test_formal_harness_main_and_three_child_roles_use_one_container(tmp_path, network):
    from codeagent.benchmark.swebench_harness import SWEbenchHarness, SWEbenchTask, OracleResult
    from codeagent.config import ModelConfig, RuntimeConfig
    import subprocess
    subprocess.run(['docker','image','inspect',IMAGE],check=True,capture_output=True,timeout=15)
    workspaces = []
    class NativePreparer(SWEbenchSourcePreparer):
        def prepare(self, *args):
            pytest.fail('正式入口不得导出或clone宿主源码')
        def open_workspace(self, task, root):
            w = super().open_workspace(task, root)
            workspaces.append(w)
            return w
    class Grader:
        def grade(self, task, path, run_id):
            assert workspaces[0].container_id is None
            patch = json.loads(path.read_text())[0]['model_patch']
            assert '+VALUE = 2' in patch and 'docker_delegate_probe.py' in patch
            return OracleResult('completed', True, 'offline grader fixture')
    from codeagent.benchmark.swebench_network import SWEbenchNetworkConfig
    result = SWEbenchHarness(NativePreparer(tmp_path/'unused-cache',allow_pull=False,
                             network_config=SWEbenchNetworkConfig(network)), Grader(),
                             model_factory=container_workflow_factory).run(
        SWEbenchTask('pylint-dev__pylint-4551',IMAGE,BASE,'验证三类委派共享同一个项目容器'),
        ContainerWorkflowModel(),ModelConfig(),tmp_path/'runs',runtime_config=RuntimeConfig(delegation_enabled=True))
    assert result.agent_status == 'completed' and result.agent_final == 'container workflow complete'
    assert len(workspaces) == 1 and workspaces[0].closed
    assert not (tmp_path/'unused-cache').exists()
    events = [json.loads(line) for p in (Path(result.prediction_path).parent/'sessions').rglob('events.jsonl')
              for line in p.read_text().splitlines()]
    assert not any(e['type']=='swebench_native_artifacts_prepared' for e in events)
    assert len([e for e in events if e['type']=='edit_transaction']) == 1
    assert len([e for e in events if e['type']=='command_completed']) == 1
    assert len({e['payload']['agent']['agent_id'] for e in events if e['type']=='agent_command_intent'}) == 3


@pytest.mark.parametrize('task,image,base', [
    ('scikit-learn__scikit-learn-11310','swebench/sweb.eval.x86_64.scikit-learn_1776_scikit-learn-11310:latest','553b5fb8f84ba05c8397f26dd079deece2b05029'),
    ('matplotlib__matplotlib-24970','swebench/sweb.eval.x86_64.matplotlib_1776_matplotlib-24970:latest','a3011dfd1aaa2487cce8aa7369475533133ef777'),
    ('django__django-7530','swebench/sweb.eval.x86_64.django_1776_django-7530:latest','f8fab6f90233c7114d642dfe01a4e6d4cb14ee7d'),
])
def test_original_compiled_generated_dependencies_and_legacy_import_hint(tmp_path, task, image, base):
    import subprocess
    subprocess.run(['docker','image','inspect',image],check=True,capture_output=True,timeout=15)
    w = SWEbenchSourcePreparer(tmp_path/'unused-cache',allow_pull=False).open_workspace(
        SWEbenchTaskSource(task,image,base),tmp_path/'native')
    try:
        store = SessionStore(w.context.active_root,session_root=tmp_path/'sessions').create()
        commands = CommandService(w.context,AutoApprovalGate(False),w.command_executor,store,CommandArtifactStore(store))
        package = 'sklearn' if task.startswith('scikit') else 'matplotlib' if task.startswith('matplotlib') else 'django'
        run(commands, 'python -c "import '+package+'; print('+package+'.__file__)"')
        if task.startswith('django'):
            assert w.package_import['workspace_edits_visible_to_tests'] is False
            assert 'PYTHONPATH=/testbed' in w.package_import['note']
        assert not w.prediction_patch()
    finally:
        w.close()
