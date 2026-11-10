from __future__ import annotations

import hashlib
import json
import subprocess
import os
import tempfile
import re
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

from codeagent.safety.path_guard import PathGuard, PathGuardError
from codeagent.session.store import SessionStore
from codeagent.workspace.workspace import WorkspaceContext
from codeagent.runtime.validation import current_validation_evidence
from codeagent.runtime.git_snapshot import GitSnapshotError, snapshot_tree


class CandidateError(RuntimeError):
    """Candidate 冻结、检查或应用失败。"""


class ApplyStatus(StrEnum):
    REJECTED = "rejected"
    SOURCE_DIRTY = "source_dirty"
    SOURCE_CHANGED = "source_changed"
    PATCH_CHECK_FAILED = "patch_check_failed"
    APPLIED = "applied"
    APPLY_FAILED = "apply_failed"


class CandidateService:
    MAX_PATCH_BYTES = 512_000

    def __init__(self, context: WorkspaceContext, session_store: SessionStore) -> None:
        self.context = context
        self.session_store = session_store
        self.root = session_store.session_dir / "current"

    def freeze(self) -> dict[str, Any]:
        self._validate_execution_identity()
        candidate_paths = sorted(self._status_paths(self.context.active_root))
        if not candidate_paths:
            raise CandidateError("没有可冻结的 Candidate changes")
        tests = current_validation_evidence(self.session_store, self.context)
        if not tests:
            raise CandidateError("当前修改没有仍有效的 validation evidence")

        guard = PathGuard(self.context.active_root)
        file_changes = []
        for relative in candidate_paths:
            try:
                target = guard.resolve(relative)
            except PathGuardError as exc:
                raise CandidateError(str(exc)) from exc
            if not target.exists():
                file_changes.append({"path": relative, "after_exists": False, "after_sha256": None})
                continue
            if not target.is_file() or target.is_symlink():
                raise CandidateError(f"候选路径不是普通非 symlink 文件: {relative}")
            content = target.read_bytes()
            if b"\0" in content[:8192]:
                raise CandidateError(f"候选包含二进制文件: {relative}")
            file_changes.append({"path": relative, "after_exists": True, "after_sha256": hashlib.sha256(content).hexdigest()})

        self._validate_changed_paths(self.context.source_root)
        ours_tree = self._snapshot_tree(self.context.active_root, self.context.base_commit)
        if ours_tree == self.context.base_commit or ours_tree == _git(self.context.active_root, "rev-parse", f"{self.context.base_commit}^{{tree}}"):
            raise CandidateError("当前没有尚未采纳的修改")
        source_tree = self._snapshot_tree(self.context.source_root, "HEAD")
        ours_commit = self._temporary_commit(ours_tree, self.context.base_commit, "CodeAgent pending changes")
        theirs_commit = self._temporary_commit(source_tree, self.context.base_commit, "CodeAgent source snapshot")
        merged_tree = self._merge_tree(self.context.base_commit, ours_commit, theirs_commit)
        patch_bytes = self._tree_patch(source_tree, merged_tree)
        if not patch_bytes or len(patch_bytes) > self.MAX_PATCH_BYTES:
            raise CandidateError("当前修改 patch 为空或超过大小限制")
        try:
            patch_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CandidateError("最终合并 patch 不是 UTF-8 文本") from exc
        patch_hash = hashlib.sha256(patch_bytes).hexdigest()
        candidate_id = uuid.uuid4().hex[:16]
        self.root.mkdir(parents=True, exist_ok=True)
        patch_path = self.root / "frozen.patch"
        patch_path.write_bytes(patch_bytes)
        manifest = {
            "candidate_id": candidate_id,
            "session_id": self.session_store.session_id,
            "base_commit": self.context.base_commit,
            "workspace_revision": self.context.workspace_revision,
            "source_snapshot_tree": source_tree,
            "merged_tree": merged_tree,
            "patch_sha256": patch_hash,
            "changed_files": candidate_paths,
            "file_changes": file_changes,
            "candidate_revision": int(self.session_store.read_meta().get("candidate_revision", 0)),
            "workspace_side_effects": [],
            "test_receipts": tests,
            "created_at": _now(),
            "status": "frozen",
        }
        manifest_path = self.root / "frozen.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        self.session_store.append_event("changes_frozen", {
            "workspace_revision": self.context.workspace_revision,
            "candidate_revision": manifest["candidate_revision"],
            "patch_sha256": patch_hash,
            "changed_files": candidate_paths,
        })
        return manifest

    def preview_patch(self) -> bytes:
        """按需生成 accepted baseline 到当前 worktree 的完整 patch，包括未跟踪文件。"""
        ours_tree = self._snapshot_tree(self.context.active_root, self.context.base_commit)
        base_tree = _git(self.context.active_root, "rev-parse", f"{self.context.base_commit}^{{tree}}")
        patch = self._tree_patch(base_tree, ours_tree)
        if len(patch) > self.MAX_PATCH_BYTES:
            raise CandidateError("当前修改 patch 超过大小限制")
        return patch

    def load(self, candidate_id: str | None = None) -> tuple[dict[str, Any], bytes]:
        try:
            manifest = json.loads((self.root / "frozen.json").read_text(encoding="utf-8"))
            patch_bytes = (self.root / "frozen.patch").read_bytes()
        except (OSError, json.JSONDecodeError) as exc:
            raise CandidateError(f"当前修改的冻结数据不完整: {exc}") from exc
        selected = candidate_id or manifest.get("candidate_id")
        if manifest.get("candidate_id") != selected or manifest.get("session_id") != self.session_store.session_id:
            raise CandidateError("当前修改 identity 不匹配")
        if hashlib.sha256(patch_bytes).hexdigest() != manifest.get("patch_sha256"):
            raise CandidateError("当前修改的冻结 patch hash 校验失败，可能已被篡改")
        return manifest, patch_bytes

    def reject(self, candidate_id: str | None = None) -> dict[str, Any]:
        manifest, _ = self.load(candidate_id)
        receipt = {"status": ApplyStatus.REJECTED.value, "reason": "用户丢弃当前修改；source 未修改"}
        return receipt

    def apply(
        self,
        candidate_id: str | None,
        approve: Callable[[dict[str, Any], str], bool],
    ) -> dict[str, Any]:
        manifest, patch_bytes = self.load(candidate_id)
        before_head, before_dirty = self._source_identity()
        if self._snapshot_tree(self.context.source_root, "HEAD") != manifest.get("source_snapshot_tree"):
            return self._record_apply(manifest, ApplyStatus.SOURCE_CHANGED, "source 在冻结后发生变化", before_head, before_dirty)
        preflight = self._git_apply(patch_bytes, check=True)
        if preflight.returncode:
            return self._record_apply(
                manifest, ApplyStatus.PATCH_CHECK_FAILED, preflight.stderr.strip() or "git apply --check 失败", before_head, before_dirty
            )

        patch_text = patch_bytes.decode("utf-8")
        if not approve(manifest, patch_text):
            return self._record_apply(manifest, ApplyStatus.REJECTED, "用户拒绝 apply；source 未修改", before_head, before_dirty)

        approved_head, approved_dirty = self._source_identity()
        if self._snapshot_tree(self.context.source_root, "HEAD") != manifest.get("source_snapshot_tree"):
            return self._record_apply(manifest, ApplyStatus.SOURCE_CHANGED, "批准后 source 发生变化", approved_head, approved_dirty)
        second_preflight = self._git_apply(patch_bytes, check=True)
        if second_preflight.returncode:
            return self._record_apply(
                manifest,
                ApplyStatus.PATCH_CHECK_FAILED,
                "批准后 patch preflight 失败: " + (second_preflight.stderr.strip() or "unknown error"),
                approved_head,
                approved_dirty,
            )
        applied = self._git_apply(patch_bytes, check=False)
        status = ApplyStatus.APPLIED if applied.returncode == 0 else ApplyStatus.APPLY_FAILED
        if status == ApplyStatus.APPLIED and self._snapshot_tree(self.context.source_root, "HEAD") != manifest.get("merged_tree"):
            status = ApplyStatus.APPLY_FAILED
            reason = "patch apply 后 source identity 与冻结合并结果不一致"
        else:
            reason = "固定当前修改已应用到 source" if status == ApplyStatus.APPLIED else applied.stderr.strip() or "git apply 失败"
        return self._record_apply(manifest, status, reason, approved_head, approved_dirty)

    def _validate_execution_identity(self) -> None:
        if self.context.workspace_kind != "git_worktree" or not self.context.base_commit:
            raise CandidateError("只能从 execution git worktree 冻结 candidate")
        active_head = _git(self.context.active_root, "rev-parse", "HEAD")
        if active_head != self.context.base_commit:
            raise CandidateError("worktree HEAD 与 base_commit 不一致")

    def _validate_changed_paths(self, root: Path) -> None:
        guard = PathGuard(root)
        for relative in self._status_paths(root):
            try:
                guard.resolve(relative)
            except PathGuardError as exc:
                raise CandidateError(f"source/workspace 包含不允许进入合并的路径: {exc}") from exc

    def _snapshot_tree(self, root: Path, base: str) -> str:
        try:
            return snapshot_tree(root, base)
        except GitSnapshotError as exc:
            raise CandidateError(str(exc)) from exc

    def _temporary_commit(self, tree: str, parent: str, message: str) -> str:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "CodeAgent Runtime", "GIT_AUTHOR_EMAIL": "runtime@codeagent.invalid",
            "GIT_COMMITTER_NAME": "CodeAgent Runtime", "GIT_COMMITTER_EMAIL": "runtime@codeagent.invalid",
        }
        proc = self._run_git(self.context.active_root, ["commit-tree", tree, "-p", parent], env=env, input_data=(message + "\n").encode())
        return proc.stdout.decode().strip()

    def _merge_tree(self, baseline: str, ours: str, theirs: str) -> str:
        proc = subprocess.run(
            ["git", "merge-tree", "--write-tree", f"--merge-base={baseline}", ours, theirs],
            cwd=self.context.active_root, capture_output=True, check=False, timeout=20,
        )
        if proc.returncode:
            detail = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")
            raise CandidateError("当前修改与 source 存在 Git 三方合并冲突；source 未改变，Agent worktree 已保留\n" + detail[-2000:])
        tree = proc.stdout.decode().splitlines()[0].strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", tree):
            raise CandidateError("Git merge-tree 未返回有效 merged tree")
        return tree

    def _tree_patch(self, before_tree: str, after_tree: str) -> bytes:
        proc = self._run_git(
            self.context.active_root,
            ["-c", "core.safecrlf=false", "diff", "--binary", "--full-index", "--no-ext-diff", before_tree, after_tree, "--"],
        )
        return proc.stdout

    @staticmethod
    def _run_git(cwd: Path, args: list[str], *, env: dict[str, str] | None = None, input_data: bytes | None = None) -> subprocess.CompletedProcess:
        proc = subprocess.run(["git", *args], cwd=cwd, env=env, input=input_data, capture_output=True, check=False, timeout=20)
        if proc.returncode:
            raise CandidateError(proc.stderr.decode("utf-8", "replace").strip() or "Git plumbing 调用失败")
        return proc

    @staticmethod
    def _status_paths(root: Path) -> set[str]:
        proc = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=root, capture_output=True, check=False, timeout=20)
        if proc.returncode:
            raise CandidateError("读取 Git status 失败")
        from codeagent.tools.git_read import _parse_porcelain_v1_z
        return {
            str(path) for entry in _parse_porcelain_v1_z(proc.stdout)
            for path in (entry.get("path"), entry.get("original_path")) if path
        }

    def _successful_test_evidence(self, after_index: int) -> list[dict[str, Any]]:
        evidence = []
        for index, event in enumerate(self.session_store.read_events()):
            if index <= after_index or event.type != "command_receipt":
                continue
            payload = event.payload
            spec = payload.get("command_spec") or {}
            result = payload.get("result") or {}
            if spec.get("command_kind") == "pytest" and payload.get("status") == "completed" and result.get("exit_code") == 0:
                command_id = spec.get("command_id")
                evidence.append(
                    {
                        "command_id": command_id,
                        "status": payload.get("status"),
                        "exit_code": result.get("exit_code"),
                    }
                )
        return evidence

    def _build_patch(self, paths: list[str]) -> str:
        tracked: list[str] = []
        untracked: list[str] = []
        for relative in paths:
            probe = subprocess.run(
                ["git", "cat-file", "-e", f"{self.context.base_commit}:{relative}"],
                cwd=self.context.active_root, capture_output=True, shell=False, check=False, timeout=10,
            )
            (tracked if probe.returncode == 0 else untracked).append(relative)
        chunks: list[str] = []
        if tracked:
            proc = subprocess.run(
                ["git", "diff", "--binary", "--no-ext-diff", self.context.base_commit, "--", *tracked],
                cwd=self.context.active_root, text=True, encoding="utf-8", errors="strict",
                capture_output=True, shell=False, check=False, timeout=20,
            )
            if proc.returncode:
                raise CandidateError(proc.stderr.strip() or "生成 tracked patch 失败")
            chunks.append(proc.stdout)
        for relative in untracked:
            proc = subprocess.run(
                ["git", "diff", "--no-index", "--binary", "--", "/dev/null", relative],
                cwd=self.context.active_root, text=True, encoding="utf-8", errors="strict",
                capture_output=True, shell=False, check=False, timeout=20,
            )
            if proc.returncode not in {0, 1}:
                raise CandidateError(proc.stderr.strip() or f"生成新增文件 patch 失败: {relative}")
            lines = proc.stdout.splitlines(keepends=True)
            for index, line in enumerate(lines):
                if line.startswith("diff --git "):
                    lines[index] = f"diff --git a/{relative} b/{relative}\n"
                elif line.startswith("+++ "):
                    lines[index] = f"+++ b/{relative}\n"
            chunks.append("".join(lines))
        return "".join(chunks)

    def _git_status_paths(self) -> set[str]:
        try:
            proc = subprocess.run(
                ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                cwd=self.context.active_root, capture_output=True, shell=False, check=False, timeout=20,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CandidateError(f"Git status 调用失败: {exc}") from exc
        if proc.returncode:
            raise CandidateError(proc.stderr.decode("utf-8", "replace").strip() or "Git status 调用失败")
        fields = proc.stdout.split(b"\0")
        paths: set[str] = set()
        index = 0
        while index < len(fields) - 1:
            record = fields[index]
            index += 1
            if len(record) < 4:
                continue
            status = record[:2]
            paths.add(record[3:].decode("utf-8", "strict").replace("\\", "/"))
            if b"R" in status or b"C" in status:
                if index < len(fields) - 1:
                    paths.add(fields[index].decode("utf-8", "strict").replace("\\", "/"))
                    index += 1
        return paths

    def _source_identity(self) -> tuple[str | None, bool | None]:
        try:
            return _git(self.context.source_root, "rev-parse", "HEAD"), bool(
                _git(self.context.source_root, "status", "--porcelain", "--untracked-files=all")
            )
        except CandidateError:
            return None, None

    @staticmethod
    def _identity_failure(manifest: dict[str, Any], head: str | None, dirty: bool | None) -> tuple[ApplyStatus, str] | None:
        if head != manifest.get("base_commit"):
            return ApplyStatus.SOURCE_CHANGED, "source HEAD 与 candidate.base_commit 不一致"
        if dirty is not False:
            return ApplyStatus.SOURCE_DIRTY, "source 不完全 clean"
        return None

    def _git_apply(self, patch: bytes, *, check: bool) -> subprocess.CompletedProcess:
        args = ["git", "apply", "--whitespace=nowarn"]
        if check:
            args.append("--check")
        return subprocess.run(
            args, cwd=self.context.source_root, input=patch, capture_output=True,
            shell=False, check=False, timeout=20,
        )

    def _record_apply(
        self,
        manifest: dict[str, Any],
        status: ApplyStatus,
        reason: str,
        source_head: str | None,
        source_dirty: bool | None,
    ) -> dict[str, Any]:
        final_head, final_dirty = self._source_identity()
        try:
            final_status = _git(
                self.context.source_root,
                "status",
                "--porcelain",
                "--untracked-files=all",
                strip_output=False,
            )
        except CandidateError:
            final_status = None
        receipt = {
            "apply_id": uuid.uuid4().hex,
            "candidate_id": manifest["candidate_id"],
            "session_id": manifest["session_id"],
            "patch_sha256": manifest["patch_sha256"],
            "changed_files": manifest["changed_files"],
            "test_receipts": manifest["test_receipts"],
            "expected_base_commit": manifest["base_commit"],
            "observed_source_head": source_head,
            "observed_source_dirty": source_dirty,
            "final_source_head": final_head,
            "final_source_dirty": final_dirty,
            "final_git_status": final_status,
            "applied_patch_sha256": manifest["patch_sha256"] if status == ApplyStatus.APPLIED else None,
            "status": status.value,
            "reason": reason,
            "created_at": _now(),
        }
        self.session_store.append_event("changes_apply_result", receipt)
        return receipt


def _git(cwd: Path, *args: str, strip_output: bool = True) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, text=True, encoding="utf-8", errors="strict",
            capture_output=True, shell=False, check=False, timeout=20,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise CandidateError(f"Git 调用失败: {exc}") from exc
    if proc.returncode:
        raise CandidateError(proc.stderr.strip() or "Git 调用失败")
    if strip_output:
        return proc.stdout.strip()
    # Porcelain status 的前两列包含有意义的空格，只移除末尾换行。
    return proc.stdout.rstrip("\r\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
