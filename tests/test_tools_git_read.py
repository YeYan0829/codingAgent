from codeagent.tools.git_read import build_git_tools
from codeagent.workspace.workspace import WorkspaceContext


def test_git_status_reports_invalid_repository_clearly(tmp_path):
    tool = {tool.name: tool for tool in build_git_tools(WorkspaceContext.source(tmp_path))}["git_status"]

    result = tool.handler({})

    assert not result.ok
    assert result.metadata["command"] == "git status --short"
    assert "git command executed" in result.error
    assert "not a valid Git repository" in result.error
