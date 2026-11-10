"""固定子角色与OpenHands任务提示适配；不创建第二套执行器或动态插件框架。"""
from dataclasses import dataclass

from codeagent.runtime.policy import READONLY_CHILD_TOOLS
from codeagent.tools.upstream.task import TASK_TOOL_DESCRIPTION, TASK_TOOL_EXAMPLES


@dataclass(frozen=True)
class AgentSpec:
    name: str
    tool_names: tuple[str, ...] = tuple(sorted(READONLY_CHILD_TOOLS))
    description: str = "独立文件调查与静态审查。"
    prompt_file: str = "readonly_child.md"
    readonly: bool = True


_GIT = {"git_status", "git_diff", "git_diff_stat"}
_READ = READONLY_CHILD_TOOLS | _GIT
SPECS = {
    "explore": AgentSpec("explore"),
    "review": AgentSpec("review"),
    "code-explorer": AgentSpec("code-explorer", tuple(sorted(_READ)),
        "理解陌生代码：定位文件、调用关系和测试，返回路径/行号/片段。只读，无shell。",
        "subagent_code_explorer.md"),
    "bash-runner": AgentSpec("bash-runner", ("run_command", "read_observation"),
        "执行命令、测试、构建和检查，将输出归纳成状态、失败原因和来源；命令可能写项目。",
        "subagent_bash_runner.md", False),
    "general-purpose": AgentSpec("general-purpose", tuple(sorted(_READ | {
        "run_command", "edit_file", "apply_workspace_edit", "read_task_plan", "update_task_plan"})),
        "需要组合能力的独立实现：读取、编辑当前项目、运行命令、记录自己的可选进度。",
        "subagent_general_purpose.md", False),
}


def task_description():
    roles = "\n".join(f"- {s.name}: {s.description}" for s in SPECS.values())
    examples = "\n".join(TASK_TOOL_EXAMPLES[n] for n in ("code-explorer", "bash-runner"))
    examples += TASK_TOOL_EXAMPLES["general purpose"].replace('subagent_type="general purpose"',
                                                               'subagent_type="general-purpose"')
    text = TASK_TOOL_DESCRIPTION.format(agent_types_info=roles, task_tool_examples=examples)
    text = text.replace("use file_editor directly", "use edit_file/apply_workspace_edit directly")
    text = text.replace("- The agent's results are authoritative — verify subagent results only when the task involves judgment or\n  interpretation.",
        "- Use the returned evidence without repeating the whole investigation. Verify judgment, interpretation,\n  changed files, or the precise location needed for an edit; reports are observations, not guarantees.")
    return text + (
        "\n本地执行边界：task同步等待，返回task_id/subagent/status/text；resume使用先前task_id，保留该子会话历史。"
        "子方不继承父对话，prompt需自足；权限取角色、父Registry及父Policy交集，不递归。"
        "主子使用同一项目；一次只执行一个工具调用，task结束后主方才继续调用模型。"
        "命令沿用Approval/Bubblewrap，默认无网络，无宿主shell回退。"
    )
