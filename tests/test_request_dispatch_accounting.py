"""真实 spawn transport 的免费回归：拒绝派发、未知结果及账本消费者保持一致。"""
import os
from pathlib import Path

import pytest

from codeagent.benchmark.c4 import ledger_summary
from codeagent.benchmark.limits import ExperimentController, ExperimentLimits, LimitedFactory
from codeagent.model_gateway.base import BaseModelClient, LLMResponse, ModelRequest, TokenUsage
from codeagent.product.orchestration_view import usage_view
from codeagent.runtime.execution_service import ModelToolExecution, UnsafeRecovery
from codeagent.runtime.request_executor import RequestNotDispatched
from test_request_pricing import store, budget_for


class UsageClient(BaseModelClient):
    def complete(self, request):
        with Path(os.environ['DISPATCH_TRACE']).open('a') as stream:
            stream.write(request.purpose + '\n')
        return LLMResponse(text='observed', usage=TokenUsage(
            input_tokens=10, cached_input_tokens=8, output_tokens=2, total_tokens=12))


def usage_factory(config):
    return UsageClient()


def unknown_factory(config):
    from test_experiment_limits import BrokenClient
    return BrokenClient()


@pytest.mark.parametrize('purpose,agent_id,direct', [
    ('main_agent', 'main', False), ('main_agent', 'main', True),
    ('readonly_child', 'child', False), ('context_condenser', 'child', False),
])
def test_confirmed_rejection_survives_transport_and_external_cancel(store, tmp_path, monkeypatch, purpose, agent_id, direct):
    trace = tmp_path / 'dispatches'
    monkeypatch.setenv('DISPATCH_TRACE', str(trace))
    executor = budget_for(store)
    controller = ExperimentController(tmp_path / 'limit.json', ExperimentLimits(max_requests=1))
    executor.factory = LimitedFactory(usage_factory, controller, executor.config)
    if direct:
        executor.client = executor.factory(executor.model_config)
    executor.cancelled = controller.cancelled
    request = ModelRequest(messages=[], max_tokens=100, purpose=purpose)
    execution = ModelToolExecution(store, executor, executor.emit)
    execution.request('accepted', request, agent_id)
    with pytest.raises(RequestNotDispatched, match='experiment_request_limit'):
        execution.request('refused', request, agent_id)
    facts = store.read_events()
    assert trace.read_text().splitlines() == [purpose]
    with controller.state() as state:
        assert len(state['requests']) == 1
    settlements = [e.payload for e in facts if e.type == 'model_request_settled']
    assert [p['outcome'] for p in settlements] == ['completed', 'not_dispatched']
    refused = settlements[-1]
    assert refused['usage'] is None and refused['fee_settled_microunits'] == 0
    assert refused['fee_state'] == refused['fee_status'] == 'not_dispatched'
    summary = ledger_summary(facts)
    assert summary['reservation_count'] == 2 and summary['requests'] == 1
    assert summary['usage']['coverage'] == 'complete'
    assert summary['by_purpose'][purpose]['requests'] == 1
    assert not summary['unknown_or_pending_request_ids']
    assert not summary['execution_unknown_or_pending_request_ids']
    assert len(summary['requests_detail']) == 2
    view = usage_view(facts)
    assert view['requests'] == 1 and view['unknownUsageRequests'] == 0
    assert view['fees'][0]['unknownRequests'] == 0
    assert view['fees'][0]['settledMicrounits'] == executor.occupied_fee() == 88

    # 明确未发送的旧操作可以在显式调用时重新检查准入，不谎报 UNKNOWN。
    # 本次实验仍停止；不会因为已知零费用而放宽实验范围。
    executor.cancelled = lambda: False
    with pytest.raises(RequestNotDispatched, match='experiment_request_limit'):
        execution.request('refused', request, agent_id)
    assert trace.read_text().splitlines() == [purpose]


def test_ambiguous_transport_failure_keeps_unknown_and_blocks_replay(store, tmp_path):
    executor = budget_for(store)
    controller = ExperimentController(tmp_path / 'limit.json', ExperimentLimits(max_requests=2))
    executor.factory = LimitedFactory(unknown_factory, controller, executor.config)
    execution = ModelToolExecution(store, executor, executor.emit)
    request = ModelRequest(messages=[], max_tokens=100)
    with pytest.raises(RuntimeError, match='model transport failed'):
        execution.request('ambiguous', request, 'main')
    with pytest.raises(UnsafeRecovery, match='UNKNOWN'):
        execution.request('ambiguous', request, 'main')
    summary = ledger_summary(store.read_events())
    assert summary['requests'] == 1 and summary['usage']['coverage'] == 'unavailable'
    assert len(summary['unknown_or_pending_request_ids']) == 1
    assert len(summary['execution_unknown_or_pending_request_ids']) == 1
    view = usage_view(store.read_events())
    assert view['unknownUsageRequests'] == view['fees'][0]['unknownRequests'] == 1
    with controller.state() as state:
        assert len(state['requests']) == 1 and state['requests'][0]['status'] == 'unknown'


def test_only_rejected_attempts_have_zero_known_cost_without_fabricated_usage(store, tmp_path, monkeypatch):
    trace = tmp_path / 'dispatches'
    monkeypatch.setenv('DISPATCH_TRACE', str(trace))
    executor = budget_for(store)
    controller = ExperimentController(tmp_path / 'limit.json', ExperimentLimits(max_requests=0))
    executor.factory = LimitedFactory(usage_factory, controller, executor.config)
    executor.cancelled = controller.cancelled
    with pytest.raises(RequestNotDispatched):
        executor.complete(ModelRequest(messages=[], max_tokens=100))
    facts = store.read_events()
    summary = ledger_summary(facts)
    assert not trace.exists()
    assert summary['reservation_count'] == 1 and summary['requests'] == 0
    assert summary['usage']['coverage'] == 'complete'
    assert summary['usage']['total_tokens'] == 0
    assert summary['by_purpose'] == {}
    assert not summary['unknown_or_pending_request_ids']
    assert usage_view(facts) == {'requests': 0, 'knownTokens': 0, 'unknownUsageRequests': 0, 'fees': []}
    assert executor.occupied_fee() == 0


class RefusingClient(BaseModelClient):
    def complete(self, request):
        raise RequestNotDispatched('local_dispatch_refused')


def refusing_factory(config):
    return RefusingClient()


def test_explicit_non_dispatch_receipt_survives_subprocess_without_benchmark_hook(store):
    executor = budget_for(store)
    executor.factory = refusing_factory
    with pytest.raises(RequestNotDispatched, match='local_dispatch_refused'):
        executor.complete(ModelRequest(messages=[], max_tokens=100), agent_id='child')
    summary = ledger_summary(store.read_events())
    assert summary['requests'] == 0 and summary['reservation_count'] == 1
    assert not summary['unknown_or_pending_request_ids']
    assert summary['outcomes'] == {'not_dispatched': 1}


def test_transport_factories_share_one_experimental_registration_per_dispatch(store, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    trace = tmp_path / 'dispatches'
    monkeypatch.setenv('DISPATCH_TRACE', str(trace))
    executor = budget_for(store)
    controller = ExperimentController(tmp_path / 'limit.json', ExperimentLimits(max_requests=2))
    executor.factory = LimitedFactory(usage_factory, controller, executor.config)

    def attempt(index):
        try:
            executor.complete(ModelRequest(messages=[], max_tokens=100,
                purpose=['main_agent', 'readonly_child', 'context_condenser'][index % 3]),
                agent_id='main' if index == 0 else 'child')
            return 'completed'
        except RequestNotDispatched:
            return 'not_dispatched'

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert results.count('completed') == results.count('not_dispatched') == 2
    assert len(trace.read_text().splitlines()) == 2
    with controller.state() as state:
        assert len(state['requests']) == 2
        assert all(row['status'] == 'completed' for row in state['requests'])
    summary = ledger_summary(store.read_events())
    assert summary['reservation_count'] == 4 and summary['requests'] == 2
    assert summary['usage']['coverage'] == 'complete'
    assert not summary['unknown_or_pending_request_ids']
