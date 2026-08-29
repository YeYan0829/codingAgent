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
            PermissionLevel.CANDIDATE_WRITE: (PolicyDecision.ALLOW, "受控编辑只作用于 Session 的 Agent worktree"),
            PermissionLevel.NETWORK: (PolicyDecision.DENY, "network tools are disabled"),
            PermissionLevel.DANGEROUS: (PolicyDecision.DENY, "dangerous tools are disabled"),
        }
        decision, reason = mapping[tool.permission_level]
        return PolicyResult(decision=decision, reason=reason)
