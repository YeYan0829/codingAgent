"""费用信息不能改变正常操作链路；使用真实图和本地模型替身，不访问 Provider。"""
from dataclasses import replace

import pytest

from test_request_pricing import store, reservation
from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.model_gateway.base import BaseModelClient, LLMResponse, LLMToolCall, ModelRequest, TokenUsage
from codeagent.runtime.request_executor import RequestExecutor, ExecutionInterrupted
from codeagent.runtime.turn_state import current_turn_state


def accounting(store, **options):
    config = RuntimeConfig(**options)
    store.append_event("user_message", {"message": "keep working", "max_model_steps_per_user_turn": None})
    model = ModelConfig(provider="deepseek")
    return RequestExecutor(store, config, model, None, lambda: False)


def test_unpriced_requests_remain_known_execution_and_have_no_money_hold(store):
    budget = accounting(store)
    request = ModelRequest(messages=[], max_tokens=100)
    for _ in range(52):  # 穿过旧的 48 次默认上限；主、子、摘要都记账。
        rid = budget.reserve(request, "child" if _ % 2 else "main")
        budget.settle(rid, "main", "completed", {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    assert current_turn_state(store.read_events()).used == 52
    assert not hasattr(budget, "deadline_epoch")
    assert all(e.payload["fee_reserved_microunits"] == 0 for e in store.read_events() if e.type == "model_request_reserved")
    from codeagent.benchmark.c4 import ledger_summary
    report = ledger_summary(store.read_events())
    assert report["requests"] == 52 and report["usage"]["total_tokens"] == 780
    assert len(report["unpriced_request_ids"]) == 52
    assert report["execution_unknown_or_pending_request_ids"] == []
    assert report["unknown_or_pending_request_ids"] == []
    from codeagent.product.orchestration_view import usage_view
    assert usage_view(store.read_events())["fees"][0]["unpricedRequests"] == 52


def test_estimate_overrun_and_missing_usage_do_not_block_following_work(store):
    budget = accounting(store, cost_per_token_microunits=100000000)
    request = ModelRequest(messages=[], max_tokens=10)
    rid = budget.reserve(request, "main")
    n = reservation(store, rid)["input_token_ceiling"] + 1
    budget.settle(rid, "main", "completed", {"input_tokens": n, "output_tokens": 20, "total_tokens": n + 20})
    row = next(e.payload for e in store.read_events() if e.type == "model_request_settled")
    assert row["fee_estimate_exceeded"] and "fee_bound_exceeded" not in row
    rid = budget.reserve(request, "child")
    budget.settle(rid, "child", "completed", None)
    budget.reserve(request.model_copy(update={"purpose": "context_condenser"}), "main")


def test_changing_reporting_price_keeps_old_cost_and_child_identity(store):
    budget = accounting(store, cost_per_token_microunits=2)
    request = ModelRequest(messages=[], max_tokens=10)
    rid = budget.reserve(request, "main")
    budget.settle(rid, "main", "completed", {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    newer = RequestExecutor(store, replace(budget.config, cost_per_token_microunits=7, fee_currency="CNY"), budget.model_config, None, lambda: False)
    next_id = newer.reserve(request, "main")
    assert reservation(store, rid)["token_price_microunits"] == 2
    assert reservation(store, next_id)["token_price_microunits"] == 7
    assert next(e.payload for e in store.read_events() if e.type == "model_request_settled")["fee_settled_microunits"] == 30


class ReadAndFinish(BaseModelClient):
    def complete(self, request):
        assert request.tools
        assert not any("费用" in m.get("content", "") or "预算收尾" in m.get("content", "") for m in request.messages if m["role"] == "system")
        count = sum(m["role"] == "tool" for m in request.messages)
        usage = None if request.model == "missing-usage" else TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
        if count < 2:
            return LLMResponse(tool_calls=[LLMToolCall(call_id=f"read-{count}", name="read_file", arguments={"path": "README.md"})], usage=usage)
        return LLMResponse(text="已读取并核对。", usage=usage)


def read_factory(config):
    return ReadAndFinish()


@pytest.mark.parametrize("price,missing", [(None, False), (2, False), (10**12, False), (2, True)])
def test_actual_loop_keeps_same_actions_across_prices(store, price, missing):
    from codeagent.application.session_runtime import build_agent_runner
    from codeagent.runtime.approval import AutoApprovalGate
    runner = build_agent_runner(store, model_config=ModelConfig(model="missing-usage" if missing else None),
        approval_gate=AutoApprovalGate(False), model_factory=read_factory,
        runtime_config=RuntimeConfig(cost_per_token_microunits=price))
    outcome = runner.run_turn("读取 README.md 两次并核对")
    assert outcome.status == "completed", outcome
    assert not any(e.type == "execution_slice_exhausted" for e in store.read_events())
    events = store.read_events()
    assert [e.payload["name"] for e in events if e.type == "tool_result"] == ["read_file", "read_file"]
    assert len([e for e in events if e.type == "model_request_reserved"]) == 3
    assert "delegate_readonly" not in {t.name for t in runner.tools.as_model_tools()}
    assert not any(e.type in {"budget_closing_started", "turn_budget_exhausted"} for e in events)


def test_legacy_limits_do_not_bind_current_execution(store):
    store.update_runtime_options({"max_steps_per_turn": 1, "max_model_steps_per_user_turn": 1,
                                  "max_fee_microunits": 0, "turn_timeout_seconds": .001,
                                  "main_closing_requests": 2})
    store.append_event("user_message", {"message": "continue", "max_model_steps_per_user_turn": 1})
    store.append_event("request_executor_started", {
        "legacy_steps": 0, "deadline_epoch": 1, "max_fee_microunits": 0,
        "main_closing_requests": 2, "accounting_revision": 2,
        "cost_per_token_microunits": 1, "fee_currency": "USD",
    })
    store.append_event("turn_budget_exhausted", {"max_model_steps_per_user_turn": 1})
    config = RuntimeConfig.from_options(store.read_meta()["runtime_options"])
    assert not {"max_steps_per_turn", "max_model_steps_per_user_turn", "max_fee_microunits",
                "main_closing_requests", "turn_timeout_seconds"} & vars(config).keys()
    ledger = RequestExecutor(store, config, ModelConfig(), None, lambda: False)
    for _ in range(3):
        ledger.reserve(ModelRequest(messages=[], max_tokens=10), "main")
    state = current_turn_state(store.read_events())
    assert state.used == 3 and not state.closed
    assert not hasattr(ledger, "deadline_epoch")


def test_legacy_budget_wait_is_readable_and_can_continue_without_expansion(store):
    from codeagent.application.session_runtime import build_agent_runner
    from codeagent.runtime.approval import AutoApprovalGate
    from codeagent.product.read_model import turn_budget_view
    store.append_event("user_message", {"message": "read README twice", "max_model_steps_per_user_turn": 1})
    store.append_event("turn_budget_exhausted", {"max_model_steps_per_user_turn": 1})
    runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
                                model_factory=read_factory)
    output = runner.continue_turn()
    assert output.status == "completed", output
    assert sum(e.type == "user_message" for e in store.read_events()) == 1
    projection = turn_budget_view(store.read_meta(), store.read_events())
    assert projection["closed"] and not projection["waiting"] and projection["limit"] is None
