from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass

from codeagent.safety.path_guard import PathGuard


class Workspace:
    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).expanduser().resolve()
        self.guard = PathGuard(self.root)

    @property
    def codeagent_dir(self) -> Path:
        return self.root / ".codeagent"

    @property
    def context(self) -> "WorkspaceContext":
        return WorkspaceContext.source(self.root)


@dataclass(frozen=True)
class WorkspaceContext:
    """一次任务实际使用的工作区身份与路径。"""

    source_root: Path
    active_root: Path
    workspace_kind: str = "source"
    base_commit: str | None = None
    task_workspace_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_root", Path(self.source_root).expanduser().resolve())
        object.__setattr__(self, "active_root", Path(self.active_root).expanduser().resolve())
        if self.workspace_kind not in {"source", "git_worktree"}:
            raise ValueError(f"unsupported workspace kind: {self.workspace_kind}")

    @classmethod
    def source(cls, root: str | Path) -> "WorkspaceContext":
        resolved = Path(root).expanduser().resolve()
        return cls(source_root=resolved, active_root=resolved)

    def to_metadata(self) -> dict[str, str | None]:
        return {
            "source_workspace": str(self.source_root),
            "active_workspace": str(self.active_root),
            "workspace_kind": self.workspace_kind,
            "base_commit": self.base_commit,
            "task_workspace_id": self.task_workspace_id,
        }

    @classmethod
    def from_metadata(cls, meta: dict) -> "WorkspaceContext":
        source = meta.get("source_workspace") or meta.get("workspace")
        if not source:
            raise ValueError("session metadata missing source workspace")
        return cls(
            source_root=source,
            active_root=meta.get("active_workspace") or source,
            workspace_kind=meta.get("workspace_kind", "source"),
            base_commit=meta.get("base_commit"),
            task_workspace_id=meta.get("task_workspace_id"),
        )
