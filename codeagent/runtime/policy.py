from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from codeagent.tools.base import PermissionLevel, ToolSpec
from codeagent.runtime.command import CommandSpec
from codeagent.workspace.workspace import WorkspaceContext
from pathlib import PurePath
import sys


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
            PermissionLevel.WRITE: (PolicyDecision.DENY, "普通 write 工具未开放；只能使用受控 candidate 写入"),
            PermissionLevel.CANDIDATE_WRITE: (PolicyDecision.ALLOW, "受控 candidate 写入只作用于 execution worktree"),
            PermissionLevel.NETWORK: (PolicyDecision.DENY, "network tools are disabled"),
            PermissionLevel.DANGEROUS: (PolicyDecision.DENY, "dangerous tools are disabled"),
        }
        decision, reason = mapping[tool.permission_level]
        return PolicyResult(decision=decision, reason=reason)


class CommandPolicy:
    MAX_TIMEOUT_SECONDS = 300

    def evaluate(self, spec: CommandSpec, context: WorkspaceContext) -> PolicyResult:
        if spec.command_kind != "pytest":
            return PolicyResult(PolicyDecision.DENY, "仅允许 pytest 检查")
        if context.workspace_kind != "git_worktree":
            return PolicyResult(PolicyDecision.DENY, "命令只能在隔离的 Git worktree 中执行")
        if not 1 <= spec.timeout_seconds <= self.MAX_TIMEOUT_SECONDS:
            return PolicyResult(PolicyDecision.DENY, f"timeout 必须在 1..{self.MAX_TIMEOUT_SECONDS} 秒内")
        try:
            (context.active_root / spec.cwd).resolve().relative_to(context.active_root)
        except ValueError:
            return PolicyResult(PolicyDecision.DENY, "cwd 越出 active workspace")
        if spec.argv[:3] != (sys.executable, "-m", "pytest"):
            return PolicyResult(PolicyDecision.DENY, "pytest argv 未按系统规则固定")
        for target in spec.argv[3:]:
            path = PurePath(target)
            if target.startswith("-") or path.is_absolute():
                return PolicyResult(PolicyDecision.DENY, "target 必须是 workspace 内相对路径")
            try:
                (context.active_root / target).resolve().relative_to(context.active_root)
            except ValueError:
                return PolicyResult(PolicyDecision.DENY, "target 越出 active workspace")
        return PolicyResult(PolicyDecision.REQUIRE_APPROVAL, "合法 pytest 检查需要单次批准")
