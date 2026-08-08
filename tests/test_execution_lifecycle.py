import json
import subprocess
import sys

import pytest

from codeagent.cli import _create_session, build_runner
from codeagent.cli import app
from typer.testing import CliRunner
from codeagent.config import ModelConfig
from codeagent.runtime.approval import ConsoleApprovalGate, FakeApprovalGate
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.command import ApprovalDecision, CommandSpec
from codeagent.runtime.command_service import CommandService
from codeagent.runtime.local_executor import LocalCommandExecutor
from codeagent.runtime.policy import CommandPolicy
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


def test_readonly_cli_session_has_no_worktree_or_run_check(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    ws = Workspace(root)
    store = _create_session(ws, tmp_path / "sessions", "readonly", ModelConfig())
    runner = build_runner(store, ws, interactive=False, model_config=ModelConfig())
    assert store.workspace_context().active_root == root.resolve()
    assert "run_check" not in {tool.name for tool in runner.tools.list_tools()}
    assert not (store.session_root / "worktrees").exists()


def test_execution_cli_session_creates_detached_worktree_and_shared_active_root(tmp_path):
    root = repo(tmp_path)
    store = _create_session(Workspace(root), tmp_path / "sessions", "execution", ModelConfig())
    context = store.workspace_context()
    runner = build_runner(store, Workspace(context.active_root), interactive=False, model_config=ModelConfig())
    assert context.workspace_kind == "git_worktree"
    assert git(context.active_root, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert runner.tools.workspace_context.active_root == context.active_root
    assert {"read_file", "run_check"} <= {tool.name for tool in runner.tools.list_tools()}


def test_execution_creation_rejects_dirty_and_non_git(tmp_path):
    root = repo(tmp_path)
    (root / "dirty.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(GitWorktreeError):
        _create_session(Workspace(root), tmp_path / "sessions-a", "execution", ModelConfig())
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(GitWorktreeError):
        _create_session(Workspace(plain), tmp_path / "sessions-b", "execution", ModelConfig())


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


def test_command_side_effect_audit_tracks_modified_and_untracked_files(tmp_path):
    root = repo(tmp_path)
    manager = GitWorktreeManager(tmp_path / "worktrees")
    context = manager.create(root, "audit")
    test_file = context.active_root / "test_effect.py"
    test_file.write_text(
        "from pathlib import Path\ndef test_effect():\n"
        "    Path('data.txt').write_text('changed\\n')\n"
        "    Path('generated.txt').write_text('generated\\n')\n",
        encoding="utf-8",
    )
    git(context.active_root, "add", "test_effect.py")
    git(context.active_root, "commit", "-m", "effect test")
    context = type(context)(context.source_root, context.active_root, context.workspace_kind, git(context.active_root, "rev-parse", "HEAD"), context.task_workspace_id)
    store = SessionStore(root, session_root=tmp_path / "store").create(mode="execution", workspace_context=context)
    artifacts = CommandArtifactStore(store)
    service = CommandService(context, CommandPolicy(), FakeApprovalGate(ApprovalDecision.APPROVE_ONCE), LocalCommandExecutor(), store, artifacts)
    result = service.run_check({"kind": "pytest", "targets": ["test_effect.py"], "timeout_seconds": 30})
    receipt = [e.payload for e in store.read_events() if e.type == "command_receipt"][-1]
    assert result.metadata["workspace_changed"] is True
    assert {"data.txt", "generated.txt"} <= set(receipt["result"]["changed_files"])
    patch = receipt["result"]["workspace_change_artifact"]
    assert "data.txt" in open(patch, encoding="utf-8").read()


def test_console_approval_noninteractive_defaults_to_deny(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    spec = CommandSpec("id", "pytest", (sys.executable, "-m", "pytest", "test_ok.py"), ".", 30)
    assert ConsoleApprovalGate().request_command(spec) == ApprovalDecision.DENY


def test_console_approval_approve_once_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("codeagent.runtime.approval.Confirm.ask", lambda *args, **kwargs: True)
    spec = CommandSpec("id", "pytest", (sys.executable, "-m", "pytest", "test_ok.py"), ".", 30)
    assert ConsoleApprovalGate(Workspace(repo(tmp_path)).context).request_command(spec) == ApprovalDecision.APPROVE_ONCE


def test_cli_start_exit_preserves_and_resume_validates_ready(tmp_path, monkeypatch):
    root = repo(tmp_path)
    session_root = tmp_path / "sessions"
    calls = []
    def near_cli_loop(store, ws, model_config, execution_allowed=True):
        calls.append((store.session_id, execution_allowed))
        if len(calls) == 1:
            context = store.workspace_context()
            service = CommandService(
                context, CommandPolicy(), FakeApprovalGate(ApprovalDecision.APPROVE_ONCE),
                LocalCommandExecutor(), store, CommandArtifactStore(store),
            )
            assert service.run_check({"kind": "pytest", "targets": ["test_ok.py"], "timeout_seconds": 30}).ok
    monkeypatch.setattr("codeagent.cli._interactive_loop", near_cli_loop)
    cli = CliRunner()
    started = cli.invoke(app, ["start", str(root), "--mode", "execution", "--session-root", str(session_root)])
    assert started.exit_code == 0, started.output
    meta = SessionStore.list_sessions(root, session_root=session_root)[0]
    context = WorkspaceContext.from_metadata(meta)
    assert context.active_root.exists()
    first_store = SessionStore(root, session_id=meta["session_id"], session_root=session_root).load()
    command_receipt = [event for event in first_store.read_events() if event.type == "command_receipt"][-1]
    assert command_receipt.payload["status"] == "completed"
    assert (first_store.session_dir / "artifacts" / "commands" / command_receipt.payload["command_spec"]["command_id"] / "result.json").exists()
    resumed = cli.invoke(app, ["resume", meta["session_id"], "--session-root", str(session_root)])
    assert resumed.exit_code == 0, resumed.output
    assert calls[-1] == (meta["session_id"], True)
    assert context.active_root.exists()
