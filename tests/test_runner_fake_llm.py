from codeagent.model_gateway.fake import FakeLLM
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.runner import AgentRunner
from codeagent.session.store import SessionStore
from codeagent.tools.fs_read import build_fs_tools
from codeagent.tools.git_read import build_git_tools
from codeagent.tools.registry import ToolRegistry
from codeagent.workspace.workspace import Workspace


def test_runner_fake_llm_completes_tool_loop(tmp_path):
    (tmp_path / "README.md").write_text("# Demo\n\nThis is a demo project.", encoding="utf-8")
    (tmp_path / "main.py").write_text("print('hi')", encoding="utf-8")
    ws = Workspace(tmp_path)
    store = SessionStore(ws.root).create()
    registry = ToolRegistry()
    for tool in build_fs_tools(ws.context) + build_git_tools(ws.context):
        registry.register(tool)
    runner = AgentRunner(store, FakeLLM(), registry, AutoApprovalGate(allow=False))

    output = runner.run_turn("帮我看看这个项目")
    event_types = [event.type for event in store.read_events()]

    assert output.final_text
    assert [step["tool"] for step in output.steps] == ["list_dir", "read_file"]
    assert event_types.count("tool_requested") == 2
    assert event_types.count("tool_result") == 2
    assert "assistant_message" in event_types
