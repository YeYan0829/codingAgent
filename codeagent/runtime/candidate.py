from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

from codeagent.safety.path_guard import PathGuard, PathGuardError
from codeagent.session.store import SessionStore
from codeagent.workspace.workspace import WorkspaceContext


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
        self.root = session_store.session_dir / "artifacts" / "candidates"

    def freeze(self) -> dict[str, Any]:
        self._validate_execution_identity()
        edit_paths, latest_hashes, last_edit_index = self._edit_state()
        if not edit_paths:
            raise CandidateError("没有可冻结的 Agent edit")
        tests = self._successful_test_evidence(after_index=last_edit_index)
        if not tests:
            raise CandidateError("最近一次编辑后没有成功的 pytest command receipt")

        guard = PathGuard(self.context.active_root)
        for relative in edit_paths:
            try:
                target = guard.resolve(relative)
            except PathGuardError as exc:
                raise CandidateError(str(exc)) from exc
            if not target.is_file() or target.is_symlink():
                raise CandidateError(f"候选路径不是普通非 symlink 文件: {relative}")
            content = target.read_bytes()
            if b"\0" in content[:8192]:
                raise CandidateError(f"候选包含二进制文件: {relative}")
            if hashlib.sha256(content).hexdigest() != latest_hashes[relative]:
                raise CandidateError(f"文件在最后一条 edit journal 后发生未记录变化: {relative}")

        patch = self._build_patch(edit_paths)
        patch_bytes = patch.encode("utf-8")
        if not patch_bytes or len(patch_bytes) > self.MAX_PATCH_BYTES:
            raise CandidateError("candidate patch 为空或超过大小限制")
        patch_hash = hashlib.sha256(patch_bytes).hexdigest()
        status_paths = self._git_status_paths()
        candidate_id = uuid.uuid4().hex[:16]
        candidate_dir = self.root / candidate_id
        candidate_dir.mkdir(parents=True, exist_ok=False)
        patch_path = candidate_dir / "candidate.patch"
        patch_path.write_bytes(patch_bytes)
        manifest = {
            "candidate_id": candidate_id,
            "session_id": self.session_store.session_id,
            "source_root": str(self.context.source_root),
            "active_root": str(self.context.active_root),
            "base_commit": self.context.base_commit,
            "patch_artifact": str(patch_path),
            "patch_sha256": patch_hash,
            "changed_files": sorted(edit_paths),
            "workspace_side_effects": sorted(status_paths - set(edit_paths)),
            "test_receipts": tests,
            "created_at": _now(),
            "status": "frozen",
        }
        manifest_path = candidate_dir / "candidate.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        (candidate_dir / "status.json").write_text(
            json.dumps({"candidate_id": candidate_id, "status": "frozen", "updated_at": _now()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.session_store.append_event("candidate_frozen", manifest)
        meta = self.session_store.read_meta()
        meta["current_candidate_id"] = candidate_id
        self.session_store.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest

    def load(self, candidate_id: str | None = None) -> tuple[dict[str, Any], bytes]:
        selected = candidate_id or self.session_store.read_meta().get("current_candidate_id")
        if not selected or Path(selected).name != selected:
            raise CandidateError("candidate id 无效或 session 尚无 candidate")
        candidate_dir = self.root / selected
        try:
            manifest = json.loads((candidate_dir / "candidate.json").read_text(encoding="utf-8"))
            patch_bytes = (candidate_dir / "candidate.patch").read_bytes()
        except (OSError, json.JSONDecodeError) as exc:
            raise CandidateError(f"candidate artifact 不完整: {exc}") from exc
        if manifest.get("candidate_id") != selected or manifest.get("session_id") != self.session_store.session_id:
            raise CandidateError("candidate identity 不匹配")
        if hashlib.sha256(patch_bytes).hexdigest() != manifest.get("patch_sha256"):
            raise CandidateError("candidate patch hash 校验失败，可能已被篡改")
        return manifest, patch_bytes

    def reject(self, candidate_id: str | None = None) -> dict[str, Any]:
        manifest, _ = self.load(candidate_id)
        receipt = self._write_status(manifest, ApplyStatus.REJECTED, "用户拒绝 candidate；source 未修改")
        self.session_store.append_event("candidate_rejected", receipt)
        return receipt

    def apply(
        self,
        candidate_id: str | None,
        approve: Callable[[dict[str, Any], str], bool],
    ) -> dict[str, Any]:
        manifest, patch_bytes = self.load(candidate_id)
        before_head, before_dirty = self._source_identity()
        failure = self._identity_failure(manifest, before_head, before_dirty)
        if failure:
            return self._record_apply(manifest, failure[0], failure[1], before_head, before_dirty)
        preflight = self._git_apply(patch_bytes, check=True)
        if preflight.returncode:
            return self._record_apply(
                manifest, ApplyStatus.PATCH_CHECK_FAILED, preflight.stderr.strip() or "git apply --check 失败", before_head, before_dirty
            )

        patch_text = patch_bytes.decode("utf-8")
        if not approve(manifest, patch_text):
            return self._record_apply(manifest, ApplyStatus.REJECTED, "用户拒绝 apply；source 未修改", before_head, before_dirty)

        approved_head, approved_dirty = self._source_identity()
        failure = self._identity_failure(manifest, approved_head, approved_dirty)
        if failure:
            return self._record_apply(manifest, failure[0], f"批准后复检失败: {failure[1]}", approved_head, approved_dirty)
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
        reason = "固定 candidate patch 已应用到 source" if applied.returncode == 0 else applied.stderr.strip() or "git apply 失败"
        return self._record_apply(manifest, status, reason, approved_head, approved_dirty)

    def _validate_execution_identity(self) -> None:
        if self.context.workspace_kind != "git_worktree" or not self.context.base_commit:
            raise CandidateError("只能从 execution git worktree 冻结 candidate")
        active_head = _git(self.context.active_root, "rev-parse", "HEAD")
        if active_head != self.context.base_commit:
            raise CandidateError("worktree HEAD 与 base_commit 不一致")

    def _edit_state(self) -> tuple[list[str], dict[str, str], int]:
        paths: list[str] = []
        hashes: dict[str, str] = {}
        last_index = -1
        for index, event in enumerate(self.session_store.read_events()):
            if event.type != "edit_receipt":
                continue
            relative = str(event.payload.get("path", ""))
            if relative and relative not in paths:
                paths.append(relative)
            hashes[relative] = str(event.payload.get("after_sha256", ""))
            last_index = index
        return paths, hashes, last_index

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
                receipt_path = self.session_store.session_dir / "artifacts" / "commands" / str(command_id) / "result.json"
                try:
                    artifact = json.loads(receipt_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if (
                    (artifact.get("command_spec") or {}).get("command_id") != command_id
                    or artifact.get("status") != "completed"
                    or (artifact.get("result") or {}).get("exit_code") != 0
                ):
                    continue
                evidence.append(
                    {
                        "command_id": command_id,
                        "status": payload.get("status"),
                        "exit_code": result.get("exit_code"),
                        "receipt_artifact": str(receipt_path),
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
        output = _git(
            self.context.active_root,
            "status",
            "--porcelain",
            "--untracked-files=all",
            strip_output=False,
        )
        return {line[3:].replace("\\", "/") for line in output.splitlines() if len(line) > 3}

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
            "source_root": manifest["source_root"],
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
        apply_root = self.session_store.session_dir / "artifacts" / "applies"
        apply_root.mkdir(parents=True, exist_ok=True)
        artifact = apply_root / f"{receipt['apply_id']}.json"
        receipt["receipt_artifact"] = str(artifact)
        artifact.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        self._write_status(manifest, status, reason)
        self.session_store.append_event("apply_receipt", receipt)
        return receipt

    def _write_status(self, manifest: dict[str, Any], status: ApplyStatus, reason: str) -> dict[str, Any]:
        record = {
            "candidate_id": manifest["candidate_id"],
            "status": status.value,
            "reason": reason,
            "updated_at": _now(),
        }
        status_path = self.root / manifest["candidate_id"] / "status.json"
        status_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        return record


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
