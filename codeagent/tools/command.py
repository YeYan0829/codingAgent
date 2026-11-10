from codeagent.runtime.command_service import CommandLimits, CommandService
from codeagent.tools.base import PermissionLevel, ToolSpec


def command_tool_schema() -> dict:
    permission = {"oneOf": [
        {"type": "object", "properties": {
            "capability": {"type": "string", "enum": ["filesystem_read", "filesystem_write"]},
            "resource": {"type": "string", "minLength": 1}, "reason": {"type": "string", "minLength": 1, "maxLength": 500},
            "scope": {"type": "string", "enum": ["once", "session"]}},
         "required": ["capability", "resource", "reason", "scope"], "additionalProperties": False},
        {"type": "object", "properties": {
            "capability": {"type": "string", "const": "network"}, "reason": {"type": "string", "minLength": 1, "maxLength": 500},
            "scope": {"type": "string", "enum": ["once", "session"]}},
         "required": ["capability", "reason", "scope"], "additionalProperties": False},
    ]}
    return {"type": "object", "properties": {
                        "command": {"type": "string", "minLength": 1, "maxLength": CommandLimits.MAX_COMMAND_CHARS},
                        "cwd": {"type": "string", "default": ".",
                                "description": "相对于 Runtime Snapshot active_workspace 的目录；省略时为 Candidate 根目录"},
                        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": CommandLimits.MAX_TIMEOUT_SECONDS},
                        "purpose": {"type": "string", "enum": ["utility", "validation"], "default": "utility",
                                    "description": "证明修改正确性的测试/检查必须使用 validation；该模式启用 Bash pipefail，并可形成当前 revision 的验证证据。普通探索命令使用 utility。"},
                        "permissions": {"type": "array", "items": permission, "maxItems": CommandLimits.MAX_PERMISSIONS, "default": []}},
                        "required": ["command"], "additionalProperties": False}


def build_command_tool(service: CommandService) -> ToolSpec:
    return ToolSpec("run_command", "在当前 active_workspace 的 Linux 沙盒中运行命令；默认 cwd 是 Candidate 根目录，额外资源必须显式申请。",
                    PermissionLevel.READ, command_tool_schema(), service.run_command)
