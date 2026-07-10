from __future__ import annotations

import json
from dataclasses import dataclass, field

from codeagent.config import RuntimeConfig
from codeagent.context.builder import ContextBuilder
from codeagent.model_gateway.base import BaseModelClient, LLMToolCall
from codeagent.runtime.approval import ApprovalGate
from codeagent.runtime.policy import DefaultPolicy, PolicyDecision
from codeagent.session.store import SessionStore
from codeagent.tools.base import ToolResult
from codeagent.tools.registry import ToolRegistry


@dataclass
class RunnerOutput:
    final_text: str
    steps: list[dict] = field(default_factory=list)


class AgentRunner:
    def __init__(
        self,
        session_store: SessionStore,
        model: BaseModelClient,
        tools: ToolRegistry,
        approval_gate: ApprovalGate,
        policy: DefaultPolicy | None = None,
        config: RuntimeConfig | None = None,
    ) -> None:
        self.session_store = session_store
        self.model = model
        self.tools = tools
        self.approval_gate = approval_gate
        self.policy = policy or DefaultPolicy()
        self.config = config or RuntimeConfig()

    def run_turn(self, message: str) -> RunnerOutput:
        self.session_store.append_event("user_message", {"message": message})
        observations: list[dict] = []
        steps: list[dict] = []

        for _ in range(self.config.max_steps_per_turn):
            context = ContextBuilder(self.session_store).build(observations)
            response = self.model.complete(context)
            if not response.tool_calls:
                final = response.text or ""
                self.session_store.append_event("assistant_message", {"message": final})
                return RunnerOutput(final_text=final, steps=steps)

            for call in response.tool_calls:
                step = self._handle_tool_call(call)
                steps.append(step)
                observations.append({"call_id": call.call_id, "name": call.name, "result": step["result"]})

        final = "本轮达到最大工具调用步数，建议缩小任务范围或继续下一轮。"
        self.session_store.append_event("assistant_message", {"message": final})
        return RunnerOutput(final_text=final, steps=steps)

    def _handle_tool_call(self, call: LLMToolCall) -> dict:
        self.session_store.append_event("tool_requested", call.model_dump())
        try:
            tool = self.tools.get(call.name)
        except KeyError as exc:
            result = ToolResult(ok=False, error=str(exc))
            payload = {"call_id": call.call_id, "name": call.name, "result": result.model_dump()}
            self.session_store.append_event("tool_denied", payload)
            return {"tool": call.name, "decision": "deny", "result": result.model_dump()}

        policy_result = self.policy.evaluate(tool)
        if policy_result.decision == PolicyDecision.DENY:
            result = ToolResult(ok=False, error=policy_result.reason)
            self.session_store.append_event("tool_denied", {"call_id": call.call_id, "name": call.name, "reason": policy_result.reason})
            return {"tool": call.name, "decision": "deny", "result": result.model_dump()}

        if policy_result.decision == PolicyDecision.ASK:
            self.session_store.append_event("approval_requested", {"call_id": call.call_id, "name": call.name, "reason": policy_result.reason})
            allowed = self.approval_gate.request(tool, call)
            self.session_store.append_event("approval_decision", {"call_id": call.call_id, "name": call.name, "allowed": allowed})
            if not allowed:
                result = ToolResult(ok=False, error="user denied approval")
                self.session_store.append_event("tool_denied", {"call_id": call.call_id, "name": call.name, "reason": "user denied approval"})
                return {"tool": call.name, "decision": "deny", "result": result.model_dump()}

        result = tool.handler(call.arguments)
        result_payload = result.model_dump()
        self.session_store.append_event(
            "tool_result",
            {"call_id": call.call_id, "name": call.name, "arguments": call.arguments, "content": json.dumps(result_payload, ensure_ascii=False), "result": result_payload},
        )
        return {"tool": call.name, "decision": policy_result.decision.value, "result": result_payload}
