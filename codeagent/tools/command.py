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
                        "cwd": {"type": "string", "default": "."},
                        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": CommandLimits.MAX_TIMEOUT_SECONDS},
                        "purpose": {"type": "string", "enum": ["utility", "validation"], "default": "utility"},
                        "permissions": {"type": "array", "items": permission, "maxItems": CommandLimits.MAX_PERMISSIONS, "default": []}},
                        "required": ["command"], "additionalProperties": False}


def build_command_tool(service: CommandService) -> ToolSpec:
    return ToolSpec("run_command", "在Candidate的Linux沙盒中运行任意shell命令；额外资源必须由Agent显式申请。",
                    PermissionLevel.READ, command_tool_schema(), service.run_command)
