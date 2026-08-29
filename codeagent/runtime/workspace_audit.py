from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from codeagent.runtime.git_snapshot import GitSnapshotError, snapshot_tree


class WorkspaceAuditError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkspaceSnapshot:
    status: str
    fingerprints: dict[str, str]
    subject_tree: str
    change_hints: dict[str, tuple[str, str | None]]


@dataclass(frozen=True)
class WorkspaceAudit:
    before_subject_tree: str
    after_subject_tree: str
    changes: tuple[dict[str, str], ...]
    complete: bool = True


def take_workspace_snapshot(root: Path, base_commit: str) -> WorkspaceSnapshot:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"], cwd=root,
            text=True, encoding="utf-8", errors="replace", capture_output=True,
            shell=False, check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkspaceAuditError(str(exc)) from exc
    if proc.returncode:
        raise WorkspaceAuditError(proc.stderr.strip() or "git status failed")
    try:
        tree = snapshot_tree(root, base_commit)
    except GitSnapshotError as exc:
        raise WorkspaceAuditError(str(exc)) from exc
    return WorkspaceSnapshot(proc.stdout, _fingerprints(root, proc.stdout), tree, _change_hints(proc.stdout))


def compare_workspace(before: WorkspaceSnapshot, after: WorkspaceSnapshot) -> WorkspaceAudit:
    changes = []
    for path in sorted(set(before.fingerprints) | set(after.fingerprints)):
        old = before.fingerprints.get(path)
        new = after.fingerprints.get(path)
        if old == new:
            continue
        hint, source = after.change_hints.get(path, ("", None))
        if hint == "rename":
            change = {"path": path, "kind": "rename"}
            if source:
                change["source"] = source
            changes.append(change)
            continue
        kind = "delete" if new == "<missing>" or new is None else "create" if old is None else "update"
        changes.append({"path": path, "kind": kind})
    return WorkspaceAudit(before.subject_tree, after.subject_tree, tuple(changes))


def _fingerprints(root: Path, status: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in status.splitlines():
        if len(line) <= 3:
            continue
        relative = line[3:].replace("\\", "/")
        if " -> " in relative:
            relative = relative.split(" -> ", 1)[1]
        target = root / relative
        try:
            result[relative] = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else "<missing>"
        except OSError as exc:
            raise WorkspaceAuditError(f"无法读取 audit path {relative}: {exc}") from exc
    return result


def _change_hints(status: str) -> dict[str, tuple[str, str | None]]:
    hints: dict[str, tuple[str, str | None]] = {}
    for line in status.splitlines():
        if len(line) <= 3:
            continue
        value = line[3:].replace("\\", "/")
        if "R" in line[:2] and " -> " in value:
            source, destination = value.split(" -> ", 1)
            hints[destination] = ("rename", source)
        elif "D" in line[:2]:
            hints[value] = ("delete", None)
        elif "?" in line[:2] or "A" in line[:2]:
            hints[value] = ("create", None)
        else:
            hints[value] = ("update", None)
    return hints
