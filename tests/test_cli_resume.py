from typer.testing import CliRunner

from codeagent.cli import app
from codeagent.session.store import SessionStore


def test_resume_displays_existing_session_history(tmp_path):
    store = SessionStore(tmp_path).create()
    store.append_event("user_message", {"message": "previous question"})
    store.append_event("assistant_message", {"message": "previous answer"})

    result = CliRunner().invoke(app, ["resume", store.session_id, str(tmp_path)], input="/exit\n")

    assert result.exit_code == 0
    assert f"session: {store.session_id}" in result.output
    assert "recent history" in result.output
    assert "previous question" in result.output
    assert "previous answer" in result.output
