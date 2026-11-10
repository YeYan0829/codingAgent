"""OpenHands TaskItem：来自固定的 task_tracker.py，仅复用数据类型。

来源：OpenHands software-agent-sdk 704cbe6015e3d59cabe04632175d99df2d448999
的 openhands-tools/openhands/tools/task_tracker/definition.py。
原始许可见同目录 LICENSE.OpenHands；字段及说明与固定上游一致。
"""
from typing import Literal
from pydantic import BaseModel, Field

TaskTrackerStatusType = Literal["todo", "in_progress", "done"]


class TaskItem(BaseModel):
    title: str = Field(..., description="A brief title for the task.")
    notes: str = Field("", description="Additional details or notes about the task.")
    status: TaskTrackerStatusType = Field(
        "todo",
        description="The current status of the task. One of 'todo', 'in_progress', or 'done'.",
    )
