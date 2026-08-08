from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from codeagent.tools.base import PermissionLevel, ToolResult, ToolSpec
from codeagent.workspace.workspace import WorkspaceContext

EMPTY_SCHEMA = {"type": "object", "properties": {}, "required": []}


def build_git_tools(context: WorkspaceContext) -> list[ToolSpec]:
    def run_git(args: dict[str, Any], command: list[str]) -> ToolResult:
        try:
            proc = subprocess.run(command, cwd=context.active_root, text=True, capture_output=True, timeout=10, check=False)
            content = proc.stdout.strip() or proc.stderr.strip()
            error = None if proc.returncode == 0 else _format_git_error(content)
            return ToolResult(
                ok=proc.returncode == 0,
                content=content,
                error=error,
                metadata={"returncode": proc.returncode, "command": " ".join(command)},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ToolResult(ok=False, error=str(exc))

    return [
        ToolSpec(name="git_status", description="执行 git status --short。", permission_level=PermissionLevel.EXEC_READONLY, schema=EMPTY_SCHEMA, handler=lambda args: run_git(args, ["git", "status", "--short"])),
        ToolSpec(name="git_diff_stat", description="执行 git diff --stat。", permission_level=PermissionLevel.EXEC_READONLY, schema=EMPTY_SCHEMA, handler=lambda args: run_git(args, ["git", "diff", "--stat"])),
    ]


def _format_git_error(content: str) -> str:
    if "not a git repository" in content:
        return f"git command executed, but workspace is not a valid Git repository: {content}"
    return content
