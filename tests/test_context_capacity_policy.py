"""验证统一容量边界、默认继承及旧摘要兼容；不调用真实模型。"""
from dataclasses import replace

import pytest

from codeagent.config import MODEL_LIMITS, ModelConfig, ModelLimits, RuntimeConfig
from codeagent.context.builder import ContextManager, estimate_request_tokens
from codeagent.context.models import ModelCapabilities, ReadyContext, TOKEN_COUNT_MARGIN
from codeagent.model_gateway.base import LLMResponse, ModelRequest
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.request_executor import ExecutionInterrupted, RequestExecutor
from codeagent.runtime.runner import AgentRunner
from codeagent.session.store import SessionStore
from codeagent.tools.registry import ToolRegistry
from test_semantic_condensation import CondensingModel, _append_tool_step, _store


def test_builder_and_executor_accept_exact_capacity_and_reject_the_same_overflow(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "任务边界🙂"})
    manager = ContextManager(store)
    baseline = manager.build()
    tokens = estimate_request_tokens(baseline.messages)
    maximum = 4000
    # 以实际输入确定边界，避免测试依赖系统提示的具体长度。
    monkeypatch.setitem(MODEL_LIMITS, ("fake", "fake"), ModelLimits(tokens + maximum + TOKEN_COUNT_MARGIN, maximum))
    config = ModelConfig()
    prepared = manager.build(model_capabilities=config.context_capabilities())
    assert isinstance(prepared, ReadyContext)
    assert prepared.budget_report.estimated_tokens == tokens == prepared.budget_report.usable_tokens
    model = CondensingModel()
    executor = RequestExecutor(store, RuntimeConfig(), config, None, lambda: False, client=model)
    executor.complete(ModelRequest(messages=prepared.messages))
    assert len(model.requests) == 1
    too_large = [*prepared.messages, {"role": "user", "content": "多出的一条消息"}]
    with pytest.raises(ExecutionInterrupted, match="context"):
        executor.complete(ModelRequest(messages=too_large))
    assert len(model.requests) == 1
    assert sum(e.type == "model_request_reserved" for e in store.read_events()) == 1


def test_summary_and_agent_use_the_same_formula_with_an_explicit_old_override():
    config = ModelConfig(provider="glm", model="glm-5.3", reasoning_enabled=True)
    assert config.context_capabilities() == config.context_capabilities(summary=True)
    assert config.resolved_summary_max_tokens == 65536
    old = replace(config, summary_max_tokens=2048)
    assert old.context_capabilities(summary=True) == ModelCapabilities(1_000_000, 2048)
    assert old.context_capabilities().usable_input_budget == 1_000_000 - 65536 - TOKEN_COUNT_MARGIN
    assert old.context_capabilities(summary=True).usable_input_budget == 1_000_000 - 2048 - TOKEN_COUNT_MARGIN


def test_locally_long_summary_is_accepted_and_reused_after_restart(tmp_path, monkeypatch):
    monkeypatch.setattr("codeagent.context.builder.HISTORY_MAX_EVENTS", 4)
    store = _store(tmp_path)
    store.append_event("user_message", {"message": "原始要求"})
    for index in range(8):
        _append_tool_step(store, index, size=2000)
    store.append_event("assistant_message", {"message": "旧调查结束"})
    long_summary = "PENDING: " + "x" * 7200
    assert estimate_request_tokens([{"role": "user", "content": long_summary}]) > 2048
    model = CondensingModel([LLMResponse(text=long_summary, finish_reason="stop")])
    runner = AgentRunner(store, model, ToolRegistry(), AutoApprovalGate(False),
                        model_config=ModelConfig(summary_max_tokens=2048))
    assert runner.run_turn("继续工作").status == "completed"
    assert model.requests[0].max_tokens == 2048
    summaries = [e for e in store.read_events() if e.type == "context_condensed"]
    assert len(summaries) == 1 and summaries[0].payload["summary"] == long_summary
    assert any(e.type == "tool_result" and len(e.payload["result"]["content"]) >= 2000 for e in store.read_events())

    monkeypatch.setattr("codeagent.context.builder.HISTORY_MAX_EVENTS", 240)
    restored = SessionStore(store.workspace_root, store.session_id, store.session_root).load()
    next_model = CondensingModel()
    resumed = AgentRunner(restored, next_model, ToolRegistry(), AutoApprovalGate(False),
                          model_config=ModelConfig(summary_max_tokens=1000))
    assert resumed.run_turn("核对当前目录").status == "completed"
    assert [r.purpose for r in next_model.requests] == ["main_agent"]
    assert any(long_summary in m.get("content", "") for m in next_model.requests[0].messages)
