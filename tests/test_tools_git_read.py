import subprocess

from codeagent.tools.git_read import _parse_porcelain_v1_z, build_git_tools
from codeagent.workspace.workspace import WorkspaceContext


def test_git_status_reports_invalid_repository_clearly(tmp_path):
    tool = {tool.name: tool for tool in build_git_tools(WorkspaceContext.source(tmp_path))}["git_status"]

    result = tool.handler({})

    assert not result.ok
    assert result.error_code == "command_failed"
    assert "git command executed" in result.error
    assert "not a valid Git repository" in result.error


def git(cwd, *args):
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_git_status_parses_special_paths_and_git_diff_reads_content(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "tests@example.com")
    git(tmp_path, "config", "user.name", "Tests")
    target = tmp_path / "space name.py"
    target.write_text("value = 1\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-m", "base")
    target.write_text("value = 2\n", encoding="utf-8")
    tools = {tool.name: tool for tool in build_git_tools(WorkspaceContext.source(tmp_path))}

    status = tools["git_status"].handler({})
    diff = tools["git_diff"].handler({"path": "space name.py", "context_lines": 1})

    assert status.ok
    assert status.metadata["entries"][0]["path"] == "space name.py"
    assert diff.ok and "-value = 1" in diff.content and "+value = 2" in diff.content
    assert tools["git_status"].permission_level.value == "READ"
    assert tools["git_diff"].permission_level.value == "READ"


def test_git_diff_rejects_workspace_escape(tmp_path):
    tool = {tool.name: tool for tool in build_git_tools(WorkspaceContext.source(tmp_path))}["git_diff"]

    result = tool.handler({"path": "../outside"})

    assert not result.ok and result.error_code == "path_rejected"


def test_porcelain_parser_handles_rename_record_order():
    entries = _parse_porcelain_v1_z(b"R  new name.py\0old name.py\0")

    assert entries == [{"index": "R", "worktree": " ", "path": "new name.py", "original_path": "old name.py"}]
