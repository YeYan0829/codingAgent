"""主角色适配器提供上下文与工具处理；推进、请求和恢复使用公共循环。"""
from dataclasses import asdict
import copy

from codeagent.context.models import ContextBudgetExceeded
from codeagent.runtime.execution_service import ModelToolExecution, UnsafeRecovery, JournaledCondenser
from codeagent.tools.base import PermissionLevel
from codeagent.orchestration.loop import AgentLoop
from codeagent.model_gateway.output_recovery import protocol_repair_exhausted, output_truncation_reason


class MainAdapter:
    purpose = "main_agent"
    agent_id = "main"

    def __init__(self, runner, sessions, execution_epoch):
        self.runner, self.sessions, self.store = runner, sessions, runner.session_store
        self.executor = copy.copy(runner.request_executor)
        self.executor.execution_epoch = execution_epoch
        self.run_id = "main:" + self.executor.turn_id
        self.execution_epoch = execution_epoch
        self.max_tokens = runner.model_config.resolved_max_tokens
        self.execution = ModelToolExecution(self.store, self.executor, self.emit)
        runner.condenser = JournaledCondenser(self.execution, self.run_id)
        self.steps = []
        runner._turn_steps = self.steps
        self.preexisting_tools = {e.payload["operation_key"] for e in self.store.read_events() if e.type == "tool_execution_result"}

    @property
    def registry(self):
        return self.runner.tools

    @property
    def request_reasoning_effort(self):
        # 与旧 Runner 复用同一恢复选择；合法动作后 continuation 消失，恢复原配置。
        return self.runner._main_request_reasoning_effort(
            recovering_output_truncation=bool(self.runner._truncation_continuation_messages())
        )

    def emit(self, kind, payload):
        from codeagent.orchestration.lease import assert_execution_epoch
        assert_execution_epoch(self.store, self.execution_epoch)
        return self.store.append_derived_events([(kind, {**payload, "execution_epoch": self.execution_epoch})], turn_id=self.executor.turn_id)[0]


    def check(self):
        self.executor.check()

    def prepare(self):
        r = self.runner
        references = {e.payload["run_id"] for e in self.store.read_events() if e.type == "child_report_observation"}
        # 同步task已将完整报告作为工具结果交付；不再以后台通知重复注入同一正文。
        references.update(e.payload["result"]["metadata"]["run_id"] for e in self.store.read_events()
            if e.type == "tool_result" and e.payload.get("name") == "task"
            and e.payload.get("result", {}).get("metadata", {}).get("run_id"))
        for event in self.store.read_events():
            if event.type == "agent_run_finished" and event.turn_id == self.executor.turn_id and event.payload["run_id"] not in references:
                self.emit("child_report_observation", {"run_id": event.payload["run_id"], "report_source_ref": "seq:" + str(event.seq)})
        controls = tuple(r._protocol_continuation_messages() + r._truncation_continuation_messages()
                         + self.sessions.validity_message())
        prepared = r._prepare_context(r.model_config.context_capabilities(), controls)
        if isinstance(prepared, ContextBudgetExceeded):
            self.sessions.drain(self.execution_epoch, cancel=True, reason="context")
            return asdict(r._terminate_context_budget(self.steps, prepared))
        return prepared if isinstance(prepared, list) else prepared.messages


    def record_response(self, response, iteration):
        r = self.runner
        key = self.run_id + ":response:" + str(iteration)
        previous = next((e for e in self.store.read_events() if e.payload.get("_execution_operation") == key), None)
        if not previous:
            response_event = next(e for e in self.store.read_events() if e.type == "model_response"
                                  and e.payload["operation_key"] == self.run_id + ":model:" + str(iteration))
            r._record_model_usage(response.usage, response.provider_request_id, response.finish_reason,
                                  reasoning_effort=self.request_reasoning_effort,
                                  request_id=response_event.payload["request_id"])
            detection = output_truncation_reason(response, self.max_tokens)
            if detection:
                output = r._record_output_truncation(self.steps, text=response.text or "", reasoning_content=response.reasoning_content,
                    finish_reason=response.finish_reason, detection=detection)
                # 可恢复的主响应截断没有改变既有子任务目标或权限；只在根真正终止时取消。
                if output is not None:
                    self.sessions.drain(self.execution_epoch, cancel=True, reason="output_truncation")
                # 截断事件也带稳定身份，恢复时不会重复增加截断次数。
                self.emit("output_truncation_projection", {"_execution_operation": key, "output": asdict(output) if output else None})
                return {"route": "finish" if output else "next", "output": asdict(output) if output else {}}
            if response.protocol_error:
                errors = []
                for event in reversed(self.store.read_events()):
                    if event.turn_id != self.executor.turn_id or event.type not in {
                            "assistant_tool_calls", "assistant_message", "model_protocol_error", "model_output_truncated"}:
                        continue
                    if event.type != "model_protocol_error":
                        break
                    errors.append(event.payload)
                stopped = protocol_repair_exhausted(response.protocol_error, errors)
                output = {"status": "failed", "final_text": "模型反复返回无法解析的工具参数；本批未执行，已有成果保留。"} if stopped else None
                self.emit("model_protocol_error", {**response.protocol_error, "attempt": len(errors) + 1,
                                                   "_execution_operation": key, "output": output})
                if stopped:
                    self.emit("turn_terminated", {"reason": "model_protocol_error", "message": output["final_text"]})
                return {"route": "finish" if stopped else "next", "output": output or {}}
            if len({c.call_id for c in response.tool_calls}) != len(response.tool_calls):
                raise ValueError("invalid main tool call ids/count")
            if response.tool_calls:
                known = {c["call_id"] for e in self.store.read_events() if e.turn_id == self.executor.turn_id and e.type == "assistant_tool_calls"
                         for c in e.payload["tool_calls"]}
                if known.intersection(c.call_id for c in response.tool_calls):
                    raise ValueError("duplicate main tool call_id")
                self.store.append_event("assistant_tool_calls", {"message": response.text or "", "reasoning_content": response.reasoning_content,
                    "tool_calls": [c.model_dump() for c in response.tool_calls], "_execution_operation": key})
                if response.text:
                    r._notify({"type": "assistant_progress", "message": response.text})
            else:
                self.sessions.finish_parent(self.execution_epoch)
                text = response.text or ""
                self.store.append_event("assistant_message", {"message": text, "reasoning_content": response.reasoning_content,
                                                              "_execution_operation": key})
        elif previous.type == "graph_closing_rejected":
            return {"route": "finish", "output": previous.payload["output"]}
        elif previous.type == "graph_final_deferred":
            return {"route": "next"}
        elif previous.type == "output_truncation_projection":
            return {"route": "finish" if previous.payload["output"] else "next", "output": previous.payload["output"] or {}}
        elif previous.type == "model_protocol_error":
            return {"route": "finish" if previous.payload["output"] else "next", "output": previous.payload["output"] or {}}
        if response.tool_calls:
            return {"route": "tools"}
        final = next(e for e in self.store.read_events() if e.payload.get("_execution_operation") == key)
        return {"route": "finish", "output": {"status": "completed", "final_text": final.payload["message"]}}

    def execute(self, call, key):
        r = self.runner
        # 主工具 result 已落账但 wrapper checkpoint/result 尚未落账，同样可安全重建。
        terminal = next((e for e in self.store.read_events() if e.turn_id == self.executor.turn_id and e.type in {"tool_result", "tool_denied"}
                         and e.payload.get("call_id") == call.call_id), None)
        if terminal and terminal.payload.get("result"):
            from codeagent.tools.base import ToolResult
            output = r._step(call, "recovered", ToolResult.model_validate(terminal.payload["result"]))
        else:
            try:
                tool = self.registry.get(call.name)
                # 命令与文件写入保留未知副作用保护；读取/可幂等会话控制按操作键恢复。
                safe = tool.permission_level in {PermissionLevel.READ, PermissionLevel.SESSION_WRITE} and call.name != "run_command"
            except KeyError:
                safe = True
            try:
                from codeagent.orchestration.agents import ACTIVE_TOOL_OPERATION
                token = ACTIVE_TOOL_OPERATION.set(key)
                try:
                    output = self.execution.tool(key, call, r._handle_tool_call, readonly=safe)
                finally:
                    ACTIVE_TOOL_OPERATION.reset(token)
            except UnsafeRecovery:
                self.store.mark_recovery_required("主工具 intent 无可靠 result；禁止自动重放，请人工检查")
                raise
        if key not in self.preexisting_tools:
            self.steps.append(output)
            r._notify({"type": "tool_step", "step": output})
        if output.get("result", {}).get("error_code") == "execution_cancelled":
            from codeagent.runtime.request_executor import ExecutionInterrupted
            raise ExecutionInterrupted("cancelled")

    def finish(self, output):
        return {**output, "steps": self.steps, "steps_used_in_turn": self.runner._steps_used_in_current_turn()}


def run_main_loop(runner, sessions, execution_epoch):
    from codeagent.runtime.runner import RunnerOutput
    adapter = MainAdapter(runner, sessions, execution_epoch)
    try:
        sessions.recover(execution_epoch)
        result = AgentLoop(adapter).invoke()
        # 结束或失败均回收子工作；普通循环推进不触发回收。
        sessions.drain(execution_epoch, cancel=result.get("status") not in {"completed", "completed_with_warnings"}, reason=result.get("status", "cancelled"))
        return RunnerOutput(**result)
    except BaseException as exc:
        reason = getattr(exc, "reason", "runtime_error" if isinstance(exc, Exception) else "interrupted")
        if isinstance(exc, KeyboardInterrupt):
            reason = "cancelled"
        sessions.drain(execution_epoch, cancel=True, reason=reason)
        raise
