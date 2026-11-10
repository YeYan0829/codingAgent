"""实验限额独立于产品 Runtime；免费模型验证跨进程共享及 UNKNOWN 不退款。"""
from concurrent.futures import ProcessPoolExecutor
import multiprocessing
import pytest

from codeagent.benchmark.limits import ExperimentController, ExperimentLimits, ExperimentLimitReached
from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.model_gateway.base import BaseModelClient, LLMResponse, ModelRequest


class KnownClient(BaseModelClient):
    def complete(self, request):
        return LLMResponse(text='sent')


class BrokenClient(BaseModelClient):
    def complete(self, request):
        raise RuntimeError('no reliable response')


def test_experiment_wrapper_uses_effective_output_limits_before_cost_estimation(tmp_path):
    class CaptureClient(KnownClient):
        requests = []

        def complete(self, request):
            self.requests.append(request)
            return super().complete(request)

    controller = ExperimentController(tmp_path / 'limits.json', ExperimentLimits())
    capture = CaptureClient()
    config = ModelConfig(provider="glm", model="glm-5.3", reasoning_enabled=True, summary_max_tokens=4096)
    client = controller.wrap(capture, config, RuntimeConfig(cost_per_token_microunits=1))
    client.complete(ModelRequest(messages=[]))
    client.complete(ModelRequest(messages=[], purpose="context_condenser"))
    client.complete(ModelRequest(messages=[], max_tokens=8192))
    assert [r.max_tokens for r in capture.requests] == [65536, 4096, 8192]
    with controller.state() as state:
        assert state['requests'][0]['quote'] >= 65536
        assert state['requests'][1]['quote'] >= 4096


def attempt(path):
    controller = ExperimentController(path, ExperimentLimits(max_requests=2))
    client = controller.wrap(KnownClient(), ModelConfig(), RuntimeConfig())
    try:
        return client.complete(ModelRequest(messages=[])).text
    except ExperimentLimitReached as exc:
        return str(exc)


def test_explicit_experiment_request_cap_is_shared_across_processes(tmp_path):
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context('spawn')) as pool:
        results = list(pool.map(attempt, [tmp_path / 'limits.json'] * 8))
    assert results.count('sent') == 2
    assert results.count('experiment_request_limit') == 6


def test_unknown_experimental_request_keeps_its_cost_estimate(tmp_path):
    controller = ExperimentController(tmp_path / 'limits.json', ExperimentLimits(max_fee_microunits=5200))
    broken = controller.wrap(BrokenClient(), ModelConfig(), RuntimeConfig(cost_per_token_microunits=1))
    with pytest.raises(RuntimeError, match='no reliable response'):
        broken.complete(ModelRequest(messages=[], max_tokens=4000))
    known = controller.wrap(KnownClient(), ModelConfig(), RuntimeConfig(cost_per_token_microunits=1))
    with pytest.raises(ExperimentLimitReached, match='experiment_fee_limit'):
        known.complete(ModelRequest(messages=[], max_tokens=4000))
    with controller.state() as state:
        assert len(state['requests']) == 1 and state['requests'][0]['status'] == 'unknown'


def test_experimental_deadline_is_external_and_cannot_reset_on_reconstruction(tmp_path, monkeypatch):
    controller = ExperimentController(tmp_path / 'limits.json', ExperimentLimits(timeout_seconds=10))
    with controller.state() as state:
        start = state['started_at']
    monkeypatch.setattr('codeagent.benchmark.limits.time.time', lambda: start + 11)
    again = ExperimentController(controller.path, controller.limits)
    assert again.cancelled() and again.reason == 'experiment_deadline'
    assert not hasattr(RuntimeConfig(), 'turn_timeout_seconds')


def test_changing_experimental_limits_requires_a_separate_experiment(tmp_path):
    controller = ExperimentController(tmp_path / 'limits.json', ExperimentLimits(max_requests=1))
    assert not controller.cancelled()
    with pytest.raises(ValueError, match='limits changed'):
        ExperimentController(controller.path, ExperimentLimits(max_requests=2)).cancelled()
