"""共享模型与工具执行；统一请求记录、权限处理和已知结果恢复。"""
from __future__ import annotations

import hashlib
import json

from codeagent.model_gateway.base import LLMResponse
from codeagent.runtime.policy import PolicyDecision
from codeagent.tools.base import ToolResult


class UnsafeRecovery(RuntimeError):
    pass


def model_identity(config):
    from dataclasses import asdict
    values = asdict(config)
    # 摘要策略不属于模型/价格记录身份；实际摘要请求已携带自己的 max_tokens。
    # 该记录不参与已保存响应的恢复准入。
    values.pop("summary_max_tokens", None)
    return {**values, "model": config.resolved_model, "reasoning_effort": config.resolved_reasoning_effort}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class ModelToolExecution:
    @staticmethod
    def readonly(registry, policy, call):
        try:
            tool = registry.get(call.name)
            if policy.evaluate(tool).decision != PolicyDecision.ALLOW:
                raise KeyError(call.name)
            return registry.call(call.name, call.arguments)
        except KeyError:
            return ToolResult(ok=False, error_code="policy_denied", error="只读子 Agent 禁止该工具；未执行。请使用已提供的文件工具；需命令、测试或历史资料时在报告中说明缺口，由主 Agent 处理。")

    @staticmethod
    def main(runner, call):
        decision, result = ModelToolExecution.dispatch(runner.tools, runner.policy, runner.approval_gate,
                                                     call, runner.session_store.append_event)
        return runner._step(call, decision, result)

    @staticmethod
    def dispatch(registry, policy, approval_gate, call, record):
        """主子复用同一权限与Approval处理；调用方选择自己的日志/历史范围。"""
        record("tool_requested", call.model_dump())
        try:
            tool = registry.get(call.name)
        except KeyError as exc:
            result = ToolResult(ok=False, error=str(exc), error_code="invalid_arguments")
            payload = {"call_id": call.call_id, "name": call.name, "result": result.model_dump()}
            record("tool_denied", payload)
            return "deny", result

        policy_result = policy.evaluate(tool)
        if policy_result.decision == PolicyDecision.DENY:
            result = ToolResult(ok=False, error=policy_result.reason, error_code="policy_denied")
            record("tool_denied", {"call_id": call.call_id, "name": call.name,
                                                            "reason": policy_result.reason, "result": result.model_dump()})
            return "deny", result

        if policy_result.decision in {PolicyDecision.ASK, PolicyDecision.REQUIRE_APPROVAL}:
            managed = bool(getattr(approval_gate, "manages_events", False))
            if not managed:
                record("approval_requested", {"call_id": call.call_id, "name": call.name, "reason": policy_result.reason})
            allowed = approval_gate is not None and approval_gate.request(tool, call)
            if not managed:
                record("approval_decision", {"call_id": call.call_id, "name": call.name, "allowed": allowed})
            if not allowed:
                result = ToolResult(ok=False, error="user denied approval", error_code="approval_denied")
                record("tool_denied", {"call_id": call.call_id, "name": call.name,
                                                                "reason": "user denied approval", "result": result.model_dump()})
                return "deny", result

        result = registry.call(call.name, call.arguments)
        result_payload = result.model_dump()
        record(
            "tool_result",
            {"call_id": call.call_id, "name": call.name, "arguments": call.arguments, "content": json.dumps(result_payload, ensure_ascii=False), "result": result_payload},
        )
        return policy_result.decision.value, result

    @staticmethod
    def complete(model, request):
        return model.complete(request)

    def __init__(self, store, executor, emit):
        self.store, self.executor, self.emit = store, executor, emit

    def request(self, key, request, agent_id):
        events = self.store.read_events()
        response = next((e for e in events if e.type == "model_response" and e.payload["operation_key"] == key), None)
        if response:
            return LLMResponse.model_validate(response.payload["response"])
        intent = next((e for e in events if e.type == "model_request_intent" and e.payload["operation_key"] == key), None)
        if intent:
            reserved = {e.payload["request_id"] for e in events
                        if e.type == "model_request_reserved" and e.payload.get("operation_key") == key}
            not_dispatched = {e.payload["request_id"] for e in events
                              if e.type == "model_request_settled" and e.payload.get("outcome") == "not_dispatched"}
            if reserved - not_dispatched:
                raise UnsafeRecovery("UNKNOWN model request retained; explicit new run required")
            self.emit("model_request_undispatched", {"operation_key": key, "request_digest": digest(request.model_dump())})
        else:
            self.emit("model_request_intent", {"operation_key": key, "agent_id": agent_id,
                                            "request_digest": digest(request.model_dump())})
        return self.executor.complete(request, agent_id=agent_id, operation_key=key)

    def tool(self, key, call, execute, *, readonly):
        events = self.store.read_events()
        prior = next((e for e in events if e.type == "tool_execution_result" and e.payload["operation_key"] == key), None)
        if prior:
            if prior.payload["call"] != call.model_dump():
                raise UnsafeRecovery("tool identity conflict")
            return prior.payload["output"]
        intent = next((e for e in events if e.type == "tool_execution_intent" and e.payload["operation_key"] == key), None)
        if intent and (not readonly or intent.payload["call"] != call.model_dump()):
            raise UnsafeRecovery("unknown main side effect; manual inspection required")
        if not intent:
            self.emit("tool_execution_intent", {"operation_key": key, "call": call.model_dump(), "readonly": readonly})
        output = execute(call)
        self.emit("tool_execution_result", {"operation_key": key, "call": call.model_dump(), "output": output})
        return output


class JournaledCondenser:
    """主 ContextManager 的摘要也使用稳定请求身份；UNKNOWN 不由旧摘要重试逻辑重发。"""
    def __init__(self, execution, run_id, agent_id="main"):
        self.execution, self.run_id = execution, run_id
        self.agent_id = agent_id
        self.last_key = None

    def complete(self, request):
        self.last_key = self.run_id + ":summary:" + digest(request.model_dump())
        return self.execution.request(self.last_key, request, self.agent_id)

    def commit(self, plan, items, *, history_store=None):
        # source prefix 由共用 ContextPreparation 在同一存储锁内核验；此处只关联请求与去重计量。
        store = history_store or self.execution.store
        with self.execution.executor.lock:
            response = next((e for e in self.execution.store.read_events() if e.type == "model_response"
                             and e.payload["operation_key"] == self.last_key), None)
            request_id = response.payload["request_id"] if response else None
            recorded = any(e.type in {"model_usage", "agent_model_usage"} and e.payload.get("request_id") == request_id
                           for e in self.execution.store.read_events()) if request_id else False
            items = [(kind, {**payload, **({"request_id": request_id} if request_id and kind == "model_usage" else {})})
                     for kind, payload in items if kind != "model_usage" or not recorded]
            return store.append_derived_events(items, turn_id=plan.turn_id)
