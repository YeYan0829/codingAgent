from __future__ import annotations

from codeagent.runtime.command_service import CommandService
from codeagent.tools.base import PermissionLevel, ToolSpec


def build_run_check_tool(service: CommandService) -> ToolSpec:
    return ToolSpec(
        name="run_check",
        description="在隔离工作区中请求受控的 pytest 检查；不接受 shell 命令。",
        permission_level=PermissionLevel.READ,
        schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["pytest"]},
                "targets": {"type": "array", "items": {"type": "string"}, "description": "workspace 内相对路径列表"},
                "cwd": {"type": "string", "description": "相对 active workspace 的工作目录"},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
            },
            "required": ["kind", "targets"],
            "additionalProperties": False,
        },
        handler=service.run_check,
    )
