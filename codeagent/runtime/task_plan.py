"""可选的持久进度记录；不决定调查、编辑、命令或回复的资格。"""
from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from codeagent.runtime.turn_state import current_turn_state
from codeagent.session.store import _store_lock
from codeagent.tools.base import ToolResult
from codeagent.tools.upstream.task_tracker import TaskItem


class UpdateTaskPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tasks: list[TaskItem] = Field(max_length=100, description="替换本轮完整进度清单；可增删或调整，空列表清空。")


class TaskPlanService:
    def __init__(self, store, enabled=True):
        self.store, self.enabled = store, enabled

    @staticmethod
    def _turn(events):
        return next((e for e in reversed(events) if e.type == "user_message"), None)

    def view(self, *, events=None):
        events = self.store.read_events() if events is None else events
        turn = self._turn(events)
        last = next((e for e in reversed(events) if e.type == "task_plan_updated" and turn and e.turn_id == turn.turn_id), None)
        if last is None:
            return None
        # 旧计划仅读取进度；验证绑定及交付树保留在原事件，不恢复成当前准入条件。
        tasks = []
        for row in last.payload["tasks"]:
            status = {"pending": "todo", "completed": "done"}.get(row.get("status", "todo"), row.get("status", "todo"))
            notes = row.get("notes", row.get("note", ""))
            if status not in {"todo", "in_progress", "done"}:
                notes = f"历史状态 {status}。" + notes
                status = "todo"
            tasks.append({"title": row["title"], "status": status, "notes": notes})
        return {"tasks": tasks}

    def read(self, arguments):
        if arguments:
            return ToolResult(ok=False, error_code="invalid_arguments", error="read_task_plan无需参数")
        data = {"plan": self.view()}
        return ToolResult(ok=True, content=json.dumps(data, ensure_ascii=False), metadata=data)

    def update(self, arguments):
        try:
            command = UpdateTaskPlan.model_validate(arguments)
            tasks = [item.model_dump() for item in command.tasks]
            if any(not item["title"].strip() for item in tasks):
                raise ValueError("任务标题不能为空白")
            with _store_lock(self.store.meta_path):
                events = self.store.read_events()
                state = current_turn_state(events)
                if state.turn_id is None or state.closed:
                    raise ValueError("当前没有活跃用户轮次")
                self.store.append_derived_events([("task_plan_updated", {"tasks": tasks})], turn_id=state.turn_id)
            return self.read({})
        except (ValidationError, ValueError) as exc:
            return ToolResult(ok=False, error_code="task_plan_update_rejected", error=str(exc)[:2000])
