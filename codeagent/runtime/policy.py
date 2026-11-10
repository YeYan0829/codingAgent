from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from codeagent.tools.base import PermissionLevel, ToolSpec


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class PolicyResult:
    decision: PolicyDecision
    reason: str


class DefaultPolicy:
    def evaluate(self, tool: ToolSpec) -> PolicyResult:
        mapping = {
            PermissionLevel.READ: (PolicyDecision.ALLOW, "readonly tool is allowed"),
            PermissionLevel.EXEC_READONLY: (PolicyDecision.ASK, "readonly executable tool requires approval"),
            PermissionLevel.WRITE: (PolicyDecision.DENY, "普通 write 工具未开放；只能使用受控 workspace 编辑"),
            PermissionLevel.WORKSPACE_WRITE: (PolicyDecision.ALLOW, "受控编辑只作用于当前项目内获准路径"),
            PermissionLevel.WORKSPACE_EXECUTE: (PolicyDecision.ALLOW, "项目命令由CommandService检查资源权限并通过沙箱执行"),
            PermissionLevel.SESSION_WRITE: (PolicyDecision.ALLOW, "会话状态与子任务控制；项目操作仍遵守各自角色权限"),
            PermissionLevel.NETWORK: (PolicyDecision.DENY, "network tools are disabled"),
            PermissionLevel.DANGEROUS: (PolicyDecision.DENY, "dangerous tools are disabled"),
        }
        decision, reason = mapping[tool.permission_level]
        return PolicyResult(decision=decision, reason=reason)


READONLY_CHILD_TOOLS = frozenset({"list_dir", "show_tree", "read_file", "search_text", "find_files", "read_observation"})


class ReadOnlyChildPolicy(DefaultPolicy):
    def evaluate(self, tool: ToolSpec) -> PolicyResult:
        if tool.name not in READONLY_CHILD_TOOLS or tool.permission_level != PermissionLevel.READ:
            return PolicyResult(PolicyDecision.DENY, "子 Agent 仅允许注册的只读文件及本会话观察工具")
        return super().evaluate(tool)


class ChildRolePolicy:
    """角色工具范围与父Policy取交集；角色提示不授予额外权限。"""
    def __init__(self, spec, parent):
        self.spec, self.parent = spec, parent

    def evaluate(self, tool):
        if tool.name not in self.spec.tool_names or (self.spec.readonly and tool.permission_level != PermissionLevel.READ):
            return PolicyResult(PolicyDecision.DENY, "工具不属于本次子角色权限")
        return self.parent.evaluate(tool)
