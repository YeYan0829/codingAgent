"""真实执行器和实际 GLM 请求装配的环境契约；SDK 为无网络动作夹具。"""
from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import subprocess
from types import SimpleNamespace as Obj
import uuid

import pytest

from codeagent.application.session_runtime import build_registry_for_context
from codeagent.benchmark.swebench_executor import SWEbenchDockerCommandExecutor
from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.model_gateway.glm_client import GLMClient
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.bubblewrap import BubblewrapBackend, SandboxUnavailable
from codeagent.runtime.runner import AgentRunner
from codeagent.runtime.sandbox_executor import SandboxedCommandExecutor
from codeagent.session.store import SessionStore
from runtime_fixtures import git, make_bug_repo
from test_swebench_docker_executor import prepared
from conftest import runtime_view


class EnvironmentSDK:
    def __init__(self):
        self.chat = Obj(completions=self)

    def with_options(self, **kwargs):
        assert kwargs == {'max_retries': 0}
        return self

    def create(self, **wire):
        snapshot = runtime_view(wire['messages'])
        root = snapshot['active_workspace']
        environment = snapshot['execution_environment']
        assert root == environment['default_cwd_resolves_to']
        guidance = environment['file_lifetime_guidance']
        command_description = next(t['function']['description'] for t in wire['tools']
                                   if t['function']['name'] == 'run_command')
        assert 'file_lifetime_guidance' in command_description
        if environment['backend'] == 'swebench_docker':
            assert '/tmp、HOME、TMPDIR和安装的依赖在题内保留' in guidance
        else:
            assert '/tmp在每条命令启动时重新创建' in guidance
            assert 'HOME和当前项目内的文件可跨命令保留' in guidance
        observations = [json.loads(m['content']) for m in wire['messages'] if m['role'] == 'tool']
        stage = len(observations)
        previous = observations[-1] if observations else None
        if previous and stage != 9:
            assert previous['ok'] == (stage not in {5, 8}), previous
        task = {'title': 'CLI排序', 'status': 'todo'}
        name = 'run_command'
        if stage == 0:
            name, args = 'update_task_plan', {'tasks': [task]}
        elif stage == 1:
            code = ('import os; from pathlib import Path; '
                    f'assert str(Path.cwd()) == {root + "/checks"!r}; '
                    'Path("../probe-persist.txt").write_text("current-workspace"); '
                    'Path(os.environ["TMPDIR"], "probe-ephemeral").write_text("temporary"); '
                    'print("cwd-and-files-confirmed")')
            args = {'cwd': 'checks', 'command': 'export CODEAGENT_TRANSIENT=temporary; python3 -c ' + shlex.quote(code)}
        elif stage == 2:
            assert previous['metadata']['launch_cwd'] == root + '/checks'
            code = ('import os; from pathlib import Path; '
                    f'assert str(Path.cwd()) == {root!r}; '
                    'assert "CODEAGENT_TRANSIENT" not in os.environ; '
                    f'assert Path(os.environ["TMPDIR"], "probe-ephemeral").exists() == {bool(environment.get("container_state_persists"))!r}; '
                    'p=Path("probe-persist.txt"); assert p.read_text()=="current-workspace"; p.unlink(); '
                    'print("fresh-shell-and-persistent-workspace-confirmed")')
            args = {'command': 'python3 -c ' + shlex.quote(code)}
        elif stage == 3:
            assert previous['metadata']['launch_cwd'] == root
            name, args = 'read_file', {'path': 'sort_utils.py'}
        elif stage in {4, 5}:
            if stage == 5:
                assert previous['error_code'] == 'edit_conflict'
            original = observations[3]
            name, args = 'apply_workspace_edit', {'operations': [{'op': 'replace_text', 'path': 'sort_utils.py',
                'expected_sha256': '0' * 64 if stage == 4 else original['metadata']['sha256'],
                'old_text': 'reverse=True', 'new_text': 'reverse=False'}]}
        elif stage == 6:
            name, args = 'read_file', {'path': 'sort_utils.py'}
        elif stage == 7:
            assert 'reverse=False' in previous['content']
            args = {'purpose': 'validation', 'command':
                    'python3 -c \'raise ImportError("intentional-check")\''}
        elif stage == 8:
            assert previous['metadata']['exit_code'] != 0
            assert 'MUST_NOT_RUN' not in previous['content']
            args = {'purpose': 'validation', 'command': "python3 -m unittest discover -s checks -p 'test_absent_*.py'"}
        elif stage == 9:
            # unittest不同版本空集合返回0或5；如实保留退出码，两者都不能算验收成功。
            assert previous['metadata']['exit_code'] in {0, 5}
            assert previous['metadata']['validation_issue'] == 'no_tests'
            assert previous['ok'] == (previous['metadata']['exit_code'] == 0)
            code = ('import json,subprocess,sys; from sort_utils import sort_values; '
                    'assert sort_values([3,1,2])==[1,2,3]; '
                    '\nfor values in ([3,1,2],[2,1,2],[]):\n'
                    ' output=subprocess.check_output([sys.executable,"cli.py",json.dumps(values)],text=True)\n'
                    ' assert json.loads(output)==sorted(values), output\n'
                    'print("function-and-cli-confirmed")')
            args = {'purpose': 'validation', 'command': 'python3 -c ' + shlex.quote(code)}
        elif stage == 10:
            assert previous['metadata']['exit_code'] == 0
            task.update(status='done', notes='函数与CLI分别通过实际断言')
            name, args = 'update_task_plan', {'tasks': [task]}
        else:
            assert stage == 11 and snapshot['task_plan']['tasks'][0]['status'] == 'done'
            name, args = None, None
        output = os.environ.get('C4_ENVIRONMENT_AUDIT_OUTPUT')
        if output:
            case = environment['backend'] + '-' + ('graph' if any(t['function']['name'] == 'spawn_agent' for t in wire['tools']) else 'legacy')
            dest = Path(output) / case; dest.mkdir(parents=True, exist_ok=True)
            (dest / f'{stage}-{uuid.uuid4().hex}.json').write_text(json.dumps(wire, ensure_ascii=False, indent=2))
        calls = [Obj(id=uuid.uuid4().hex, function=Obj(name=name, arguments=json.dumps(args)))] if name else []
        return Obj(id='offline-environment', choices=[Obj(message=Obj(content='' if name else '当前工作区修改与CLI均已验证', tool_calls=calls),
                                                        finish_reason='tool_calls' if name else 'stop')],
                   usage=Obj(prompt_tokens=100, completion_tokens=20, total_tokens=120))


def environment_factory(config):
    return GLMClient(model=config.model, client=EnvironmentSDK(), reasoning_enabled=True, reasoning_effort='high')


@pytest.mark.parametrize('backend', ['legacy', 'langgraph'])
@pytest.mark.parametrize('executor_kind', ['bubblewrap', 'docker'])
def test_actual_request_edit_execution_and_failure_recovery(tmp_path, backend, executor_kind):
    if executor_kind == 'docker' and os.environ.get('CODEAGENT_RUN_ENVIRONMENT_DOCKER') != '1':
        pytest.skip('设置CODEAGENT_RUN_ENVIRONMENT_DOCKER=1使用已缓存镜像执行，无拉取')
    root = make_bug_repo(tmp_path)
    (root / 'cli.py').write_text('import json,sys\nfrom sort_utils import sort_values\nprint(json.dumps(sort_values(json.loads(sys.argv[1]))))\n')
    (root / 'checks').mkdir(); (root / 'checks' / '.gitkeep').touch()
    git(root, 'add', '.'); git(root, 'commit', '-qm', 'add entry fixture')
    store = SessionStore(root, session_root=tmp_path / 'sessions').create()
    context = store.workspace_context()
    try:
        if executor_kind == 'bubblewrap':
            try:
                BubblewrapBackend().probe(context.active_root)
            except SandboxUnavailable as exc:
                pytest.skip(str(exc))
            executor = SandboxedCommandExecutor()
        else:
            image = 'swebench/sweb.eval.x86_64.sympy_1776_sympy-20590:latest'
            # 先inspect已有镜像；缺失直接失败，绝不通过准备器触发pull。
            details = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image]))[0]
            digest = details['RepoDigests'][0].rsplit('@', 1)[1]
            source = replace(prepared(context), image_ref=image, image_digest=digest)
            executor = SWEbenchDockerCommandExecutor(source)
        config = ModelConfig(provider='glm', model='glm-5.3', max_tokens=8192, reasoning_enabled=True, reasoning_effort='high')
        runner = AgentRunner(store, environment_factory(config), build_registry_for_context(context), AutoApprovalGate(True),
                             config=RuntimeConfig(),
                             model_config=config, workspace_context=context, command_executor=executor, model_factory=environment_factory)
        result = runner.run_turn('修复CLI排序为升序，保留重复和空输入；实际检查当前文件及错误恢复。')
        assert result.status == 'completed', result
        commands = [e.payload for e in store.read_events() if e.type == 'command_completed']
        assert [c['exit_code'] for c in commands[:3]] == [0, 0, 1]
        assert commands[-1]['exit_code'] == 0
        assert commands[-2]['validation_issue'] == 'no_tests'
        assert not any(e.type == 'validation_completed' for e in store.read_events())
        assert len([e for e in store.read_events() if e.type == 'model_usage']) == 12
        assert 'reverse=False' in (root / 'sort_utils.py').read_text()
        assert 'reverse=False' in (context.active_root / 'sort_utils.py').read_text()
        assert not (context.active_root / 'probe-persist.txt').exists()
        output = os.environ.get('C4_ENVIRONMENT_AUDIT_OUTPUT')
        if output:
            dest = Path(output) / f'{executor_kind}-{backend}'; dest.mkdir(parents=True, exist_ok=True)
            (dest / 'events.jsonl').write_text(store.events_path.read_text())
    finally:
        if executor_kind == 'docker' and 'executor' in locals():
            executor.close()
