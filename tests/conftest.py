import pytest


@pytest.fixture(autouse=True)
def isolated_session_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEAGENT_SESSION_ROOT", str(tmp_path / "codeagent-sessions"))
