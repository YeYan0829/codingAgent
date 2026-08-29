from codeagent.model_gateway.base import BaseModelClient, LLMResponse, LLMToolCall, ModelRequest
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.runner import AgentRunner
from codeagent.session.store import SessionStore
from codeagent.tools.git_read import build_git_tools
from codeagent.tools.registry import ToolRegistry
from codeagent.workspace.workspace import Workspace


class GitStatusModel(BaseModelClient):
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request: ModelRequest) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(tool_calls=[LLMToolCall(call_id="call_git_status", name="git_status", arguments={})])
        return LLMResponse(text="done")


def test_fixed_git_read_tool_does_not_require_approval(tmp_path):
    ws = Workspace(tmp_path)
    store = SessionStore(ws.root, session_root=tmp_path / "sessions").create()
    registry = ToolRegistry()
    for tool in build_git_tools(ws.context):
        registry.register(tool)
    runner = AgentRunner(store, GitStatusModel(), registry, AutoApprovalGate(allow=False))

    output = runner.run_turn("check git status")
    events = store.read_events()
    event_types = [event.type for event in events]

    assert output.steps[0]["tool"] == "git_status"
    assert output.steps[0]["decision"] == "allow"
    assert output.steps[0]["result"]["ok"] is False
    assert output.steps[0]["result"]["error_code"] == "command_failed"
    assert "approval_requested" not in event_types
    assert "approval_decision" not in event_types
    assert "tool_denied" not in event_types
    assert "tool_result" in event_types
