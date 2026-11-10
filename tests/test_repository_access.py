from dataclasses import replace
from pathlib import Path
import hashlib
import subprocess

import pytest

from codeagent.safety.repository_access import RepositoryAccessStore, RepositoryAccessError, session_access_lease, assert_session_access
from codeagent.session.store import SessionStore
from codeagent.tools.fs_read import build_fs_tools
from codeagent.tools.git_read import build_git_tools
from codeagent.tools.registry import ToolRegistry
from codeagent.runtime.atomic_edit import AtomicEditService
from runtime_fixtures import make_bug_repo


def restricted_workspace(tmp_path, ignored=False):
    repo = make_bug_repo(tmp_path)
    (repo / 'private').mkdir()
    (repo / 'private' / 'data.txt').write_text('DENIED_TEST_SENTINEL\n')
    (repo / 'allowed.txt').write_text('allowed\n')
    if ignored:
        (repo/'.gitignore').write_text('.env\n')
        (repo/'.env').write_text('DENIED_TEST_SENTINEL ignored\n')
    subprocess.run(['git', 'add', '.'], cwd=repo, check=True, capture_output=True)
    subprocess.run(['git', 'commit', '-m', 'access fixture'], cwd=repo, check=True, capture_output=True)
    policy = RepositoryAccessStore(repo, tmp_path / 'sessions')
    settings = policy.save(['private/'] + (['.env'] if ignored else []), '0')
    store = SessionStore(repo, session_root=tmp_path / 'sessions').create()
    context = store.workspace_context()
    return repo, policy, settings, store, context


def test_same_policy_blocks_file_tools_edits_git_and_child_materialization(tmp_path):
    repo, policy, _, store, context = restricted_workspace(tmp_path)
    tools = ToolRegistry()
    for tool in build_fs_tools(context) + build_git_tools(context):
        tools.register(tool)
    assert tools.call('read_file', {'path':'allowed.txt'}).ok
    for path in ['private/data.txt', '.git', 'private/../private/data.txt']:
        result = tools.call('read_file', {'path':path})
        assert not result.ok
        assert 'DENIED_TEST_SENTINEL' not in str(result)
    (context.active_root/'private/data.txt').write_text('DENIED_TEST_SENTINEL changed\n')
    (context.active_root/'allowed.txt').write_text('allowed changed\n')
    for name in ['git_diff', 'git_diff_stat', 'git_status']:
        result=tools.call(name,{})
        assert result.ok, result.error
        assert 'private' not in result.content and 'DENIED_TEST_SENTINEL' not in result.content
        assert 'allowed.txt' in result.content
    assert not tools.call('git_diff', {'path':'private/data.txt'}).ok
    for name, args in [('list_dir',{}),('show_tree',{}),('search_text',{'query':'DENIED_TEST_SENTINEL','include_hidden':True}),('find_files',{'include_hidden':True})]:
        result = tools.call(name, args)
        assert result.ok, result.error
        assert 'DENIED_TEST_SENTINEL' not in result.content and 'private' not in result.content
    edits = AtomicEditService(context, store)
    before = (context.active_root/'private/data.txt').read_bytes()
    result = edits.apply_workspace_edit({'operations':[{'op':'replace_text','path':'private/data.txt','expected_sha256':hashlib.sha256(before).hexdigest(),'old_text':'DENIED_TEST_SENTINEL','new_text':'changed'}]})
    assert not result.ok
    assert (context.active_root/'private/data.txt').read_bytes() == before
    assert SessionStore(repo,store.session_id,store.session_root).load().workspace_context().denied_paths==('private',)


def test_policy_changes_invalidate_old_session_and_cannot_race_active_work(tmp_path):
    repo, policy, settings, store, _ = restricted_workspace(tmp_path)
    with session_access_lease(store):
        with pytest.raises(RepositoryAccessError, match='正在执行'):
            policy.save([], settings['revision'])
    cleared = policy.save([], settings['revision'])
    with pytest.raises(RepositoryAccessError, match='新建任务'):
        assert_session_access(store)
    new = SessionStore(repo,session_root=store.session_root).create()
    assert not new.workspace_context().denied_paths
    assert_session_access(new)
    with pytest.raises(RepositoryAccessError, match='其他窗口'):
        policy.save([], settings['revision'])
    assert policy.save([], cleared['revision'])==cleared


def test_empty_and_unconfigured_are_distinct_and_swe_needs_no_setup(tmp_path):
    repo=tmp_path/'repo';repo.mkdir()
    policy=RepositoryAccessStore(repo,tmp_path/'sessions')
    assert policy.read()['configured'] is False
    store=SessionStore(repo,session_root=tmp_path/'sessions').create()
    assert_session_access(store)
    configured=policy.save([], '0')
    assert configured['configured'] and configured['deniedPaths']==[]
    assert_session_access(store)  # 首次确认空配置不改变已有空策略。


@pytest.mark.parametrize('paths', [['../outside'], ['/tmp'], ['.'], ['private/**'], ['missing'], ['.git'], 'private'])
def test_invalid_settings_leave_current_policy_unchanged(tmp_path, paths):
    root=tmp_path/'repo';root.mkdir()
    policy=RepositoryAccessStore(root,tmp_path/'sessions')
    with pytest.raises(RepositoryAccessError):policy.save(paths,'0')
    assert policy.read()['configured'] is False


def test_rpc_settings_are_shared_and_old_session_cannot_run(tmp_path):
    from codeagent.product.service import ProductApplicationService
    from codeagent.product.rpc import JsonRpcServer
    root=tmp_path/'repo';root.mkdir();(root/'private').mkdir()
    app=ProductApplicationService(tmp_path/'sessions');rpc=JsonRpcServer(app)
    session=app.create_session(root)
    response=rpc.dispatch({'jsonrpc':'2.0','id':1,'method':'workspace/access/set','params':{'workspace':str(root),'deniedPaths':['private'],'expectedRevision':'0'}})
    assert response['result']['deniedPaths']==['private']
    assert not app.get_session(session['sessionId'])['availableActions']['canRun']
    with pytest.raises(RepositoryAccessError):app.run_session(session['sessionId'],'hello')
    next_session=app.create_session(root)
    assert app.get_session(next_session['sessionId'])['availableActions']['canRun']
    assert ProductApplicationService(tmp_path/'sessions').get_repository_access(root)['deniedPaths']==['private']


@pytest.mark.parametrize("ignored", [False, True])
def test_real_command_cannot_read_denied_file_source_git_or_old_session(tmp_path, ignored):
    import os
    if not os.environ.get('CODEAGENT_TEST_BWRAP'):pytest.skip('requires real bwrap')
    from codeagent.runtime.command_service import CommandService
    from codeagent.runtime.artifacts import CommandArtifactStore
    from codeagent.runtime.sandbox_executor import SandboxedCommandExecutor
    from codeagent.runtime.bubblewrap import BubblewrapBackend
    from codeagent.runtime.approval import AutoApprovalGate
    import shlex
    repo, policy, _, store, context=restricted_workspace(tmp_path, ignored)
    # 真实秘密只使用独立夹具内容。模拟旧会话与其他受管副本的残留。
    old=store.base_dir/'old-session';old.mkdir();(old/'old.txt').write_text('DENIED_TEST_SENTINEL')
    other = store.session_root / 'worktrees' / 'other'
    other.mkdir(parents=True)
    (other / 'private').mkdir(); (other / 'private/data.txt').write_text('DENIED_TEST_SENTINEL')
    service=CommandService(context,AutoApprovalGate(True),SandboxedCommandExecutor(BubblewrapBackend(Path(os.environ['CODEAGENT_TEST_BWRAP']))),store,CommandArtifactStore(store))
    paths=[context.active_root/'private/data.txt',repo/'private/data.txt',old/'old.txt',other/'private/data.txt',policy.path]
    if ignored:
        paths.extend([context.active_root/'.env',repo/'.env'])
        assert (context.active_root/'.env').exists()
    command='; '.join('cat '+shlex.quote(str(p))+' 2>/dev/null || true' for p in paths)
    command+='; git show HEAD:private/data.txt 2>/dev/null || true; cat allowed.txt; printf ok > allowed-output.txt'
    result=service.run_command({'command':command})
    assert result.ok, result.error or result.content
    assert 'DENIED_TEST_SENTINEL' not in result.content
    assert 'allowed' in result.content
    assert (context.active_root/'allowed-output.txt').read_text()=='ok'
    denied=service.run_command({'command':'printf hacked > private/data.txt'})
    assert not denied.ok
    assert (context.active_root/'private/data.txt').read_text()=='DENIED_TEST_SENTINEL\n'
    assert (repo/'private/data.txt').read_text()=='DENIED_TEST_SENTINEL\n'
    if ignored:
        assert (context.active_root/'.env').exists()
        denied=service.run_command({'command':'printf hacked > .env'})
        assert not denied.ok
        assert (context.active_root/'.env').exists()
        assert (repo/'.env').read_text()=='DENIED_TEST_SENTINEL ignored\n'
        stable=service.run_command({'command':'test -f allowed.txt', 'purpose':'validation'})
        assert stable.ok and 'validation_evidence_recorded' not in stable.metadata
        assert 'command_induced_changes' not in stable.metadata
        # 保护开启后，直接修复、真实测试与局部撤销均可完成。
        import sys
        original=(context.active_root/'sort_utils.py').read_bytes()
        edit=AtomicEditService(context,store).apply_workspace_edit({'operations':[{
            'op':'replace_text','path':'sort_utils.py','expected_sha256':hashlib.sha256(original).hexdigest(),
            'old_text':'reverse=True','new_text':'reverse=False'}]})
        assert edit.ok, edit.error
        validated=service.run_command({'command':shlex.quote(sys.executable)+' -B -m pytest -p no:cacheprovider test_sort_utils.py', 'purpose':'validation'})
        assert validated.ok and 'validation_evidence_recorded' not in validated.metadata, validated.content
        assert 'reverse=False' in (repo/'sort_utils.py').read_text()
        receipt=AtomicEditService(context,store).undo()
        assert receipt.ok,receipt
        assert (repo/'sort_utils.py').read_bytes()==original
        assert (repo/'.env').read_text()=='DENIED_TEST_SENTINEL ignored\n'
        assert (repo/'private/data.txt').read_text()=='DENIED_TEST_SENTINEL\n'



def test_missing_denied_mountpoints_are_temporary_and_crash_is_not_silent(tmp_path):
    from codeagent.runtime.access_mounts import denied_mountpoints, recover_access_mounts
    repo, _, _, store, context = restricted_workspace(tmp_path)
    missing = replace(context, denied_paths=('missing/deep/.env',))
    with denied_mountpoints(missing, store):
        assert (context.active_root/'missing/deep/.env').read_bytes()==b''
    assert not (context.active_root/'missing').exists()
    recover_access_mounts(store)
    assert store.workspace_state().value=='current_project'
    store.append_event('access_mounts_preparing',{'operation':'crashed','paths':['missing/.env']})
    recover_access_mounts(store)
    assert store.workspace_state().value=='recovery_required'


def test_external_mountpoint_change_is_preserved_and_blocks_completion(tmp_path):
    from codeagent.runtime.access_mounts import denied_mountpoints
    _, _, _, store, context = restricted_workspace(tmp_path)
    missing=replace(context,denied_paths=('missing.txt',))
    with pytest.raises(ValueError,match='清理未确认'):
        with denied_mountpoints(missing,store):
            (context.active_root/'missing.txt').write_text('external change')
    assert (context.active_root/'missing.txt').read_text()=='external change'
    assert store.workspace_state().value=='recovery_required'
