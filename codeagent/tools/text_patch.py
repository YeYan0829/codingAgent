from __future__ import annotations

from codeagent.runtime.edit_service import TextPatchService
from codeagent.tools.base import PermissionLevel, ToolSpec


def build_text_patch_tool(service: TextPatchService) -> ToolSpec:
    return ToolSpec(
        name="apply_text_patch",
        description="在 execution task worktree 内执行一次受控 UTF-8 文本替换或新增文件；不能删除文件或写 source。",
        permission_level=PermissionLevel.CANDIDATE_WRITE,
        schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对 active workspace 的文件路径"},
                "old_text": {"type": "string", "description": "必须唯一匹配的原文本；新增文件时为空"},
                "new_text": {"type": "string", "description": "替换后的 UTF-8 文本"},
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
        handler=service.apply_text_patch,
    )
