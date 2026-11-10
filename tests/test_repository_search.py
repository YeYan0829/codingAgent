import subprocess

from codeagent.model_gateway.base import BaseModelClient, LLMResponse, LLMToolCall, ModelRequest
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.repository_search import RepositorySearchService
from codeagent.runtime.runner import AgentRunner
from codeagent.session.store import SessionStore
from codeagent.tools.fs_read import build_fs_tools
from codeagent.tools.git_read import build_git_tools
from codeagent.tools.registry import ToolRegistry
from codeagent.workspace.workspace import WorkspaceContext


def tools(tmp_path, *, executable=None):
    context = WorkspaceContext.source(tmp_path)
    service = RepositorySearchService(context, executable=executable)
    return {tool.name: tool for tool in build_fs_tools(context, search_service=service)}


def test_missing_ripgrep_is_tool_unavailable(tmp_path):
    result = tools(tmp_path, executable="/definitely/missing/rg")["search_text"].handler({"query": "needle"})

    assert not result.ok
    assert result.error_code == "tool_unavailable"


def test_find_files_is_sorted_and_reports_limit(tmp_path):
    for name in ("c.py", "a.py", "b.py"):
        (tmp_path / name).write_text("pass\n", encoding="utf-8")

    result = tools(tmp_path)["find_files"].handler({"include": ["*.py"], "max_results": 2})

    assert result.ok and result.truncated
    assert result.content.splitlines() == ["a.py", "b.py"]
    assert result.metadata["limit_reason"] == "max_results"


def test_search_and_read_reject_symlink_path(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("needle\n", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    registry = tools(tmp_path)

    searched = registry["search_text"].handler({"query": "needle", "path": "link.txt"})
    read = registry["read_file"].handler({"path": "link.txt"})

    assert not searched.ok and searched.error_code == "path_rejected"
    assert not read.ok and read.error_code == "path_rejected"


def test_model_schemas_reject_additional_properties(tmp_path):
    for tool in tools(tmp_path).values():
        assert tool.schema["additionalProperties"] is False


class ReadToolsModel(BaseModelClient):
    def complete(self, request: ModelRequest) -> LLMResponse:
        calls = sum(message.get("role") == "tool" for message in request.messages)
        sequence = [
            LLMToolCall(call_id="search", name="search_text", arguments={"query": "needle", "include": ["*.py"]}),
            LLMToolCall(call_id="status", name="git_status", arguments={}),
            LLMToolCall(call_id="diff", name="git_diff", arguments={"path": "module.py"}),
        ]
        return LLMResponse(tool_calls=[sequence[calls]]) if calls < len(sequence) else LLMResponse(text="readonly done")


def test_agent_loop_combines_search_status_and_diff_without_approval(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=tmp_path, check=True)
    target = tmp_path / "module.py"
    target.write_text("needle = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=tmp_path, check=True, capture_output=True)
    target.write_text("needle = 2\n", encoding="utf-8")
    context = WorkspaceContext.source(tmp_path)
    registry = ToolRegistry(workspace_context=context)
    for tool in build_fs_tools(context) + build_git_tools(context):
        registry.register(tool)
    store = SessionStore(tmp_path, session_root=tmp_path / "sessions").create()

    output = AgentRunner(store, ReadToolsModel(), registry, AutoApprovalGate(allow=False)).run_turn("inspect")

    assert output.final_text == "readonly done"
    assert [step["tool"] for step in output.steps] == ["search_text", "git_status", "git_diff"]
    assert all(step["decision"] == "allow" for step in output.steps)
    assert all(step["result"]["ok"] for step in output.steps)
