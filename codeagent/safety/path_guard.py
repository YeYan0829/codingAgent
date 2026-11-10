from __future__ import annotations

from pathlib import Path


class PathGuardError(ValueError):
    pass


class PathGuard:
    def __init__(self, workspace_root: Path | str, *, denied_paths: tuple[str, ...] = ()) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        from codeagent.safety.repository_access import normalize_paths
        self.denied_paths = normalize_paths(denied_paths)

    def resolve(self, path: str | Path = ".") -> Path:
        raw = Path(path)
        target = raw if raw.is_absolute() else self.workspace_root / raw
        resolved = target.resolve()
        if not self._is_relative_to(resolved, self.workspace_root):
            raise PathGuardError(f"path escapes workspace: {path}")
        relative = resolved.relative_to(self.workspace_root)
        if self.denied_paths and (relative.parts[:1] == (".git",) or any(relative == Path(p) or Path(p) in relative.parents for p in self.denied_paths)):
            raise PathGuardError("用户禁止访问此路径；本次操作未执行，请选择允许的文件或让用户修改仓库访问设置")
        return resolved

    @staticmethod
    def _is_relative_to(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False
