from io import StringIO

from rich.console import Console
from typer.testing import CliRunner
import pytest

from codeagent.cli import _format_tool_step, app
from codeagent.config import ModelConfig


def test_ask_records_fake_provider_model_and_title_outside_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    session_root = tmp_path / "sessions"
    workspace.mkdir()
    (workspace / "README.md").write_text("# demo", encoding="utf-8")

    result = CliRunner().invoke(app, ["ask", str(workspace), "看看项目", "--session-root", str(session_root)])

    assert result.exit_code == 0
    meta_files = list(session_root.glob("*/*/session.json"))
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


def test_glm_provider_without_key_fails_clearly(tmp_path, monkeypatch):
    monkeypatch.delenv("GLM_API_KEY", raising=False)

    result = CliRunner().invoke(app, ["ask", str(tmp_path), "hi", "--provider", "glm"])

    assert result.exit_code != 0
    assert "GLM_API_KEY" in str(result.exception)


def test_glm_uses_provider_specific_context_limit():
    assert ModelConfig(provider="glm").context_limit == 128_000
    assert ModelConfig(provider="deepseek").context_limit == 32_000


def test_reasoning_configuration_uses_provider_specific_efforts_and_safe_defaults():
    assert ModelConfig(provider="glm", reasoning_enabled=True).resolved_reasoning_effort == "high"
    assert ModelConfig(provider="deepseek", reasoning_enabled=True).resolved_reasoning_effort == "high"
    assert ModelConfig(provider="deepseek", reasoning_enabled=False).resolved_reasoning_effort is None
    with pytest.raises(ValueError, match="must be one of"):
        ModelConfig(provider="glm", reasoning_enabled=True, reasoning_effort="low")
    assert ModelConfig(provider="glm", model="glm-5.3", reasoning_enabled=True,
                       reasoning_effort="low").resolved_reasoning_effort == "low"
    with pytest.raises(ValueError, match="cannot be disabled"):
        ModelConfig(provider="glm", model="glm-5.3")
    with pytest.raises(ValueError, match="not supported"):
        ModelConfig(provider="glm", model="glm-5.1", reasoning_enabled=True)


def test_tool_progress_renders_model_text_without_rich_markup_parsing():
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    step = {
        "tool": "run_command",
        "arguments": {"command": r"pattern=[^/path][/path]"},
        "result": {"ok": True, "truncated": False},
    }

    console.print(_format_tool_step(step))

    assert "[/path]" in output.getvalue()
