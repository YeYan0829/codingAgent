from __future__ import annotations

import shutil
import os
import subprocess
from typing import Any, Callable

from codeagent.runtime.candidate import ApplyStatus, CandidateError, CandidateService
from codeagent.session.store import SessionStore
from codeagent.workspace.git_worktree import GitWorktreeError
from codeagent.workspace.workspace import SessionWorkspaceState, WorkspaceContext
from codeagent.runtime.git_snapshot import GitSnapshotError, head_tree, snapshot_tree


class CurrentChangesError(RuntimeError):
    pass


class CurrentChangesService:
    """面向产品的当前修改采纳/丢弃；Candidate 仅作为内部冻结机制。"""

    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self.context = store.workspace_context()
        self.candidate = CandidateService(self.context, store)

    def freeze_for_review(self) -> tuple[dict[str, Any], bytes]:
        manifest = self.candidate.freeze()
        return self.candidate.load(manifest["candidate_id"])

    def preview(self) -> bytes:
        return self.candidate.preview_patch()

    def accept(self, approve: Callable[[dict[str, Any], str], bool]) -> dict[str, Any]:
        self.store.begin_resolution(SessionWorkspaceState.ACCEPTING)
        source_applied = False
        try:
            manifest = self.candidate.freeze()
            receipt = self.candidate.apply(manifest["candidate_id"], approve)
            if receipt["status"] != ApplyStatus.APPLIED.value:
                self._clear_frozen()
                self.store.restore_changes_active(receipt["reason"])
                return receipt
            source_applied = True
            checkpoint = self._checkpoint(manifest["merged_tree"], manifest["base_commit"])
            updated = WorkspaceContext(
                self.context.source_root, self.context.active_root, "git_worktree",
                checkpoint, self.context.workspace_revision,
            )
            self._clear_frozen()
            self.store.finish_accept(
                updated,
                candidate_revision=int(manifest["candidate_revision"]),
                patch_sha256=receipt["patch_sha256"],
                changed_files=list(receipt["changed_files"]),
            )
            return receipt
        except (CandidateError, GitWorktreeError, OSError, subprocess.SubprocessError) as exc:
            self._clear_frozen()
            if source_applied:
                self.store.mark_recovery_required(f"source 已采纳但内部 checkpoint 失败: {exc}")
            elif self.context.active_root.exists():
                self.store.restore_changes_active(str(exc))
            else:
                self.store.mark_recovery_required(f"采纳流程无法确认 workspace: {exc}")
            raise CurrentChangesError(str(exc)) from exc

    def discard(self) -> dict[str, Any]:
        self.store.begin_resolution(SessionWorkspaceState.DISCARDING)
        try:
            subprocess.run(["git", "reset", "--hard", self.context.base_commit], cwd=self.context.active_root, check=True, capture_output=True, timeout=20)
            subprocess.run(["git", "clean", "-fdx"], cwd=self.context.active_root, check=True, capture_output=True, timeout=20)
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=self.context.active_root,
                text=True, capture_output=True, check=True, timeout=20,
            ).stdout.strip()
            status = subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=self.context.active_root,
                text=True, capture_output=True, check=True, timeout=20,
            ).stdout
            restored_tree = snapshot_tree(self.context.active_root, self.context.base_commit)
            expected_tree = head_tree(self.context.active_root, self.context.base_commit)
            if head != self.context.base_commit or status or restored_tree != expected_tree:
                raise GitSnapshotError("discard 后无法证明 workspace 已完整恢复 accepted baseline")
            self._clear_current()
            latest = int(self.store.read_meta().get("candidate_revision", 0))
            self.store.finish_discard(candidate_revision=latest)
            return {"status": "discarded", "workspace_revision": self.context.workspace_revision, "source_changed": False}
        except (GitWorktreeError, GitSnapshotError, OSError, subprocess.SubprocessError) as exc:
            self.store.mark_recovery_required(f"丢弃流程无法确认 workspace 完整恢复: {exc}")
            raise CurrentChangesError(str(exc)) from exc

    def _clear_frozen(self) -> None:
        root = self.store.session_dir / "current"
        for name in ("frozen.json", "frozen.patch"):
            (root / name).unlink(missing_ok=True)
        if root.is_dir() and not any(root.iterdir()):
            root.rmdir()

    def _clear_current(self) -> None:
        shutil.rmtree(self.store.session_dir / "current", ignore_errors=True)

    def _checkpoint(self, tree: str, parent: str) -> str:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "CodeAgent Runtime", "GIT_AUTHOR_EMAIL": "runtime@codeagent.invalid",
            "GIT_COMMITTER_NAME": "CodeAgent Runtime", "GIT_COMMITTER_EMAIL": "runtime@codeagent.invalid",
        }
        commit = subprocess.run(
            ["git", "commit-tree", tree, "-p", parent], cwd=self.context.active_root,
            input=b"CodeAgent accepted baseline\n", env=env, capture_output=True, check=True, timeout=20,
        ).stdout.decode().strip()
        subprocess.run(
            ["git", "reset", "--hard", commit], cwd=self.context.active_root,
            capture_output=True, check=True, timeout=20,
        )
        return commit
