from importlib.resources import files

from codeagent.runtime.atomic_edit import AtomicEditService
from codeagent.tools.base import PermissionLevel, ToolSpec
from codeagent.tools.inputs import EditFileInput, WorkspaceEditInput


def build_edit_file_tool(service: AtomicEditService) -> ToolSpec:
    return ToolSpec.from_model("edit_file",
        files("codeagent.prompts").joinpath("edit_file.md").read_text(encoding="utf-8").strip(),
        PermissionLevel.WORKSPACE_WRITE, EditFileInput, service.edit_file)


def build_atomic_edit_tool(service: AtomicEditService) -> ToolSpec:
    return ToolSpec.from_model("apply_workspace_edit",
        "新建、删除、移动文件或跨文件事务修改；单个已有文件的替换用edit_file。"
        "create_file提供path/content，默认不覆盖，已存在需overwrite=true；replace_text提供path/old_text/new_text。"
        "这两种操作可省略op，delete_file/move_file必须显式指定op；移动目标必须不存在。"
        "expected_sha256可选，省略或null时按实际文件和唯一文本匹配检查，无需为取hash额外读取。"
        "整批先校验再写入，准备失败不写文件，提交失败尝试恢复涉及文件；无法确认恢复则保留现场。"
        "成功修改直接生效并记录局部undo，不提交Git，也不认证测试版本。",
        PermissionLevel.WORKSPACE_WRITE, WorkspaceEditInput, service.apply_workspace_edit)
