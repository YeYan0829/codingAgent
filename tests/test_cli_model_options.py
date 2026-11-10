from io import StringIO
import json

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
    options = json.loads(meta_text)["model_options"]
    assert options["max_tokens"] == 4000 and "summary_max_tokens" not in options
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


def test_model_output_defaults_overrides_and_context_reserve_share_one_config():
    config = ModelConfig(provider="glm", model="glm-5.3", reasoning_enabled=True)
    assert config.resolved_max_tokens == 65536
    assert config.context_limit == 1_000_000
    assert config.limits.max_output_tokens == 131072
    explicit = ModelConfig(provider="glm", model="glm-5.3", reasoning_enabled=True,
                           max_tokens=32768, summary_max_tokens=4096)
    caps = explicit.context_capabilities()
    assert caps.max_output_tokens == 32768
    assert explicit.context_capabilities(summary=True).max_output_tokens == 4096
    assert config.resolved_summary_max_tokens == config.resolved_max_tokens == 65536
    assert caps.context_limit == explicit.context_limit
    assert ModelConfig().resolved_max_tokens == ModelConfig(provider="glm").resolved_max_tokens == 4000


def test_summary_policy_does_not_change_model_accounting_identity():
    from dataclasses import replace
    from codeagent.runtime.execution_service import model_identity
    config = ModelConfig(provider="glm", model="glm-5.3", reasoning_enabled=True)
    identity = model_identity(config)
    assert "summary_max_tokens" not in identity
    assert model_identity(replace(config, summary_max_tokens=4096)) == identity


@pytest.mark.parametrize("options", [
    {"max_tokens": 0}, {"max_tokens": -1}, {"max_tokens": True},
    {"max_tokens": 131073}, {"summary_max_tokens": 0},
    {"summary_max_tokens": 131073},
])
def test_model_output_limits_reject_invalid_values(options):
    with pytest.raises(ValueError):
        ModelConfig(provider="glm", model="glm-5.3", reasoning_enabled=True, **options)


@pytest.mark.parametrize("command", ["ask", "start"])
def test_cli_output_options_survive_session_resume(tmp_path, monkeypatch, command):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_root = tmp_path / "sessions"
    args = [command, str(workspace)] + (["hi"] if command == "ask" else [])
    args += ["--session-root", str(session_root), "--max-tokens", "6000", "--summary-max-tokens", "3072"]
    result = CliRunner().invoke(app, args, input="/exit\n")
    assert result.exit_code == 0, result.output
    meta = json.loads(next(session_root.glob("*/*/session.json")).read_text())
    assert meta["model_options"]["max_tokens"] == 6000
    assert meta["model_options"]["summary_max_tokens"] == 3072
    captured = []
    monkeypatch.setattr("codeagent.cli._interactive_loop", lambda store, ws, model_config, **kw: captured.append(model_config))
    resumed = CliRunner().invoke(app, ["resume", meta["session_id"], "--session-root", str(session_root)])
    assert resumed.exit_code == 0, resumed.output
    assert captured[0].resolved_max_tokens == 6000 and captured[0].summary_max_tokens == 3072


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
