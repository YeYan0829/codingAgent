from codeagent.interface.commands import handle_manual_call
from codeagent.model_gateway.fake import FakeLLM
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.runner import AgentRunner
from codeagent.session.store import SessionStore
from codeagent.tools.fs_read import build_fs_tools
from codeagent.tools.registry import ToolRegistry
from codeagent.workspace.workspace import Workspace


class DummyConsole:
    def print(self, *args, **kwargs):
        pass


def test_manual_call_logs_cli_command_to_events_without_duplicate_transcript(tmp_path):
    (tmp_path / "README.md").write_text("hello", encoding="utf-8")
    ws = Workspace(tmp_path)
    store = SessionStore(ws.root).create()
    registry = ToolRegistry()
    for tool in build_fs_tools(ws.context):
        registry.register(tool)
    runner = AgentRunner(store, FakeLLM(), registry, AutoApprovalGate(allow=False))

    handle_manual_call(DummyConsole(), runner, '/call read_file {"path":"README.md"}')

    events = store.read_events()
    assert any(event.type == "cli_command" for event in events)
    assert not (store.session_dir / "transcript.md").exists()
