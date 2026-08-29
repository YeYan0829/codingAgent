import json
import pytest

from codeagent.session.store import SessionStore


def test_session_store_create_append_and_read(tmp_path):
    session_root = tmp_path / "sessions"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(workspace, session_root=session_root).create()
    store.append_event("user_message", {"message": "hello"})
    store.append_event("assistant_message", {"message": "world"})

    meta = store.read_meta()
    events = store.read_events()

    assert meta["session_id"] == store.session_id
    assert meta["title"] == "hello"
    assert meta["session_root"] == str(session_root.resolve())
    assert [event.type for event in events] == ["session_created", "user_message", "assistant_message"]
    assert store.events_path.read_text(encoding="utf-8").count("\n") == 3
    assert not (store.session_dir / "transcript.md").exists()
    assert store.meta_path.name == "session.json"
    assert not (workspace / ".codeagent" / "sessions").exists()


def test_list_sessions(tmp_path):
    session_root = tmp_path / "sessions"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(workspace, session_root=session_root).create(title="Inspect repo")

    sessions = SessionStore.list_sessions(workspace, session_root=session_root)

    assert sessions[0]["session_id"] == store.session_id
    assert sessions[0]["title"] == "Inspect repo"


def test_list_all_sessions_across_workspaces(tmp_path):
    session_root = tmp_path / "sessions"
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    store_a = SessionStore(workspace_a, session_root=session_root).create(title="Inspect A")
    store_b = SessionStore(workspace_b, session_root=session_root).create(title="Inspect B")

    sessions = SessionStore.list_all_sessions(session_root=session_root)

    ids = {session["session_id"] for session in sessions}
    workspaces = {session["workspace"] for session in sessions}
    assert ids == {store_a.session_id, store_b.session_id}
    assert workspaces == {str(workspace_a.resolve()), str(workspace_b.resolve())}


def test_legacy_workspace_sessions_are_not_loaded(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_id = "legacy123456"
    legacy_dir = workspace / ".codeagent" / "sessions" / session_id
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "meta.json").write_text(
        json.dumps({"session_id": session_id, "title": "Legacy", "workspace": str(workspace.resolve())}),
        encoding="utf-8",
    )
    (legacy_dir / "events.jsonl").write_text("", encoding="utf-8")

    from codeagent.session.store import SessionStoreError
    with pytest.raises(SessionStoreError):
        SessionStore(workspace, session_id=session_id, session_root=tmp_path / "new-sessions").load()


def test_explicit_session_root_can_exclude_legacy_sessions(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_root = tmp_path / "sessions"
    current = SessionStore(workspace, session_root=session_root).create(title="Current")
    legacy_dir = workspace / ".codeagent" / "sessions" / "legacy123456"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "meta.json").write_text(
        json.dumps({"session_id": "legacy123456", "title": "Legacy", "workspace": str(workspace.resolve())}),
        encoding="utf-8",
    )

    sessions = SessionStore.list_sessions(workspace, session_root=session_root, include_legacy=False)

    assert [session["session_id"] for session in sessions] == [current.session_id]
