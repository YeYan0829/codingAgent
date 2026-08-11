from __future__ import annotations

import json
from dataclasses import dataclass, field

from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.context.builder import ContextBuilder
from codeagent.model_gateway.base import BaseModelClient, LLMToolCall, ModelRequest
from codeagent.runtime.approval import ApprovalGate
from codeagent.runtime.policy import DefaultPolicy, PolicyDecision
from codeagent.session.store import SessionStore
from codeagent.tools.base import ToolResult
from codeagent.tools.registry import ToolRegistry
from codeagent.workspace.workspace import WorkspaceContext
from codeagent.runtime.command import CommandArtifactStoreProtocol, CommandExecutor
from codeagent.runtime.command_service import CommandService
from codeagent.runtime.candidate import CandidateService
from codeagent.runtime.edit_service import TextPatchService
from codeagent.runtime.policy import CommandPolicy


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
        model_config: ModelConfig | None = None,
        workspace_context: WorkspaceContext | None = None,
        command_executor: CommandExecutor | None = None,
        command_policy: CommandPolicy | None = None,
        command_artifact_store: CommandArtifactStoreProtocol | None = None,
    ) -> None:
        self.session_store = session_store
        self.model = model
        self.tools = tools
        self.approval_gate = approval_gate
        self.policy = policy or DefaultPolicy()
        self.config = config or RuntimeConfig()
        self.model_config = model_config or ModelConfig()
        self.workspace_context = workspace_context or tools.workspace_context
        self.command_service: CommandService | None = None
        self.edit_service: TextPatchService | None = None
        self.candidate_service: CandidateService | None = None
        if command_executor is not None:
            if self.workspace_context is None:
                raise ValueError("command executor requires WorkspaceContext")
            self.command_service = CommandService(
                context=self.workspace_context,
                policy=command_policy or CommandPolicy(),
                approval_gate=approval_gate,
                executor=command_executor,
                session_store=session_store,
                artifact_store=command_artifact_store,
            )
            self.tools.register_run_check(self.command_service)
            self.edit_service = TextPatchService(self.workspace_context, session_store)
            self.candidate_service = CandidateService(self.workspace_context, session_store)
            self.tools.register_candidate_tools(self.edit_service, self.candidate_service)

    def run_turn(self, message: str) -> RunnerOutput:
        self.session_store.append_event("user_message", {"message": message})
        steps: list[dict] = []

        for _ in range(self.config.max_steps_per_turn):
            request = ModelRequest(
                messages=ContextBuilder(self.session_store).build(),
                tools=self.tools.as_model_tools(),
                model=self.model_config.resolved_model,
                temperature=self.model_config.temperature,
                max_tokens=self.model_config.max_tokens,
            )
            response = self.model.complete(request)
            if not response.tool_calls:
                final = response.text or ""
                self.session_store.append_event("assistant_message", {"message": final})
                return RunnerOutput(final_text=final, steps=steps)

            self.session_store.append_event("assistant_tool_calls", {"tool_calls": [call.model_dump() for call in response.tool_calls]})
            for call in response.tool_calls:
                steps.append(self._handle_tool_call(call))

        final = "本轮达到最大模型步骤数，建议缩小任务范围或继续下一轮。"
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
            return self._step(call, "deny", result)

        policy_result = self.policy.evaluate(tool)
        if policy_result.decision == PolicyDecision.DENY:
            result = ToolResult(ok=False, error=policy_result.reason)
            self.session_store.append_event("tool_denied", {"call_id": call.call_id, "name": call.name, "reason": policy_result.reason})
            return self._step(call, "deny", result)

        if policy_result.decision == PolicyDecision.ASK:
            self.session_store.append_event("approval_requested", {"call_id": call.call_id, "name": call.name, "reason": policy_result.reason})
            allowed = self.approval_gate.request(tool, call)
            self.session_store.append_event("approval_decision", {"call_id": call.call_id, "name": call.name, "allowed": allowed})
            if not allowed:
                result = ToolResult(ok=False, error="user denied approval")
                self.session_store.append_event("tool_denied", {"call_id": call.call_id, "name": call.name, "reason": "user denied approval"})
                return self._step(call, "deny", result)

        result = tool.handler(call.arguments)
        result_payload = result.model_dump()
        self.session_store.append_event(
            "tool_result",
            {"call_id": call.call_id, "name": call.name, "arguments": call.arguments, "content": json.dumps(result_payload, ensure_ascii=False), "result": result_payload},
        )
        return self._step(call, policy_result.decision.value, result)

    def _step(self, call: LLMToolCall, decision: str, result: ToolResult) -> dict:
        return {
            "tool": call.name,
            "arguments": call.arguments,
            "decision": decision,
            "result": result.model_dump(),
        }
