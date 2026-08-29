from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from codeagent.safety.path_guard import PathGuard, PathGuardError
from codeagent.tools.git_read import _parse_porcelain_v1_z


class GitSnapshotError(RuntimeError):
    pass


def snapshot_tree(root: Path, base: str) -> str:
    """把 tracked 与允许的 untracked 内容写入临时 index，返回稳定 Git tree。"""
    _validate_changed_paths(root)
    with tempfile.TemporaryDirectory(prefix="codeagent-index-") as temp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(temp) / "index")}
        _git(root, ["read-tree", base], env)
        _git(root, ["add", "-A", "--", "."], env)
        return _git(root, ["write-tree"], env).stdout.decode().strip()


def head_tree(root: Path, commit: str) -> str:
    return _git(root, ["rev-parse", f"{commit}^{{tree}}"]).stdout.decode().strip()


def _validate_changed_paths(root: Path) -> None:
    proc = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=root, capture_output=True, check=False, timeout=20,
    )
    if proc.returncode:
        raise GitSnapshotError(proc.stderr.decode("utf-8", "replace").strip() or "读取 Git status 失败")
    guard = PathGuard(root)
    for entry in _parse_porcelain_v1_z(proc.stdout):
        for value in (entry.get("path"), entry.get("original_path")):
            if not value:
                continue
            try:
                guard.resolve(str(value))
            except PathGuardError as exc:
                raise GitSnapshotError(f"workspace 包含不允许进入快照的路径: {exc}") from exc


def _git(root: Path, args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=root, env=env, capture_output=True, check=False, timeout=20)
    if proc.returncode:
        raise GitSnapshotError(proc.stderr.decode("utf-8", "replace").strip() or "Git snapshot 失败")
    return proc
