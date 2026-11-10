"""请求账本及可终止 transport；费用和请求次数仅用于统计。"""
from __future__ import annotations

import multiprocessing
from codeagent.runtime.execution_service import model_identity, digest
import os
import signal
import uuid
import copy

from codeagent.runtime.request_pricing import input_envelope, charge

from codeagent.model_gateway.base import LLMResponse, MalformedToolArgumentsError
from codeagent.runtime.turn_state import current_turn_state
from codeagent.session.store import _store_lock


class ExecutionInterrupted(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class RequestNotDispatched(ExecutionInterrupted):
    """调用方确认在发送 Provider 请求前拒绝；超时、取消和网络异常不能据此记零。"""


def _transport(connection, factory, config, request, parent_pid):
    # spawn 不继承主历史或已打开 SDK socket。Linux 上父进程异常退出也回收在途 transport。
    try:
        if os.name == "posix":
            os.setsid()
        if __import__("sys").platform == "linux":
            import ctypes
            ctypes.CDLL(None).prctl(1, signal.SIGKILL)
            if os.getppid() != parent_pid:
                return
        response = factory(config).complete(request)
        connection.send(("response", response.model_dump()))
    except MalformedToolArgumentsError as exc:
        connection.send(("malformed", {
            "tool_name": exc.tool_name, "message": exc.message, "line": exc.line,
            "column": exc.column, "raw_arguments": exc.raw_arguments,
            "usage": exc.usage.model_dump() if exc.usage else None,
            "provider_request_id": exc.provider_request_id, "finish_reason": exc.finish_reason,
            "text": exc.text, "reasoning_content": exc.reasoning_content,
        }))
    except RequestNotDispatched as exc:
        connection.send(("not_dispatched", exc.reason))
    except BaseException as exc:
        # 不把 provider 异常全文（可能含 token / URL）带到公开业务事件。
        connection.send(("error", type(exc).__name__))
    finally:
        connection.close()


class RequestExecutor:
    def __init__(self, store, config, model_config, factory, cancelled, *, client=None):
        self.store, self.config, self.model_config = store, config, model_config
        self.factory, self.cancelled, self.client = factory, cancelled, client
        self.lock = _store_lock(store.meta_path)
        with self.lock:
            self._initialize(store, config, model_config)

    def _initialize(self, store, config, model_config):
        self.turn_id = current_turn_state(store.read_events()).turn_id
        self.root_model_config = model_config
        self.local_cancelled = None
        self.execution_epoch = None
        self._stop_cursor = -1
        self._stop_reason = None
        events = store.read_events()
        start = next((e for e in events if e.turn_id == self.turn_id and e.type == "request_executor_started"), None)
        if start is None:
            self.emit("request_executor_started", {
                "legacy_steps": current_turn_state(events).used,
                "accounting_revision": 3, "ledger_id": self.turn_id,
                "root_model": model_identity(model_config), "backend": "runtime",
            })
        # 每笔请求独立保存价格。旧会话里的金额、次数和整轮期限不再控制当前执行。
        self.model_prices = dict(config.model_prices or {})
        self.model_token_prices = copy.deepcopy(config.model_token_prices or {})
        self.price, self.currency = config.cost_per_token_microunits, config.fee_currency
        self.accounting_revision = 3
        self.ledger_id = start.payload.get("ledger_id", self.turn_id) if start else self.turn_id

    def emit(self, kind, payload):
        with self.lock:
            if self.execution_epoch is not None:
                from codeagent.orchestration.lease import assert_execution_epoch
                assert_execution_epoch(self.store, self.execution_epoch)
            return self.store.append_derived_events([(kind, payload)], turn_id=self.turn_id)[0]

    def check(self, cancelled=None):
        meta = self.store.read_meta()
        if meta.get("current_turn_id") != self.turn_id:
            raise ExecutionInterrupted("root_turn_changed")
        if self._stop_cursor != meta.get("event_seq", 0):
            stops = [e for e in self.store.read_events() if e.turn_id == self.turn_id and e.type == "agent_root_stopping"]
            self._stop_reason = stops[-1].payload["reason"] if stops else None
            self._stop_cursor = meta.get("event_seq", 0)
        legacy_controls = {"steps", "slice_exhausted", "fee", "main_reserve", "budget_limited",
                           "closing_exhausted", "fee_bound_exceeded", "deadline"}
        if self._stop_reason not in {None, "output_truncation", "interrupted"} | legacy_controls:
            raise ExecutionInterrupted(self._stop_reason)
        if self.cancelled() or (self.local_cancelled and self.local_cancelled()) or (cancelled and cancelled()):
            raise ExecutionInterrupted("cancelled")

    def reserve(self, request, agent_id, cancelled=None, operation_key=None):
        with self.lock:
            self.check(cancelled)
            price = self.price_for(self.model_config)
            rates = self.rates_for(self.model_config)
            input_tokens = input_envelope(request)
            estimated = charge(rates, input_tokens, request.max_tokens) if rates else None
            request_id = "req-" + uuid.uuid4().hex[:16]
            self.emit("model_request_reserved", {
                "request_id": request_id, "agent_id": agent_id, "purpose": request.purpose,
                "operation_key": operation_key, "fee_reserved_microunits": 0,
                "fee_currency": self.currency, "max_tokens": request.max_tokens,
                "provider": self.model_config.provider, "model": self.model_config.resolved_model,
                "ledger_id": self.ledger_id, "token_price_microunits": price,
                "pricing_identity": self.pricing_identity(self.model_config),
                "accounting_revision": 3, "token_prices": rates, "input_token_ceiling": input_tokens,
                "fee_limit_enabled": False, "fee_estimate_microunits": estimated,
            })
            return request_id

    def complete(self, request, agent_id="main", cancelled=None, operation_key=None):
        request = request.model_copy(update={"max_tokens": (
            request.max_tokens if request.max_tokens is not None else (
                self.model_config.resolved_summary_max_tokens if request.purpose == "context_condenser"
                else self.model_config.resolved_max_tokens)
        ), "journaled": True})
        limit = self.model_config.limits.max_output_tokens or self.model_config.context_limit
        if not 1 <= request.max_tokens <= limit:
            raise ValueError("max_tokens outside model output bound")
        from codeagent.context.builder import estimate_request_tokens
        from codeagent.context.models import ModelCapabilities
        capacity = ModelCapabilities(self.model_config.context_limit, request.max_tokens)
        if estimate_request_tokens(request.messages, request.tools) > capacity.usable_input_budget:
            raise ExecutionInterrupted("context")
        request_id = self.reserve(request, agent_id, cancelled, operation_key)
        if self.client is not None and agent_id == "main":
            outcome, usage, provider_id = "unknown", None, None
            try:
                try:
                    response = self.client.complete(request)
                except MalformedToolArgumentsError as exc:
                    response = LLMResponse(text=exc.text, reasoning_content=exc.reasoning_content,
                        usage=exc.usage, provider_request_id=exc.provider_request_id, finish_reason=exc.finish_reason,
                        protocol_error={"tool": exc.tool_name[:128], "message": exc.message[:512],
                            "line": exc.line, "column": exc.column,
                            "fingerprint": digest({"tool": exc.tool_name, "arguments": exc.raw_arguments})})
                usage = response.usage.model_dump() if response.usage else None
                provider_id = response.provider_request_id
                self.check(cancelled)
                if operation_key is not None:
                    self.emit("model_response", {"operation_key": operation_key,
                        "request_id": request_id, "agent_id": agent_id, "response": response.model_dump()})
                outcome = "protocol_error" if response.protocol_error else "completed"
                return response
            except RequestNotDispatched:
                outcome = "not_dispatched"
                raise
            except Exception:
                outcome = "failed"
                raise
            finally:
                self.settle(request_id, agent_id, outcome, usage, provider_id)
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = None
        outcome = "unknown"
        usage = None
        provider_request_id = None
        try:
            # 可选 transport 工厂准备；普通工厂没有该钩子。实验控制在 benchmark 实现。
            prepare = getattr(self.factory, "prepare", None)
            factory = prepare(self.model_config, request) if prepare else self.factory
            process = context.Process(target=_transport, args=(
                sender, factory, self.model_config, request, os.getpid(),
            ), name="codeagent-" + request_id)
            process.start()
            sender.close()
            while True:
                if receiver.poll(0.02):
                    try:
                        kind, data = receiver.recv()
                    except EOFError as exc:
                        raise RuntimeError("model transport exited without response") from exc
                    # 先记录可靠的未派发回执；实验拒绝本身也可能同时触发取消。
                    if kind == "not_dispatched":
                        raise RequestNotDispatched(data)
                    if kind == "response":
                        response = LLMResponse.model_validate(data)
                        usage = response.usage.model_dump() if response.usage else None
                        provider_request_id = response.provider_request_id
                        # 晚到 usage 可用于计费，Stop 后仍不交付响应或执行工具。
                        self.check(cancelled)
                        if operation_key is not None:
                            self.emit("model_response", {
                                "operation_key": operation_key, "request_id": request_id,
                                "agent_id": agent_id, "response": response.model_dump(),
                            })
                        outcome = "completed"
                        return response
                    if kind == "malformed":
                        from codeagent.model_gateway.base import TokenUsage
                        usage = data["usage"]
                        provider_request_id = data["provider_request_id"]
                        self.check(cancelled)
                        data["usage"] = TokenUsage.model_validate(usage) if usage else None
                        outcome = "protocol_error"
                        if operation_key is not None and request.purpose in {"main_agent", "readonly_child", "worker_child"}:
                            # A known invalid response is recoverable input, not an UNKNOWN request.
                            # Persist it before projecting history so replay never pays again.
                            response = LLMResponse(
                                text=data.get("text"), reasoning_content=data.get("reasoning_content"),
                                finish_reason=data.get("finish_reason"), usage=data["usage"],
                                provider_request_id=provider_request_id, protocol_error={
                                    "tool": data["tool_name"][:128], "message": data["message"][:512],
                                    "line": data["line"], "column": data["column"],
                                    "fingerprint": digest({"tool": data["tool_name"], "arguments": data["raw_arguments"]}),
                                })
                            self.emit("model_response", {
                                "operation_key": operation_key, "request_id": request_id,
                                "agent_id": agent_id, "response": response.model_dump(),
                            })
                            return response
                        raise MalformedToolArgumentsError(**data)
                    self.check(cancelled)
                    outcome = "failed"
                    raise RuntimeError("model transport failed: " + data)
                self.check(cancelled)
                if not process.is_alive():
                    # response 可能在 poll 返回与退出检查之间入管道；先读同一次尝试的已到达结果。
                    # 交付响应前仍检查 Stop，不重发请求；EOF 走无可靠响应的失败路径。
                    if receiver.poll(0):
                        continue
                    raise RuntimeError("model transport exited without response")
        except RequestNotDispatched:
            outcome = "not_dispatched"
            raise
        except ExecutionInterrupted as exc:
            outcome = exc.reason
            raise
        finally:
            if process is not None and process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(1)
                if process.is_alive():
                    process.kill()
                    process.join()
                process.close()
            sender.close()
            receiver.close()
            self.settle(request_id, agent_id, outcome, usage, provider_request_id)

    def price_for(self, model):
        return self.model_prices.get(model.provider + "/" + model.resolved_model, self.price)

    def rates_for(self, model):
        profile = self.model_token_prices.get(model.provider + "/" + model.resolved_model)
        if profile is not None:
            return dict(profile)
        price = self.price_for(model)
        return {k: price for k in ("input", "cached_input", "output")} if price is not None else None

    def pricing_identity(self, model):
        from codeagent.runtime.execution_service import digest
        return digest({"model": model_identity(model), "price": self.price_for(model), "token_prices": self.rates_for(model), "currency": self.currency,
                       "ledger_id": self.ledger_id})

    def occupied_fee(self):
        return occupied_fee(self.store.read_events(), self.turn_id)

    def settle(self, request_id, agent_id, outcome, usage, provider_request_id=None):
        with self.lock:
            events = self.store.read_events()
            if any(e.type == "model_request_settled" and e.payload["request_id"] == request_id for e in events):
                return
            reserved = next(e.payload for e in events if e.type == "model_request_reserved" and e.payload["request_id"] == request_id)
            self.emit("model_request_settled", {
                "request_id": request_id, "agent_id": agent_id, "outcome": outcome,
                "usage": usage, "provider_request_id": provider_request_id,
                **({"fee_state": "not_dispatched", "fee_status": "not_dispatched",
                    "fee_settled_microunits": 0,
                    "fee_released_microunits": reserved["fee_reserved_microunits"]}
                   if outcome == "not_dispatched" else settlement(reserved, usage)),
            })


def settlement(reserved, usage):
    """仅完整且自洽的单次 usage 可释放差额；历史累计量从不相加。"""
    record_only = reserved.get("fee_limit_enabled") is False
    unknown = {"fee_status": "usage_unavailable" if record_only else "conservative_reservation_retained", "fee_state": "unknown"}
    price = reserved.get("token_price_microunits")
    rates = reserved.get("token_prices")
    if not usage:
        return unknown
    counts = [usage.get(k) for k in ("input_tokens", "output_tokens", "total_tokens")]
    if any(type(n) is not int or n < 0 for n in counts) or counts[0] + counts[1] != counts[2]:
        return unknown
    for key, bound in (("reasoning_tokens", counts[1]), ("cached_input_tokens", counts[0]),
                       ("cache_miss_input_tokens", counts[0])):
        value = usage.get(key)
        if value is not None and (type(value) is not int or not 0 <= value <= bound):
            return unknown
    cached, missed = usage.get("cached_input_tokens"), usage.get("cache_miss_input_tokens")
    if cached is not None and missed is not None and cached + missed != counts[0]:
        return unknown
    if price is None and rates is None:
        return {"fee_status": "price_unavailable", "fee_state": "unpriced"} if record_only else unknown
    if rates is not None:
        cached = cached if cached is not None else counts[0] - missed if missed is not None else 0
        cost = charge(rates, counts[0], counts[1], cached)
    else:
        cost = counts[2] * price
    if cost > reserved["fee_reserved_microunits"] and reserved.get("accounting_revision", 1) < 2:
        return unknown
    estimate = reserved.get("fee_estimate_microunits") if record_only else reserved["fee_reserved_microunits"]
    exceeded = ((estimate is not None and cost > estimate) or
                (reserved.get("accounting_revision", 1) >= 2 and
                 (counts[0] > reserved["input_token_ceiling"] or counts[1] > reserved["max_tokens"])))
    return {"fee_status": "settled_known", "fee_state": "settled_known",
            "fee_settled_microunits": cost,
            "fee_released_microunits": max(0, reserved["fee_reserved_microunits"] - cost),
            **({"fee_estimate_exceeded" if record_only else "fee_bound_exceeded": exceeded} if reserved.get("accounting_revision", 1) >= 2 else {})}


def occupied_fee(events, turn_id):
    settled = {}
    for event in events:
        if event.turn_id == turn_id and event.type == "model_request_settled":
            settled.setdefault(event.payload["request_id"], event.payload)
    reservations = {e.payload["request_id"]: e.payload for e in events
                    if e.turn_id == turn_id and e.type == "model_request_reserved"}
    return sum(settled[rid]["fee_settled_microunits"]
               if settled.get(rid, {}).get("fee_state") in {"settled_known", "not_dispatched"}
               else p["fee_reserved_microunits"] for rid, p in reservations.items())
