from __future__ import annotations

from pathlib import Path

from codeagent.safety.path_guard import PathGuard


class Workspace:
    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).expanduser().resolve()
        self.guard = PathGuard(self.root)

    @property
    def codeagent_dir(self) -> Path:
        return self.root / ".codeagent"
