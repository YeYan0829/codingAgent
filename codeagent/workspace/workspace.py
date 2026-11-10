from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
from enum import StrEnum

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


class SessionWorkspaceState(StrEnum):
    CURRENT_PROJECT = "current_project"
    SOURCE_ONLY = "source_only"
    PREPARING_WORKSPACE = "preparing_workspace"
    CHANGES_ACTIVE = "changes_active"
    WORKSPACE_TAINTED = "workspace_tainted"
    ACCEPTING = "accepting"
    DISCARDING = "discarding"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True)
class WorkspaceContext:
    """实际文件路径与访问限制；不携带代码版本、测试或交付身份。"""

    source_root: Path
    active_root: Path
    denied_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_root", Path(self.source_root).expanduser().resolve())
        object.__setattr__(self, "active_root", Path(self.active_root).expanduser().resolve())

    @classmethod
    def source(cls, root: str | Path) -> "WorkspaceContext":
        resolved = Path(root).expanduser().resolve()
        return cls(source_root=resolved, active_root=resolved)

    def path_guard(self) -> PathGuard:
        return PathGuard(self.active_root, denied_paths=self.denied_paths)

    def to_metadata(self) -> dict:
        return {"source_workspace": str(self.source_root), "active_workspace": str(self.active_root),
                "workspace_kind": "source", "denied_paths": list(self.denied_paths)}

    @classmethod
    def from_metadata(cls, meta: dict) -> "WorkspaceContext":
        source = meta.get("source_workspace") or meta.get("workspace")
        if not source:
            raise ValueError("session metadata missing source workspace")
        # 旧active目录和版本只留在原metadata里供历史读取，不参与当前文件操作。
        return cls(source_root=source, active_root=source,
                   denied_paths=tuple(meta.get("denied_paths", ())))
