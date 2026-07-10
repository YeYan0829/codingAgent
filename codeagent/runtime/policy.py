from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from codeagent.tools.base import PermissionLevel, ToolSpec


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class PolicyResult:
    decision: PolicyDecision
    reason: str


class DefaultPolicy:
    def evaluate(self, tool: ToolSpec) -> PolicyResult:
        mapping = {
            PermissionLevel.READ: (PolicyDecision.ALLOW, "readonly tool is allowed"),
            PermissionLevel.EXEC_READONLY: (PolicyDecision.ASK, "readonly executable tool requires approval"),
            PermissionLevel.WRITE: (PolicyDecision.DENY, "write tools are disabled in v0.1"),
            PermissionLevel.NETWORK: (PolicyDecision.DENY, "network tools are disabled in v0.1"),
            PermissionLevel.DANGEROUS: (PolicyDecision.DENY, "dangerous tools are disabled in v0.1"),
        }
        decision, reason = mapping[tool.permission_level]
        return PolicyResult(decision=decision, reason=reason)
