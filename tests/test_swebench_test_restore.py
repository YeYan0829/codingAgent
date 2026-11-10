"""评分端rename恢复：真实临时Git仓库中重现候选测试修改与官方补丁冲突。"""
import importlib.util
import json
from pathlib import Path
import shlex
import subprocess
import sys
from types import ModuleType

import pytest

from runtime_fixtures import git


def wrapper():
    path = Path(__file__).parents[1] / 'benchmarks/swebench/official_grader_with_test_restore.py'
    spec = importlib.util.spec_from_file_location('test_restore_wrapper', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('old,new', [('tests/old.py', 'tests/new.py'), ('tests/old file.py', 'tests/new file.py')])
def test_real_git_reset_restores_rename_source_and_preserves_production_change(tmp_path, old, new):
    repo = tmp_path / 'repo'; repo.mkdir()
    git(repo, 'init'); git(repo, 'config', 'user.email', 'fixture@localhost'); git(repo, 'config', 'user.name', 'Fixture')
    (repo / 'tests').mkdir()
    (repo / 'implementation.py').write_text('VALUE = 0\n')
    original = ''.join('# stable context %s\n' % i for i in range(12)) + 'assert __import__("implementation").VALUE == 0\n'
    (repo / old).write_text(original)
    (repo / 'tests/keep.py').write_text('# baseline\n')
    git(repo, 'add', '.'); git(repo, 'commit', '-m', 'base')
    base = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'mv', old, new)
    (repo / new).write_text(original.replace('== 0', '== 1'))
    patch = git(repo, 'diff', 'HEAD', '--find-renames=10%') + '\n'
    assert 'rename from ' in patch
    git(repo, 'reset', '--hard', base); git(repo, 'clean', '-fd')
    (repo / 'implementation.py').write_text('VALUE = 1\n')
    (repo / old).write_text(original + '# model regression test\n')
    (repo / 'tests/keep.py').write_text('# model test edit\n')
    reset = f'git checkout {base} {shlex.quote(new)} tests/keep.py\n'
    # 同秒内写回等长源码会命中旧pyc，夹具显式禁用缓存来检查真实当前内容。
    script = 'set -uxo pipefail\n' + reset + "git apply - <<'OFFICIAL_TEST_PATCH'\n" + patch + 'OFFICIAL_TEST_PATCH\n' + f'PYTHONPATH=. {shlex.quote(sys.executable)} -B {shlex.quote(new)}\n'
    broken = subprocess.run(['bash'], cwd=repo, input=script, text=True, capture_output=True)
    assert broken.returncode != 0 and 'patch does not apply' in broken.stderr
    # 已应用的生产修复不能被测试恢复顺带清空。
    repaired, audit = wrapper().repair_eval_script(script, patch, base)
    passed = subprocess.run(['bash'], cwd=repo, input=repaired, text=True, capture_output=True)
    assert passed.returncode == 0, passed.stderr
    assert audit['changed'] and audit['reset_commands'] == 1
    assert (repo / 'implementation.py').read_text() == 'VALUE = 1\n'
    assert (repo / 'tests/keep.py').read_text() == '# baseline\n'
    # 脚本末尾的同类恢复也能恢复来源并清掉基线没有的rename目标。
    clean, _ = wrapper().repair_eval_script(reset, patch, base)
    subprocess.run(['bash', '-c', clean], cwd=repo, check=True, capture_output=True)
    assert (repo / old).read_text() == original and not (repo / new).exists()
    (repo / 'implementation.py').write_text('VALUE = 0\n')
    wrong = subprocess.run(['bash'], cwd=repo, input=repaired, text=True, capture_output=True)
    assert wrong.returncode != 0 and 'AssertionError' in wrong.stderr


def test_all_fixed_50_tasks_only_astropy_changes_and_test_patch_bytes_stay_unchanged():
    root = Path(__file__).parents[1]
    if not (root / 'reference/swe-bench-tasks/tasks').is_dir():
        pytest.skip('需本地固定题库才能核对50题；不自动下载')
    selection = json.loads((root / 'benchmarks/swebench/selections/hal-verified-mini-50.json').read_text())
    module = wrapper(); changed = []
    for row in selection['tasks']:
        task = root / 'reference/swe-bench-tasks/tasks' / row['instance_id']
        script, patch = (task / 'eval.sh').read_text(), (task / 'test.patch').read_text()
        repaired, audit = module.repair_eval_script(script, patch, row['base_commit'])
        if audit['changed']:
            changed.append(row['instance_id'])
            assert audit['reset_commands'] == 2
            assert patch in repaired and patch in script
        else:
            assert repaired == script
    assert changed == ['astropy__astropy-7336']


@pytest.mark.parametrize('path', ['../outside', '/etc/passwd', '.git/config', 'tests/../outside', 'tests/./file', 'tests/file\nother'])
def test_invalid_rename_paths_are_rejected(path):
    with pytest.raises(ValueError):
        wrapper().renamed_paths('rename from ' + json.dumps(path) + '\nrename to tests/new.py\n')


def test_missing_reset_anchor_refuses_silent_compatibility():
    with pytest.raises(ValueError, match='恢复入口'):
        wrapper().repair_eval_script('echo no reset\n', 'rename from tests/a\nrename to tests/b\n', 'a' * 40)


def test_entrypoint_only_changes_spec_script_keeps_ca_wrapper_and_restores_hook(tmp_path, monkeypatch):
    module = wrapper()
    package, harness, utils = (ModuleType(name) for name in ('swebench', 'swebench.harness', 'swebench.harness.utils'))
    package.harness = harness; harness.utils = utils
    seen = []; original = lambda value: seen.append(value) or value
    utils.make_test_spec = original
    for value in (package, harness, utils): monkeypatch.setitem(sys.modules, value.__name__, value)
    patch = 'rename from tests/a\nrename to tests/b\n'
    instance = {'instance_id': 'fixture', 'base_commit': 'a' * 40, 'eval_script': 'git checkout ' + 'a' * 40 + ' tests/b\n', 'test_patch': patch, 'patch': 'private gold unchanged'}
    def run(path, run_name):
        assert path.endswith('official_grader_with_ca.py') and run_name == '__main__'
        assert sys.argv[1:] == ['--ca-bundle', 'frozen-ca', '--ca-sha256', 'hash']
        assert utils.make_test_spec(instance)['test_patch'] == patch
        raise RuntimeError('synthetic grader stop')
    monkeypatch.setattr(module.runpy, 'run_path', run)
    monkeypatch.setattr(sys, 'argv', ['wrapper', '--test-restore-audit', str(tmp_path / 'audit.jsonl'), '--ca-bundle', 'frozen-ca', '--ca-sha256', 'hash'])
    with pytest.raises(RuntimeError): module.main()
    assert utils.make_test_spec is original and sys.argv[0] == 'wrapper'
    assert seen[0]['patch'] == instance['patch'] and instance['eval_script'].startswith('git checkout')
    assert json.loads((tmp_path / 'audit.jsonl').read_text())['changed']
