from codeagent.safety.path_guard import PathGuard
from codeagent.safety.sensitive import SensitiveListingMode
from codeagent.tools.fs_read import build_fs_tools
from codeagent.tools.registry import ToolRegistry


def registry(tmp_path, sensitive_listing_mode=SensitiveListingMode.SHOW_MARKED):
    reg = ToolRegistry()
    for tool in build_fs_tools(PathGuard(tmp_path), sensitive_listing_mode=sensitive_listing_mode):
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

    result = registry(tmp_path).call("find_files", {"pattern": "*.py", "path": "."})

    assert result.ok
    assert "main.py" in result.content


def test_find_files_marks_sensitive_names_by_default(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=x", encoding="utf-8")

    result = registry(tmp_path).call("find_files", {"pattern": ".env", "path": "."})

    assert result.ok
    assert ".env [sensitive, content blocked]" in result.content


def test_large_file_truncates(tmp_path):
    (tmp_path / "large.txt").write_text("x" * 25000, encoding="utf-8")

    result = registry(tmp_path).call("read_file", {"path": "large.txt"})

    assert result.ok
    assert result.truncated
