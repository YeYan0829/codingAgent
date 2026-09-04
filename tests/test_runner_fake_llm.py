from codeagent.model_gateway.fake import FakeLLM
from codeagent.config import ModelConfig
from codeagent.model_gateway.base import BaseModelClient, LLMResponse, LLMToolCall, ModelRequest, TokenUsage
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


def test_runner_reports_assistant_intent_and_tool_results_immediately(tmp_path):
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    ws = Workspace(tmp_path)
    store = SessionStore(ws.root, session_root=tmp_path / "sessions").create()
    registry = ToolRegistry()
    for tool in build_fs_tools(ws.context):
        registry.register(tool)

    class IntentModel(BaseModelClient):
        calls = 0

        def complete(self, request: ModelRequest) -> LLMResponse:
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    text="我先读取 README，确认项目背景。",
                    tool_calls=[LLMToolCall(call_id="read", name="read_file", arguments={"path": "README.md"})],
                )
            return LLMResponse(text="完成。")

    progress = []
    runner = AgentRunner(
        store,
        IntentModel(),
        registry,
        AutoApprovalGate(allow=False),
        progress_callback=progress.append,
    )

    output = runner.run_turn("检查项目")

    assert output.final_text == "完成。"
    assert progress[0] == {"type": "assistant_progress", "message": "我先读取 README，确认项目背景。"}
    assert progress[1]["type"] == "tool_step"
    assert progress[1]["step"]["tool"] == "read_file"
    tool_call_event = next(event for event in store.read_events() if event.type == "assistant_tool_calls")
    assert tool_call_event.payload["message"] == "我先读取 README，确认项目背景。"


def test_runner_passes_glm_context_capacity_to_context_manager(tmp_path):
    class FinalModel(BaseModelClient):
        def complete(self, request: ModelRequest) -> LLMResponse:
            return LLMResponse(text="done")

    ws = Workspace(tmp_path)
    store = SessionStore(ws.root, session_root=tmp_path / "sessions").create()
    runner = AgentRunner(
        store,
        FinalModel(),
        ToolRegistry(),
        AutoApprovalGate(allow=False),
        model_config=ModelConfig(provider="glm"),
    )
    captured = []

    def build(**kwargs):
        captured.append(kwargs["model_capabilities"])
        return [{"role": "user", "content": "finish"}]

    runner.context_manager.build = build
    output = runner.run_turn("test")

    assert output.status == "completed"
    assert captured[0].context_limit == 128_000
    assert captured[0].usable_input_budget == 118_000


def test_runner_persists_usage_for_every_model_request(tmp_path):
    class MeteredModel(BaseModelClient):
        def complete(self, request: ModelRequest) -> LLMResponse:
            return LLMResponse(
                text="done", provider_request_id="req-42",
                usage=TokenUsage(input_tokens=100, output_tokens=5, total_tokens=105),
            )

    ws = Workspace(tmp_path)
    store = SessionStore(ws.root, session_root=tmp_path / "sessions").create(provider="glm", model="glm-5.2")
    runner = AgentRunner(
        store, MeteredModel(), ToolRegistry(), AutoApprovalGate(allow=False),
        model_config=ModelConfig(provider="glm"),
    )

    runner.run_turn("test")

    event = next(event for event in store.read_events() if event.type == "model_usage")
    assert event.payload == {
        "provider": "glm",
        "model": "glm-5.2",
        "provider_request_id": "req-42",
        "usage": {
            "input_tokens": 100, "output_tokens": 5, "total_tokens": 105,
            "cached_input_tokens": None, "cache_miss_input_tokens": None, "reasoning_tokens": None,
        },
    }
