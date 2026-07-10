from typer.testing import CliRunner

from codeagent.cli import app


def test_ask_records_fake_provider_model_and_title_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    session_root = tmp_path / "sessions"
    workspace.mkdir()
    (workspace / "README.md").write_text("# demo", encoding="utf-8")

    result = CliRunner().invoke(app, ["ask", str(workspace), "看看项目", "--session-root", str(session_root)])

    assert result.exit_code == 0
    meta_files = list(session_root.glob("*/*/meta.json"))
    assert meta_files
    meta_text = meta_files[0].read_text(encoding="utf-8")
    assert '"provider": "fake"' in meta_text
    assert '"model": "fake"' in meta_text
    assert '"title": "看看项目"' in meta_text
    assert not (workspace / ".codeagent" / "sessions").exists()


def test_deepseek_provider_without_key_fails_clearly(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    result = CliRunner().invoke(app, ["ask", str(tmp_path), "hi", "--provider", "deepseek"])

    assert result.exit_code != 0
    assert "DEEPSEEK_API_KEY" in str(result.exception)
