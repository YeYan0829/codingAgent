"""旧同步批次工具的兼容入口；实际执行完全交给共享AgentSessions。"""
from __future__ import annotations


from pydantic import BaseModel, ConfigDict, Field

from codeagent.runtime.policy import DefaultPolicy
from codeagent.tools.base import PermissionLevel, ToolResult, ToolSpec


class ChildTask(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    goal: str = Field(min_length=1, max_length=2000, description="可由文件调查独立回答的具体问题及范围。")
    done_when: str = Field(default="", max_length=1000, description="返回哪些证据即可汇报；能力不足时报告已确认部分和缺口，不要求执行命令或查Git历史。")
    context: str = Field(default="", max_length=4000, description="子不继承父对话：提供原要求、相关路径、事实/假设、父的工作边界。")


class DelegationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_key: str | None = Field(default=None, min_length=1, max_length=80)
    tasks: list[ChildTask] = Field(min_length=1, max_length=6, description="兼容批次，最多容纳2运行+4排队；已有任务占用容量时返回queue_full，不部分提交。")


def recover_delegations(store):
    """仅在持有 Session 执行权、没有存活 Runner 时调用；重启不重放工具。"""
    events = store.read_events()
    terminal = {e.payload["task_id"] for e in events if e.type == "delegation_task_finished"}
    for event in events:
        if event.type == "delegation_task_queued" and event.payload["task_id"] not in terminal:
            store.append_derived_events([("delegation_task_finished", {
                **event.payload, "state": "interrupted", "report": "",
                "error": "execution restarted; task was not resumed",
            })], turn_id=event.turn_id)
            terminal.add(event.payload["task_id"])
    settled = {e.payload["request_id"] for e in events if e.type == "model_request_settled"}
    for event in events:
        if event.type == "model_request_reserved" and event.payload["request_id"] not in settled:
            store.append_derived_events([("model_request_settled", {
                "request_id": event.payload["request_id"], "agent_id": event.payload["agent_id"],
                "outcome": "interrupted", "usage": None,
                "fee_status": "conservative_reservation_retained",
            })], turn_id=event.turn_id)
    resolved = {(e.turn_id, e.payload.get("call_id")) for e in events if e.type in {"tool_result", "tool_denied"}}
    for event in events:
        if event.type == "assistant_tool_calls":
            for call in event.payload.get("tool_calls", []):
                key = (event.turn_id, call["call_id"])
                if call["name"] == "delegate_readonly" and key not in resolved:
                    store.append_derived_events([("tool_denied", {
                        "call_id": call["call_id"], "name": call["name"],
                        "result": ToolResult(ok=False, error_code="interrupted", error="委派执行已中断，未重放").model_dump(),
                    })], turn_id=event.turn_id)
                    resolved.add(key)

class DelegationService:
    """旧批次工具的薄适配器，所有子请求都交给 AgentSessions 的共同循环。"""
    def __init__(self, store, executor, context_provider, parent_policy=None, *, parent_runtime=None):
        self.store, self.executor, self.context_provider = store, executor, context_provider
        self.parent_policy = parent_policy or DefaultPolicy()
        self.parent_runtime = parent_runtime
        self.sessions = None

    def tool(self):
        return ToolSpec("delegate_readonly", "兼容同步批次：提交独立只读文件调查并等待全部结果，最多按2运行/4排队容量准入；常规委派优先spawn_agent/send_agent。子使用当前共享目录和私有对话，历史结果可能过时。",
            PermissionLevel.SESSION_WRITE, DelegationRequest.model_json_schema(), self.execute, input_model=DelegationRequest)

    def execute(self, arguments):
        if self.sessions is None:
            from codeagent.orchestration.agents import AgentSessions
            self.sessions = AgentSessions(self.store, self.executor, self)
        return self.sessions.compat_delegate(arguments)
