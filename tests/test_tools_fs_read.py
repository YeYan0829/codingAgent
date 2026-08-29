from codeagent.safety.sensitive import SensitiveListingMode
from codeagent.tools.fs_read import build_fs_tools
from codeagent.tools.registry import ToolRegistry
from codeagent.workspace.workspace import WorkspaceContext


def registry(tmp_path, sensitive_listing_mode=SensitiveListingMode.SHOW_MARKED):
    reg = ToolRegistry()
    for tool in build_fs_tools(WorkspaceContext.source(tmp_path), sensitive_listing_mode=sensitive_listing_mode):
        reg.register(tool)
    return reg


def test_list_dir(tmp_path):
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")

    result = registry(tmp_path).call("list_dir", {"path": "."})

    assert result.ok
    assert "a.txt" in result.content


def test_list_dir_marks_sensitive_names_by_default(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=x", encoding="utf-8")
    (tmp_path / "secret.json").write_text("{}", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("ok", encoding="utf-8")

    result = registry(tmp_path).call("list_dir", {"path": "."})

    assert result.ok
    assert "visible.txt" in result.content
    assert ".env [sensitive, content blocked]" in result.content
    assert "secret.json [sensitive-name, content blocked]" in result.content


def test_show_tree_marks_sensitive_names_by_default(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=x", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print(1)", encoding="utf-8")

    result = registry(tmp_path).call("show_tree", {"path": ".", "max_depth": 2})

    assert result.ok
    assert "src" in result.content
    assert ".env [sensitive, content blocked]" in result.content


def test_sensitive_listing_modes_redact_and_hide(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=x", encoding="utf-8")

    redacted = registry(tmp_path, SensitiveListingMode.REDACT_NAME).call("list_dir", {"path": "."})
    hidden = registry(tmp_path, SensitiveListingMode.HIDE).call("list_dir", {"path": "."})

    assert redacted.ok
    assert "<sensitive file hidden> [sensitive, content blocked]" in redacted.content
    assert ".env" not in redacted.content
    assert hidden.ok
    assert ".env" not in hidden.content


def test_read_file_and_line_range(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    reg = registry(tmp_path)

    whole = reg.call("read_file", {"path": "a.txt"})
    part = reg.call("read_file", {"path": "a.txt", "start_line": 2, "end_line": 2})

    assert whole.ok and "three" in whole.content
    assert part.content == "two"


def test_read_file_sensitive_env_is_blocked(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=x", encoding="utf-8")

    result = registry(tmp_path).call("read_file", {"path": ".env"})

    assert not result.ok
    assert "sensitive file is blocked" in result.error


def test_read_file_env_example_is_allowed(tmp_path):
    (tmp_path / ".env.example").write_text("TOKEN=example", encoding="utf-8")

    result = registry(tmp_path).call("read_file", {"path": ".env.example"})

    assert result.ok
    assert "TOKEN=example" in result.content


def test_search_text(tmp_path):
    (tmp_path / "a.txt").write_text("alpha\nneedle here\n", encoding="utf-8")

    result = registry(tmp_path).call("search_text", {"query": "needle", "path": "."})

    assert result.ok
    assert "a.txt:2" in result.content


def test_search_text_skips_sensitive_file_content(tmp_path):
    (tmp_path / ".env").write_text("SECRET_NEEDLE=x", encoding="utf-8")
    (tmp_path / "a.txt").write_text("normal", encoding="utf-8")

    result = registry(tmp_path).call("search_text", {"query": "SECRET_NEEDLE", "path": "."})

    assert result.ok
    assert result.content == ""


def test_find_files(tmp_path):
    (tmp_path / "main.py").write_text("print(1)", encoding="utf-8")

    result = registry(tmp_path).call("find_files", {"include": ["*.py"], "path": "."})

    assert result.ok
    assert "main.py" in result.content


def test_find_files_never_exposes_sensitive_names(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=x", encoding="utf-8")

    result = registry(tmp_path).call("find_files", {"include": [".env"], "include_hidden": True, "path": "."})

    assert result.ok
    assert result.content == ""


def test_large_file_truncates(tmp_path):
    (tmp_path / "large.txt").write_text("x" * 70000, encoding="utf-8")

    result = registry(tmp_path).call("read_file", {"path": "large.txt"})

    assert result.ok
    assert result.truncated


def test_read_file_rejects_invalid_utf8(tmp_path):
    (tmp_path / "invalid.txt").write_bytes(b"ok\xff")

    result = registry(tmp_path).call("read_file", {"path": "invalid.txt"})

    assert not result.ok
    assert result.error_code == "unsupported_text_encoding"


def test_search_regex_hidden_and_ignore(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / "visible.py").write_text("Alpha42\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("Alpha43\n", encoding="utf-8")
    (tmp_path / ".hidden.py").write_text("Alpha44\n", encoding="utf-8")
    reg = registry(tmp_path)

    default = reg.call("search_text", {"query": "Alpha[0-9]+", "mode": "regex"})
    hidden = reg.call("search_text", {"query": "Alpha[0-9]+", "mode": "regex", "include_hidden": True})

    assert default.ok and "visible.py" in default.content
    assert "ignored.py" not in default.content and ".hidden.py" not in default.content
    assert hidden.ok and ".hidden.py" in hidden.content


def test_search_invalid_regex_has_stable_error(tmp_path):
    (tmp_path / "a.py").write_text("ok\n", encoding="utf-8")

    result = registry(tmp_path).call("search_text", {"query": "(", "mode": "regex"})

    assert not result.ok
    assert result.error_code == "invalid_arguments"
