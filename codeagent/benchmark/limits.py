"""显式实验限额。普通 Runtime 不依赖本模块，也不解释这些停止条件。"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import fcntl
import json
import os
from pathlib import Path
import time
import uuid

from codeagent.model_gateway.base import BaseModelClient
from codeagent.runtime.request_pricing import charge, input_envelope
from codeagent.runtime.request_executor import RequestNotDispatched
from codeagent.session.store import _store_lock


class ExperimentLimitReached(RequestNotDispatched):
    pass


@dataclass(frozen=True)
class ExperimentLimits:
    max_requests: int | None = None
    timeout_seconds: float | None = None
    max_fee_microunits: int | None = None

    def __post_init__(self):
        for name in ("max_requests", "max_fee_microunits"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


@dataclass(frozen=True)
class ExperimentController:
    path: Path
    limits: ExperimentLimits

    @contextmanager
    def state(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _store_lock(self.path), self.path.with_suffix('.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = json.loads(self.path.read_text()) if self.path.exists() else {
                'started_at': time.time(), 'limits': asdict(self.limits), 'reason': None, 'requests': [],
            }
            if state['limits'] != asdict(self.limits):
                raise ValueError('experiment limits changed; use a new experiment')
            try:
                yield state
            finally:
                temporary = self.path.with_suffix('.tmp')
                with temporary.open('w') as stream:
                    json.dump(state, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(self.path)

    def _check(self, state):
        timeout = self.limits.timeout_seconds
        if timeout is not None and time.time() - state['started_at'] >= timeout:
            state['reason'] = 'experiment_deadline'
        return state['reason']

    def cancelled(self):
        with self.state() as state:
            return bool(self._check(state))

    @property
    def reason(self):
        with self.state() as state:
            return self._check(state)

    @property
    def remaining_seconds(self):
        with self.state() as state:
            timeout = self.limits.timeout_seconds
            return max(0, timeout - (time.time() - state['started_at'])) if timeout is not None else None

    def reserve(self, request, rates):
        with self.state() as state:
            reason = self._check(state)
            if not reason and self.limits.max_requests is not None and len(state['requests']) >= self.limits.max_requests:
                reason = 'experiment_request_limit'
            quote = charge(rates, input_envelope(request), request.max_tokens) if rates else None
            if not reason and self.limits.max_fee_microunits is not None:
                occupied = sum(row.get('actual', row['quote']) for row in state['requests'])
                if quote is None:
                    reason = 'experiment_price_required'
                elif occupied + quote > self.limits.max_fee_microunits:
                    reason = 'experiment_fee_limit'
            if reason:
                state['reason'] = reason
                raise ExperimentLimitReached(reason)
            ident = uuid.uuid4().hex
            state['requests'].append({'id': ident, 'quote': quote or 0, 'status': 'unknown'})
            return ident

    def settle(self, ident, response, rates):
        with self.state() as state:
            row = next(row for row in state['requests'] if row['id'] == ident)
            row['status'] = 'completed'
            usage = response.usage
            if rates and usage and usage.input_tokens + usage.output_tokens == usage.total_tokens:
                cached = usage.cached_input_tokens or 0
                row['actual'] = charge(rates, usage.input_tokens, usage.output_tokens, cached)
                if self.limits.max_fee_microunits is not None and row['actual'] > row['quote']:
                    state['reason'] = 'experiment_fee_estimate_exceeded'

    def rates_for(self, config, runtime_config):
        key = config.provider + '/' + config.resolved_model
        price = (runtime_config.model_prices or {}).get(key, runtime_config.cost_per_token_microunits)
        rates = (runtime_config.model_token_prices or {}).get(key)
        if rates is None and price is not None:
            rates = {name: price for name in ('input', 'cached_input', 'output')}
        return rates

    def wrap(self, client, config, runtime_config, *, reservation_id=None):
        return LimitedModel(client, self, self.rates_for(config, runtime_config), config, reservation_id)


class LimitedModel(BaseModelClient):
    def __init__(self, client, controller, rates, config, reservation_id=None):
        self.client, self.controller, self.rates = client, controller, rates
        self.config = config
        self.reservation_id = reservation_id

    def complete(self, request):
        if request.max_tokens is None:
            maximum = (self.config.resolved_summary_max_tokens if request.purpose == "context_condenser"
                       else self.config.resolved_max_tokens)
            request = request.model_copy(update={"max_tokens": maximum})
        ident = self.reservation_id or self.controller.reserve(request, self.rates)
        response = self.client.complete(request)
        self.controller.settle(ident, response, self.rates)
        return response


@dataclass(frozen=True)
class LimitedFactory:
    factory: object
    controller: ExperimentController
    runtime_config: object
    reservation_id: str | None = None

    def prepare(self, config, request):
        # 在父进程原子登记实验请求；拒绝回执不能被同一拒绝触发的取消抢先吞掉。
        # 子进程只使用这一个登记并结算，不再申请第二次实验请求。
        ident = self.controller.reserve(request, self.controller.rates_for(config, self.runtime_config))
        return replace(self, reservation_id=ident)

    def __call__(self, config):
        return self.controller.wrap(self.factory(config), config, self.runtime_config,
                                    reservation_id=self.reservation_id)
