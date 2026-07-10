from codeagent.session.store import SessionStore


def test_session_store_create_append_and_read(tmp_path):
    store = SessionStore(tmp_path).create()
    store.append_event("user_message", {"message": "hello"})
    store.append_event("assistant_message", {"message": "world"})

    meta = store.read_meta()
    events = store.read_events()

    assert meta["session_id"] == store.session_id
    assert [event.type for event in events] == ["session_created", "user_message", "assistant_message"]
    assert store.events_path.read_text(encoding="utf-8").count("\n") == 3
    assert "hello" in store.transcript_path.read_text(encoding="utf-8")


def test_list_sessions(tmp_path):
    store = SessionStore(tmp_path).create()

    sessions = SessionStore.list_sessions(tmp_path)

    assert sessions[0]["session_id"] == store.session_id
