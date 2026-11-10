from __future__ import annotations

from pathlib import Path

from codeagent.safety.sensitive import is_sensitive_path


class PathGuardError(ValueError):
    pass


class PathGuard:
    def __init__(self, workspace_root: Path | str) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()

    def resolve(self, path: str | Path = ".", *, reject_sensitive: bool = True) -> Path:
        raw = Path(path)
        target = raw if raw.is_absolute() else self.workspace_root / raw
        resolved = target.resolve()
        if not self._is_relative_to(resolved, self.workspace_root):
            raise PathGuardError(f"path escapes workspace: {path}")
        relative = resolved.relative_to(self.workspace_root)
        if reject_sensitive and is_sensitive_path(relative):
            raise PathGuardError(f"sensitive file is blocked: {resolved.name}")
        return resolved

    @staticmethod
    def _is_relative_to(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False
