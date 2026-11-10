from codeagent.runtime.command_service import CommandService
from codeagent.tools.base import PermissionLevel, ToolSpec
from codeagent.tools.inputs import CommandInput

_COMMAND_BASE = (
    "在当前项目的 Linux 沙盒中运行项目命令，默认 cwd 为 active_workspace。每次是 fresh shell，export/source/cd 不跨调用保留；"
    "临时文件是否跨命令保留，以执行环境的 file_lifetime_guidance 为准；需要复用的脚本或数据放在其中说明的持久目录。"
    "依赖未知时可参考当前 Runtime 上下文中的执行环境、项目配置或探测解释器。"
)
_COMMAND_TAIL = (
    "按原样执行 shell 命令，不替你解释或改写测试流程。"
    "检查 exit code 和 stdout/stderr，purpose 只标记用途，不认证当前代码版本。"
    "环境错误不能当作测试通过。网络及额外宿主资源通过 permissions 申请，拒绝后不得绕过。"
)
RUN_COMMAND_DESCRIPTION = _COMMAND_BASE + "运行测试或检查时可用 purpose=validation，普通调查用utility。" + _COMMAND_TAIL
RUN_COMMAND_DESCRIPTION_UNTRACKED = RUN_COMMAND_DESCRIPTION


def command_tool_schema(task_tracking: bool = True) -> dict:
    return CommandInput.model_json_schema()


def build_command_tool(service: CommandService, task_tracking: bool = True) -> ToolSpec:
    """清单开关不改变命令能力；执行权限不等于READ。"""
    return ToolSpec.from_model("run_command", RUN_COMMAND_DESCRIPTION,
                              PermissionLevel.WORKSPACE_EXECUTE, CommandInput, service.run_command)
