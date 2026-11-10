"""当前项目内的局部事务与持久撤销，不依赖Git版本或测试资格。"""
import json
import stat
import subprocess
from pathlib import Path

import pytest

from codeagent.runtime.atomic_edit import AtomicEditService
from codeagent.session.store import SessionStore


def setup(tmp_path, *, git=False):
    root = tmp_path / 'project'; root.mkdir()
    (root / 'a.py').write_text('original\n')
    if git:
        subprocess.run(['git', 'init', '-q', str(root)], check=True)
        subprocess.run(['git', 'add', '.'], cwd=root, check=True)
        subprocess.run(['git', '-c', 'user.name=T', '-c', 'user.email=t@example.invalid', 'commit', '-qm', 'base'], cwd=root, check=True)
    store = SessionStore(root, session_root=tmp_path / 'sessions').create()
    return root, store, AtomicEditService(store.workspace_context(), store)


def edit(service, *operations):
    result = service.apply_workspace_edit({'operations': list(operations)})
    assert result.ok, result
    return result


def test_current_project_edits_do_not_require_git_or_prior_read_and_persist_undo(tmp_path):
    root, store, service = setup(tmp_path)
    store.append_event('user_message', {'message': '修改'})
    edit(service, {'path': 'a.py', 'old_text': 'original', 'new_text': 'edited'})
    assert (root / 'a.py').read_text() == 'edited\n'
    restored = SessionStore(root, store.session_id, store.session_root).load()
    undo = AtomicEditService(restored.workspace_context(), restored).undo()
    assert undo.ok, undo
    assert (root / 'a.py').read_text() == 'original\n'
    assert not service.undo().ok
    events = [e for e in store.read_events() if e.type == 'edit_transaction']
    assert len(events) == 2 and events[1].payload['undo_of'] == [events[0].payload['transaction_id']]
    assert not any('tree' in key or 'revision' in key for e in events for key in e.payload)


def test_undo_groups_turn_and_preserves_initial_staged_unstaged_and_untracked(tmp_path):
    root, store, service = setup(tmp_path, git=True)
    (root / 'a.py').write_text('staged\n'); subprocess.run(['git', 'add', 'a.py'], cwd=root, check=True)
    (root / 'a.py').write_text('dirty\n'); (root / 'local.txt').write_text('user-local\n')
    index = (root / '.git/index').read_bytes()
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root)
    store.append_event('user_message', {'message': '修改两次'})
    edit(service, {'path': 'a.py', 'old_text': 'dirty', 'new_text': 'agent-one'})
    edit(service, {'path': 'a.py', 'old_text': 'agent-one', 'new_text': 'agent-two'},
         {'path': 'new/f.py', 'content': 'new\n'})
    result = service.undo()
    assert result.ok, result
    assert (root / 'a.py').read_text() == 'dirty\n'
    assert (root / 'local.txt').read_text() == 'user-local\n'
    assert not (root / 'new/f.py').exists()
    assert (root / '.git/index').read_bytes() == index
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root) == head


def test_delete_move_and_executable_mode_are_reversible(tmp_path):
    root, store, service = setup(tmp_path)
    (root / 'a.py').chmod(0o755); (root / 'remove.py').write_text('keep\n'); (root / 'remove.py').chmod(0o700)
    store.append_event('user_message', {'message': '改名删除'})
    edit(service, {'op': 'move_file', 'source': 'a.py', 'destination': 'renamed.py'},
         {'op': 'delete_file', 'path': 'remove.py'})
    assert stat.S_IMODE((root / 'renamed.py').stat().st_mode) == 0o755
    assert service.undo().ok
    assert (root / 'a.py').read_text() == 'original\n' and not (root / 'renamed.py').exists()
    assert stat.S_IMODE((root / 'a.py').stat().st_mode) == 0o755
    assert stat.S_IMODE((root / 'remove.py').stat().st_mode) == 0o700


@pytest.mark.parametrize('external', ['content', 'mode', 'symlink'])
def test_undo_conflict_checks_all_paths_before_writing(tmp_path, external):
    root, store, service = setup(tmp_path)
    (root / 'b.py').write_text('before\n')
    store.append_event('user_message', {'message': '修改'})
    edit(service, {'path': 'a.py', 'old_text': 'original', 'new_text': 'edited'},
         {'path': 'b.py', 'old_text': 'before', 'new_text': 'after'})
    if external == 'content': (root / 'b.py').write_text('user-new\n')
    elif external == 'mode': (root / 'b.py').chmod(0o700)
    else:
        (root / 'b.py').unlink(); (root / 'b.py').symlink_to(root / 'a.py')
    result = service.undo()
    assert not result.ok and result.error_code in {'undo_conflict', 'path_rejected'}
    assert (root / 'a.py').read_text() == 'edited\n'
    assert len([e for e in store.read_events() if e.type == 'edit_transaction']) == 1


def test_shell_modified_path_conflicts_but_unrelated_shell_files_are_preserved(tmp_path):
    root, store, service = setup(tmp_path)
    store.append_event('user_message', {'message': '改文件'})
    edit(service, {'path': 'a.py', 'old_text': 'original', 'new_text': 'edited'})
    (root / 'command.out').write_text('shell output')
    assert service.undo().ok
    assert (root / 'command.out').read_text() == 'shell output'


def test_crash_recovery_does_not_overwrite_external_changes(tmp_path):
    root, store, service = setup(tmp_path)
    result = edit(service, {'path': 'a.py', 'old_text': 'original', 'new_text': 'edited'})
    directory = service.root / result.metadata['transaction_id']
    service._write_state(directory, 'committing')
    (root / 'a.py').write_text('external after crash\n')
    with pytest.raises(Exception, match='恢复失败'):
        service.ensure_recovered()
    assert (root / 'a.py').read_text() == 'external after crash\n'
    assert store.workspace_state().value == 'recovery_required'


def test_internal_metadata_edits_are_rejected(tmp_path):
    root, store, service = setup(tmp_path, git=True)
    original = (root / '.git/config').read_bytes()
    result = service.apply_workspace_edit({'operations': [{'path': '.git/config', 'content': 'bad', 'overwrite': True}]})
    assert not result.ok and result.error_code == 'path_rejected'
    assert (root / '.git/config').read_bytes() == original


def test_undo_obeys_current_repository_access_and_leaves_files_unchanged(tmp_path):
    from codeagent.safety.repository_access import RepositoryAccessStore
    root, store, service = setup(tmp_path)
    store.append_event('user_message', {'message': '修改'})
    edit(service, {'path': 'a.py', 'old_text': 'original', 'new_text': 'edited'})
    RepositoryAccessStore(root, store.session_root).save(['a.py'], '0')
    result = service.undo()
    assert not result.ok and '访问设置' in result.error
    assert (root / 'a.py').read_text() == 'edited\n'
