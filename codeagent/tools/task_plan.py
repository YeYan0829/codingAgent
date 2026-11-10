"""可选的进度工具。结构参考已固定的 OpenHands task_tracker，不复制其先 view 要求。"""
from codeagent.runtime.task_plan import UpdateTaskPlan
from codeagent.tools.base import PermissionLevel, ToolSpec
from codeagent.tools.inputs import EmptyInput


def build_task_plan_tools(service):
    return [
        ToolSpec.from_model("read_task_plan", "查看本轮已有进度清单。新用户轮次使用独立清单。",
                 PermissionLevel.READ, EmptyInput, service.read),
        ToolSpec("update_task_plan", "按需要替换本轮进度清单，可增删或调整工作项。进度由你判断；此工具不验证代码，也不是使用其他工具或最终回复的前置条件。无需先读取或建立清单。",
                 PermissionLevel.SESSION_WRITE, UpdateTaskPlan.model_json_schema(), service.update, input_model=UpdateTaskPlan),
    ]
