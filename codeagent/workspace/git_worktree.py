from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from codeagent.workspace.workspace import WorkspaceContext


class GitWorktreeError(RuntimeError):
    """Git worktree 创建、恢复或清理失败。"""


class WorkspaceState(StrEnum):
    READY = "ready"
    SOURCE_DIRTY = "source_dirty"
    STALE = "stale"
    WORKTREE_DIRTY = "worktree_dirty"
    MISSING = "missing"
    COMPLETED = "completed"
    DISCARDED = "discarded"


@dataclass(frozen=True)
class WorkspaceStateReport:
    state: WorkspaceState
    source_head: str | None = None
    worktree_head: str | None = None
    source_dirty: bool = False
    worktree_dirty: bool = False
    registered: bool = False
    reason: str = ""

    @property
    def executable(self) -> bool:
        return self.state == WorkspaceState.READY


class GitWorktreeManager:
    def __init__(self, session_root: str | Path) -> None:
        self.session_root = Path(session_root).expanduser().resolve()
        self.worktrees_root = self.session_root / "worktrees"

    def create(self, source_root: str | Path, task_workspace_id: str) -> WorkspaceContext:
        source = Path(source_root).expanduser().resolve()
        repo_root = self._git(source, "rev-parse", "--show-toplevel", error="不是 Git 仓库")
        if Path(repo_root).resolve() != source:
            raise GitWorktreeError(f"source workspace 必须是 Git 仓库根目录: {repo_root}")
        dirty = self._git(source, "status", "--porcelain", "--untracked-files=all")
        if dirty:
            raise GitWorktreeError("原始 Git 工作区不干净（存在 tracked 修改或 untracked 文件），拒绝创建 worktree")
        base_commit = self._git(source, "rev-parse", "HEAD")
        active = (self.worktrees_root / task_workspace_id).resolve()
        if active.exists():
            return self.recover(source, active, task_workspace_id, base_commit)
        active.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._git(source, "worktree", "add", "--detach", str(active), base_commit, error="创建 detached worktree 失败")
        except GitWorktreeError as exc:
            try:
                if active.exists():
                    self._git(source, "worktree", "remove", str(active), error="回滚半初始化 worktree 失败")
            except GitWorktreeError as rollback_exc:
                raise GitWorktreeError(f"{exc}; partial worktree 保留在 {active}: {rollback_exc}") from exc
            raise
        return WorkspaceContext(source, active, "git_worktree", base_commit, task_workspace_id)

    def recover(
        self,
        source_root: str | Path,
        active_root: str | Path,
        task_workspace_id: str,
        base_commit: str | None = None,
    ) -> WorkspaceContext:
        source = Path(source_root).expanduser().resolve()
        active = Path(active_root).expanduser().resolve()
        self._ensure_managed(active)
        if not active.is_dir():
            raise GitWorktreeError(f"worktree 不存在，无法恢复: {active}")
        actual_commit = self._git(active, "rev-parse", "HEAD", error="已有目录不是有效 Git worktree")
        if base_commit and actual_commit != base_commit:
            raise GitWorktreeError("worktree HEAD 与 session 记录的 base commit 不一致")
        return WorkspaceContext(source, active, "git_worktree", base_commit or actual_commit, task_workspace_id)

    def inspect(self, context: WorkspaceContext) -> WorkspaceStateReport:
        if context.workspace_kind != "git_worktree" or not context.source_root.is_dir() or not context.active_root.is_dir():
            return WorkspaceStateReport(WorkspaceState.MISSING, reason="source 或 active worktree 不存在")
        try:
            repo_root = self._git(context.source_root, "rev-parse", "--show-toplevel")
            if Path(repo_root).resolve() != context.source_root:
                return WorkspaceStateReport(WorkspaceState.MISSING, reason="source 不再是记录的 Git 仓库")
            registered = context.active_root in self._registered_worktrees(context.source_root)
            if not registered:
                return WorkspaceStateReport(WorkspaceState.MISSING, reason="worktree 未被 Git 注册")
            source_head = self._git(context.source_root, "rev-parse", "HEAD")
            worktree_head = self._git(context.active_root, "rev-parse", "HEAD")
            source_dirty = bool(self._git(context.source_root, "status", "--porcelain", "--untracked-files=all"))
            worktree_dirty = bool(self._git(context.active_root, "status", "--porcelain", "--untracked-files=all"))
        except GitWorktreeError as exc:
            return WorkspaceStateReport(WorkspaceState.MISSING, reason=str(exc))
        common = dict(source_head=source_head, worktree_head=worktree_head, source_dirty=source_dirty, worktree_dirty=worktree_dirty, registered=True)
        if source_dirty:
            return WorkspaceStateReport(WorkspaceState.SOURCE_DIRTY, reason="source workspace 存在未提交变化", **common)
        if source_head != context.base_commit or worktree_head != context.base_commit:
            return WorkspaceStateReport(WorkspaceState.STALE, reason="HEAD 与 base_commit 不一致", **common)
        if worktree_dirty:
            return WorkspaceStateReport(WorkspaceState.WORKTREE_DIRTY, reason="task worktree 存在变化", **common)
        return WorkspaceStateReport(WorkspaceState.READY, reason="execution workspace ready", **common)

    @staticmethod
    def _registered_worktrees(source: Path) -> set[Path]:
        output = GitWorktreeManager._git(source, "worktree", "list", "--porcelain")
        return {Path(line[9:]).resolve() for line in output.splitlines() if line.startswith("worktree ")}

    def cleanup(self, context: WorkspaceContext) -> None:
        if context.workspace_kind != "git_worktree":
            return
        self._ensure_managed(context.active_root)
        # Git 拒绝移除 dirty worktree；这里不使用 --force，避免丢失未来阶段产生的内容。
        self._git(context.source_root, "worktree", "remove", str(context.active_root), error="worktree 非干净状态或清理失败")
        if context.active_root.exists():
            raise GitWorktreeError(f"Git 未能完整清理 worktree: {context.active_root}")

    def _ensure_managed(self, path: Path) -> None:
        try:
            path.relative_to(self.worktrees_root)
        except ValueError as exc:
            raise GitWorktreeError(f"拒绝操作 session root 之外的 worktree: {path}") from exc

    @staticmethod
    def _git(cwd: Path, *args: str, error: str | None = None) -> str:
        try:
            proc = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitWorktreeError(f"Git 调用失败: {exc}") from exc
        output = (proc.stdout or proc.stderr).strip()
        if proc.returncode:
            raise GitWorktreeError(f"{error or 'Git 操作失败'}: {output}")
        return output
