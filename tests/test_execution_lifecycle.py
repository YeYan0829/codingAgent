import json
import subprocess

import pytest

from codeagent.cli import _create_session, build_runner
from codeagent.cli import app
from typer.testing import CliRunner
from codeagent.config import ModelConfig
from codeagent.session.store import SessionStore
from codeagent.workspace.git_worktree import GitWorktreeError, GitWorktreeManager, WorkspaceState
from codeagent.workspace.workspace import Workspace, WorkspaceContext


def git(cwd, *args):
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.email", "tests@example.com")
    git(root, "config", "user.name", "Tests")
    (root / "data.txt").write_text("base\n", encoding="utf-8")
    (root / "test_ok.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    return root


def test_new_session_has_no_worktree_and_exposes_upgradeable_tools(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    ws = Workspace(root)
    store = _create_session(ws, tmp_path / "sessions", ModelConfig())
    runner = build_runner(store, ws, interactive=False, model_config=ModelConfig())
    assert store.workspace_context().active_root == root.resolve()
    assert {"run_command", "apply_workspace_edit"} <= {tool.name for tool in runner.tools.list_tools()}
    assert store.read_meta()["workspace_state"] == "source_only"
    assert not (store.session_root / "worktrees").exists()


def test_session_creation_defers_worktree(tmp_path):
    root = repo(tmp_path)
    store = _create_session(Workspace(root), tmp_path / "sessions", ModelConfig())
    context = store.workspace_context()
    runner = build_runner(store, Workspace(context.active_root), interactive=False, model_config=ModelConfig())
    assert context.workspace_kind == "source"
    assert runner.tools.workspace_context.active_root == context.active_root
    assert {"read_file", "run_command"} <= {tool.name for tool in runner.tools.list_tools()}


def test_session_creation_allows_readonly_dirty_and_non_git_workspace(tmp_path):
    root = repo(tmp_path)
    (root / "dirty.txt").write_text("dirty", encoding="utf-8")
    assert _create_session(Workspace(root), tmp_path / "sessions-a", ModelConfig()).workspace_state().value == "source_only"
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _create_session(Workspace(plain), tmp_path / "sessions-b", ModelConfig()).workspace_state().value == "source_only"


def test_workspace_state_ready_stale_source_dirty_and_missing(tmp_path):
    root = repo(tmp_path)
    manager = GitWorktreeManager(tmp_path / "sessions")
    context = manager.create(root, "one")
    assert manager.inspect(context).state == WorkspaceState.READY
    (root / "new.txt").write_text("new", encoding="utf-8")
    git(root, "add", "new.txt")
    git(root, "commit", "-m", "new head")
    assert manager.inspect(context).state == WorkspaceState.STALE
    (root / "dirty.txt").write_text("dirty", encoding="utf-8")
    assert manager.inspect(context).state == WorkspaceState.SOURCE_DIRTY
    (root / "dirty.txt").unlink()
    manager.cleanup(context)
    assert manager.inspect(context).state == WorkspaceState.MISSING


def test_dirty_worktree_is_preserved_by_cleanup(tmp_path):
    root = repo(tmp_path)
    manager = GitWorktreeManager(tmp_path / "sessions")
    context = manager.create(root, "dirty")
    (context.active_root / "generated.txt").write_text("keep", encoding="utf-8")
    assert manager.inspect(context).state == WorkspaceState.WORKTREE_DIRTY
    with pytest.raises(GitWorktreeError):
        manager.cleanup(context)
    assert (context.active_root / "generated.txt").exists()


def test_two_sessions_are_isolated_and_cleanup_one_keeps_other(tmp_path):
    root = repo(tmp_path)
    manager = GitWorktreeManager(tmp_path / "sessions")
    first = manager.create(root, "first")
    second = manager.create(root, "second")
    assert first.active_root != second.active_root
    manager.cleanup(first)
    assert not first.active_root.exists()
    assert second.active_root.exists() and manager.inspect(second).state == WorkspaceState.READY


def test_cli_start_exit_preserves_and_resume_validates_ready(tmp_path, monkeypatch):
    root = repo(tmp_path)
    session_root = tmp_path / "sessions"
    calls = []
    def near_cli_loop(store, ws, model_config, execution_allowed=True):
        calls.append((store.session_id, execution_allowed))
    monkeypatch.setattr("codeagent.cli._interactive_loop", near_cli_loop)
    cli = CliRunner()
    started = cli.invoke(app, ["start", str(root), "--session-root", str(session_root)])
    assert started.exit_code == 0, started.output
    meta = SessionStore.list_sessions(root, session_root=session_root)[0]
    context = WorkspaceContext.from_metadata(meta)
    assert context.active_root == root.resolve()
    resumed = cli.invoke(app, ["resume", meta["session_id"], "--session-root", str(session_root)])
    assert resumed.exit_code == 0, resumed.output
    assert calls[-1] == (meta["session_id"], True)
    assert not (session_root / "worktrees").exists()
