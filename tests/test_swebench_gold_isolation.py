import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from runtime_fixtures import git, make_bug_repo


def script(name):
    path = Path(__file__).resolve().parents[1] / 'benchmarks/swebench' / (name + '.py')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cache_fixture(tmp_path):
    repo = make_bug_repo(tmp_path)
    head = git(repo, 'rev-parse', 'HEAD')
    cache = tmp_path / 'cache'
    target = cache / 'sources/one/frozen'
    target.mkdir(parents=True)
    repo.rename(target / 'source')
    source = target / 'source'
    manifest = {'prepared_head': head, 'prepared_tree': git(source, 'rev-parse', 'HEAD^{tree}'),
                'dataset_base_commit': head, 'image_digest': 'fixture'}
    (target / 'manifest.json').write_text(json.dumps(manifest))
    return cache, source


def contains(repo, oid):
    return subprocess.run(['git', 'cat-file', '-e', oid], cwd=repo, capture_output=True).returncode == 0


def test_container_preflight_does_not_load_gold_and_undo_leaves_source_unchanged(tmp_path, monkeypatch):
    from test_swebench_harness import SourcePreparer
    source = make_bug_repo(tmp_path)
    preparer = SourcePreparer(source)
    module = script('check_repository_preflight')
    class Factory:
        def __init__(self, *args, **kwargs): pass
        def open_workspace(self, task, root):
            from codeagent.runtime.command import CommandResult
            workspace = preparer.open_workspace(task, root)
            workspace.command_executor.execute = lambda *args: CommandResult(
                exit_code=0, stdout='probe passed', stderr='', payload_started=True, process_tree_stopped=True)
            return workspace
    monkeypatch.setattr(module, 'SWEbenchSourcePreparer', Factory)
    loader = SimpleNamespace(load_metadata=lambda _: {'image':'fixture','base_commit':git(source,'rev-parse','HEAD')})
    result = module.check_task('one', loader, tmp_path / 'output')
    assert result['passed'], result
    assert result['gold_objects_isolated'] and result['source_unchanged'] and result['container_removed']
    assert not (source/'__codeagent_environment_probe__.txt').exists()


def test_pristine_cache_excludes_private_refs_and_unreachable_objects(tmp_path):
    cache, source = cache_fixture(tmp_path)
    baseline = git(source, 'rev-parse', 'HEAD')
    (source / 'oracle.txt').write_text('private answer\n')
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'private gold')
    private_commit = git(source, 'rev-parse', 'HEAD')
    private_blob = git(source, 'rev-parse', 'HEAD:oracle.txt')
    git(source, 'update-ref', 'refs/private/gold', private_commit)
    git(source, 'reset', '--hard', baseline)
    dangling = subprocess.check_output(['git', 'hash-object', '-w', '--stdin'], cwd=source, input=b'unreachable answer').decode().strip()
    target = tmp_path / 'pristine'
    script('prepare_pristine_cache').prepare('one', cache, target)
    clone = target / 'sources/one/frozen/source'
    assert git(clone, 'rev-parse', 'HEAD') == baseline
    assert all(not contains(clone, oid) for oid in (private_commit, private_blob, dangling))
    assert not (clone / '.git/objects/info/alternates').exists()
