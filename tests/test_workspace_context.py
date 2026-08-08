import subprocess

import pytest

from codeagent.session.store import SessionStore
from codeagent.tools.fs_read import build_fs_tools
from codeagent.workspace.git_worktree import GitWorktreeError, GitWorktreeManager
from codeagent.workspace.workspace import WorkspaceContext


def git(cwd, *args):
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def clean_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "tests@example.com")
    git(repo, "config", "user.name", "CodeAgent Tests")
    (repo / "tracked.txt").write_text("HEAD content\n", encoding="utf-8")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "initial")
    return repo


def test_readonly_context_preserves_source_behavior(tmp_path):
    (tmp_path / "a.txt").write_text("source", encoding="utf-8")
    context = WorkspaceContext.source(tmp_path)
    tool = {item.name: item for item in build_fs_tools(context)}["read_file"]
    assert context.source_root == context.active_root
    assert context.workspace_kind == "source"
    assert tool.handler({"path": "a.txt"}).content == "source"


def test_clean_repo_creates_detached_worktree_with_head_content(tmp_path):
    repo = clean_repo(tmp_path)
    manager = GitWorktreeManager(tmp_path / "sessions")
    context = manager.create(repo, "task-1")
    assert context.workspace_kind == "git_worktree"
    assert context.base_commit == git(repo, "rev-parse", "HEAD")
    assert git(context.active_root, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert (context.active_root / "tracked.txt").read_text(encoding="utf-8") == "HEAD content\n"


@pytest.mark.parametrize("dirty_name", ["tracked.txt", "untracked.txt"])
def test_dirty_repo_is_rejected(tmp_path, dirty_name):
    repo = clean_repo(tmp_path)
    (repo / dirty_name).write_text("dirty", encoding="utf-8")
    with pytest.raises(GitWorktreeError, match="不干净"):
        GitWorktreeManager(tmp_path / "sessions").create(repo, "task-dirty")


def test_non_git_repo_is_rejected(tmp_path):
    source = tmp_path / "plain"
    source.mkdir()
    with pytest.raises(GitWorktreeError, match="不是 Git 仓库"):
        GitWorktreeManager(tmp_path / "sessions").create(source, "task-plain")


def test_read_tools_use_active_root(tmp_path):
    source = tmp_path / "source"
    active = tmp_path / "active"
    source.mkdir()
    active.mkdir()
    (source / "value.txt").write_text("source", encoding="utf-8")
    (active / "value.txt").write_text("active", encoding="utf-8")
    context = WorkspaceContext(source, active, "git_worktree", "abc", "task")
    tool = {item.name: item for item in build_fs_tools(context)}["read_file"]
    assert tool.handler({"path": "value.txt"}).content == "active"


def test_session_resume_recovers_worktree_context(tmp_path):
    repo = clean_repo(tmp_path)
    session_root = tmp_path / "sessions"
    store = SessionStore(repo, session_root=session_root).create()
    manager = GitWorktreeManager(session_root)
    created = manager.create(repo, store.session_id)
    store.update_workspace_context(created)
    loaded = SessionStore(repo, session_id=store.session_id, session_root=session_root).load()
    recovered = manager.recover(**{
        "source_root": loaded.workspace_context().source_root,
        "active_root": loaded.workspace_context().active_root,
        "task_workspace_id": loaded.workspace_context().task_workspace_id,
        "base_commit": loaded.workspace_context().base_commit,
    })
    assert recovered == created
    assert loaded.read_meta()["worktree_lifecycle_state"] == "active"


def test_cleanup_keeps_source_workspace_unchanged(tmp_path):
    repo = clean_repo(tmp_path)
    before = git(repo, "status", "--porcelain", "--untracked-files=all")
    manager = GitWorktreeManager(tmp_path / "sessions")
    context = manager.create(repo, "task-cleanup")
    manager.cleanup(context)
    assert repo.exists()
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "HEAD content\n"
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == before
    assert not context.active_root.exists()
