from typer.testing import CliRunner

from codeagent.cli import app
from codeagent.session.store import SessionStore


def test_resume_displays_existing_session_history(tmp_path):
    session_root = tmp_path / "sessions"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(workspace, session_root=session_root).create(title="Previous chat")
    store.append_event("user_message", {"message": "previous question"})
    store.append_event("assistant_message", {"message": "previous answer"})

    result = CliRunner().invoke(app, ["resume", store.session_id, str(workspace), "--session-root", str(session_root)], input="/exit\n")

    assert result.exit_code == 0
    assert "title: Previous chat" in result.output
    assert f"session: {store.session_id}" in result.output
    assert "recent history" in result.output
    assert "previous question" in result.output
    assert "previous answer" in result.output


def test_resume_without_id_can_select_session_by_number(tmp_path):
    session_root = tmp_path / "sessions"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(workspace, session_root=session_root).create(title="Selectable chat")

    result = CliRunner().invoke(app, ["resume", "--workspace", str(workspace), "--session-root", str(session_root)], input="1\n/exit\n")

    assert result.exit_code == 0
    assert "Selectable chat" in result.output
    assert f"session: {store.session_id}" in result.output


def test_resume_without_id_selects_across_all_workspaces(tmp_path):
    session_root = tmp_path / "sessions"
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    SessionStore(workspace_a, session_root=session_root).create(title="Chat A")
    store_b = SessionStore(workspace_b, session_root=session_root).create(title="Chat B")

    result = CliRunner().invoke(app, ["resume", "--session-root", str(session_root)], input="1\n/exit\n")

    assert result.exit_code == 0
    assert "workspace-b" in result.output
    assert f"[{store_b.session_id}]" in result.output
    assert f"session: {store_b.session_id}" in result.output


def test_list_sessions_defaults_to_all_workspaces(tmp_path):
    session_root = tmp_path / "sessions"
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    SessionStore(workspace_a, session_root=session_root).create(title="Chat A")
    SessionStore(workspace_b, session_root=session_root).create(title="Chat B")

    result = CliRunner().invoke(app, ["list-sessions", "--session-root", str(session_root)])

    assert result.exit_code == 0
    assert "Chat A" in result.output
    assert "Chat B" in result.output
    assert "workspace=" in result.output
    assert "[" not in result.output
