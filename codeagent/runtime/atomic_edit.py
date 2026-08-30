from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from codeagent.safety.path_guard import PathGuard, PathGuardError
from codeagent.session.store import SessionStore
from codeagent.tools.base import ToolResult
from codeagent.workspace.workspace import SessionWorkspaceState, WorkspaceContext


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AtomicEditError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FileState:
    content: bytes | None
    origin_sha256: str | None


@dataclass(frozen=True)
class EditPlan:
    transaction_id: str
    operations: list[dict[str, Any]]
    before: dict[str, bytes | None]
    after: dict[str, bytes | None]
    created_directories: list[str]
    patch: str


class AtomicEditService:
    """在 Session active worktree 内执行可恢复的多文件文本事务。"""

    MAX_OPERATIONS = 50
    MAX_FILE_BYTES = 512_000
    MAX_TEXT_BYTES = 256_000
    MAX_PATCH_BYTES = 512_000
    _lock = threading.Lock()

    def __init__(self, context: WorkspaceContext, session_store: SessionStore) -> None:
        self.context = context
        self.session_store = session_store
        self.guard = PathGuard(context.active_root)
        self.root = session_store.diagnostics_dir / "transactions"

    def apply_workspace_edit(self, arguments: dict[str, Any]) -> ToolResult:
        state = self.session_store.workspace_state()
        if state == SessionWorkspaceState.WORKSPACE_TAINTED:
            return ToolResult(ok=False, error_code="workspace_tainted", error="workspace 已被命令副作用污染；只允许只读检查和 discard")
        if state == SessionWorkspaceState.RECOVERY_REQUIRED:
            return ToolResult(ok=False, error_code="recovery_required", error="Session 需要人工检查，编辑能力保持关闭")
        try:
            with self._lock:
                self.ensure_recovered()
                plan = self.prepare(arguments)
                receipt = self.commit(plan)
            return ToolResult(
                ok=True,
                content=(
                    f"已提交原子编辑事务 {receipt['transaction_id']}\n"
                    f"candidate_revision: {receipt['candidate_revision']}\n"
                    f"changed_files: {', '.join(receipt['changed_files'])}"
                ),
                metadata=receipt,
            )
        except AtomicEditError as exc:
            return ToolResult(ok=False, error_code=exc.code, error=f"workspace edit 被拒绝: {exc}", metadata={
                "workspace_changed": False,
                "operation_errors": _operation_argument_errors(arguments) if exc.code == "invalid_arguments" else [],
            })
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
            return ToolResult(ok=False, error_code="internal_error", error=f"workspace edit 失败: {exc}")

    def prepare(self, arguments: dict[str, Any]) -> EditPlan:
        self._validate_workspace()
        if not isinstance(arguments, dict) or set(arguments) != {"operations"}:
            raise AtomicEditError("invalid_arguments", "只允许 operations 参数")
        operations = arguments.get("operations")
        if not isinstance(operations, list) or not 1 <= len(operations) <= self.MAX_OPERATIONS:
            raise AtomicEditError("invalid_arguments", f"operations 数量必须为 1..{self.MAX_OPERATIONS}")

        normalized: list[dict[str, Any]] = []
        before: dict[str, bytes | None] = {}
        planned: dict[str, FileState] = {}
        required_dirs: set[str] = set()
        move_edges: dict[str, str] = {}
        text_bytes = 0

        def load(relative: str) -> FileState:
            if relative in planned:
                return planned[relative]
            target = self._checked_path(relative)
            if not target.exists():
                state = FileState(None, None)
            else:
                content = self._read_text_file(target, relative)
                digest = _sha(content)
                state = FileState(content, digest)
            before.setdefault(relative, state.content)
            planned[relative] = state
            return state

        for raw in operations:
            if not isinstance(raw, dict) or not isinstance(raw.get("op"), str):
                raise AtomicEditError("invalid_arguments", "每个 operation 必须是包含 op 的 object")
            op = raw["op"]
            allowed = {
                "create_file": {"op", "path", "content"},
                "replace_text": {"op", "path", "expected_sha256", "old_text", "new_text"},
                "delete_file": {"op", "path", "expected_sha256"},
                "move_file": {"op", "source", "destination", "expected_sha256"},
            }.get(op)
            if allowed is None or set(raw) != allowed:
                raise AtomicEditError("invalid_arguments", f"{op!r} operation 字段不完整或包含未知字段")
            item = dict(raw)
            if op == "move_file":
                source = self._normalize_path(item["source"])
                destination = self._normalize_path(item["destination"])
                if source == destination or source.casefold() == destination.casefold():
                    raise AtomicEditError("invalid_arguments", "move source/destination 不能相同或仅大小写不同")
                source_state = load(source)
                self._require_existing(source, source_state)
                self._check_expected(item["expected_sha256"], source_state, source)
                destination_state = load(destination)
                if destination_state.content is not None:
                    raise AtomicEditError("already_exists", f"move destination 已存在: {destination}")
                self._plan_parent(destination, planned, required_dirs)
                planned[source] = FileState(None, source_state.origin_sha256)
                planned[destination] = source_state
                move_edges[source] = destination
                item.update(source=source, destination=destination)
            else:
                path = self._normalize_path(item["path"])
                state = load(path)
                if op == "create_file":
                    if state.content is not None:
                        raise AtomicEditError("already_exists", f"create path 已存在: {path}")
                    content = self._text_bytes(item["content"], "content")
                    text_bytes += len(content)
                    self._plan_parent(path, planned, required_dirs)
                    planned[path] = FileState(content, None)
                elif op == "replace_text":
                    self._require_existing(path, state)
                    self._check_expected(item["expected_sha256"], state, path)
                    old_text = self._text(item["old_text"], "old_text")
                    new_text = self._text(item["new_text"], "new_text")
                    if not old_text:
                        raise AtomicEditError("invalid_arguments", "replace_text.old_text 不能为空")
                    text_bytes += len(old_text.encode()) + len(new_text.encode())
                    current = state.content.decode("utf-8")  # prepare 已验证编码
                    matched, replacement = _adapt_patch_newlines(current, old_text, new_text)
                    matches = current.count(matched)
                    if matches != 1:
                        raise AtomicEditError("edit_conflict", f"{path} old_text 必须唯一匹配，实际 {matches} 次；请重新读取")
                    content = current.replace(matched, replacement, 1).encode("utf-8")
                    planned[path] = FileState(content, state.origin_sha256)
                elif op == "delete_file":
                    self._require_existing(path, state)
                    self._check_expected(item["expected_sha256"], state, path)
                    planned[path] = FileState(None, state.origin_sha256)
                item["path"] = path
            normalized.append(item)
            if text_bytes > self.MAX_TEXT_BYTES:
                raise AtomicEditError("limit_exceeded", "transaction 文本字段总量超过限制")

        for start in move_edges:
            seen: set[str] = set()
            current = start
            while current in move_edges:
                if current in seen:
                    raise AtomicEditError("invalid_arguments", "move operations 不能形成 cycle")
                seen.add(current)
                current = move_edges[current]

        after = {path: state.content for path, state in planned.items() if before.get(path) != state.content}
        before = {path: before.get(path) for path in after}
        if not after:
            raise AtomicEditError("no_changes", "transaction 未产生最终文件变化")
        if len(after) > self.MAX_OPERATIONS:
            raise AtomicEditError("limit_exceeded", "transaction 涉及文件数超过限制")
        for path, content in after.items():
            if content is not None and len(content) > self.MAX_FILE_BYTES:
                raise AtomicEditError("limit_exceeded", f"编辑后文件超过大小限制: {path}")
        patch = _build_review_patch(before, after)
        if len(patch.encode("utf-8")) > self.MAX_PATCH_BYTES:
            raise AtomicEditError("limit_exceeded", "transaction diff 超过大小限制")
        return EditPlan(uuid.uuid4().hex, normalized, before, after, sorted(required_dirs, key=lambda p: (p.count("/"), p)), patch)

    def commit(self, plan: EditPlan) -> dict[str, Any]:
        tx_dir = self.root / plan.transaction_id
        try:
            tx_dir.mkdir(parents=True, exist_ok=False)
            backup = tx_dir / "backup"
            backup.mkdir()
            self._write_json(tx_dir / "request.json", {"operations": plan.operations})
            self._write_json(tx_dir / "plan.json", self._plan_record(plan))
            (tx_dir / "changes.patch").write_text(plan.patch, encoding="utf-8")
            for relative, content in plan.before.items():
                if content is not None:
                    target = backup / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
            self._write_state(tx_dir, "prepared")
            self._revalidate(plan)
            self._write_state(tx_dir, "committing")
            for relative in plan.created_directories:
                (self.context.active_root / relative).mkdir()
            for relative in sorted(plan.after):
                target = self.context.active_root / relative
                content = plan.after[relative]
                if content is None:
                    self._remove_path(target)
                else:
                    self._write_path(target, content)
            self._verify_after(plan)
        except Exception as exc:
            if tx_dir.exists() and self._state(tx_dir) in {"prepared", "committing"}:
                try:
                    self._restore(plan)
                    shutil.rmtree(tx_dir)
                except Exception as rollback_exc:
                    self._write_state(tx_dir, "recovery_required", error=f"{exc}; rollback: {rollback_exc}")
                    self.session_store.update_workspace_state("recovery_required", "atomic edit rollback 无法确认完整恢复")
                    raise AtomicEditError("rollback_failed", f"事务失败且 rollback 无法确认: {rollback_exc}") from exc
            if isinstance(exc, AtomicEditError):
                raise
            raise AtomicEditError("internal_error", f"事务提交失败，workspace 已恢复: {exc}") from exc

        revision = self._next_revision()
        receipt = self._receipt(plan, revision)
        self._write_json(tx_dir / "receipt.json", receipt)
        self._write_state(tx_dir, "committed", candidate_revision=revision)
        try:
            self.session_store.append_event("edit_transaction", receipt)
        except OSError as exc:
            raise AtomicEditError("internal_error", f"事务已提交但 event 待 resume 补建: {exc}") from exc
        # 成功事务的紧凑事实已经进入 events；完整 plan/patch/backup 不作为业务归档。
        shutil.rmtree(tx_dir)
        return receipt

    def ensure_recovered(self) -> None:
        if not self.root.exists():
            return
        for tx_dir in sorted(path for path in self.root.iterdir() if path.is_dir()):
            state = self._state(tx_dir)
            if state == "recovery_required":
                raise AtomicEditError("rollback_failed", "存在 recovery_required transaction，execution capability 已关闭")
            if state not in {"prepared", "committing", "committed"}:
                continue
            if state == "committed":
                self._repair_event(tx_dir)
                shutil.rmtree(tx_dir)
                continue
            try:
                plan = self._load_plan(tx_dir)
                self._restore(plan)
                shutil.rmtree(tx_dir)
            except Exception as exc:
                self._write_state(tx_dir, "recovery_required", error=str(exc))
                self.session_store.update_workspace_state("recovery_required", "未完成 atomic edit 无法恢复")
                raise AtomicEditError("rollback_failed", f"未完成事务恢复失败: {exc}") from exc

    def _validate_workspace(self) -> None:
        if self.context.workspace_kind != "git_worktree" or self.context.active_root == self.context.source_root:
            raise AtomicEditError("path_rejected", "写工具只允许 Session 的独立 Agent worktree，不能直接写 source")

    def _normalize_path(self, value: Any) -> str:
        if not isinstance(value, str) or not value or len(value) > 1024 or "\0" in value or "\\" in value:
            raise AtomicEditError("invalid_arguments", "path 必须是非空 workspace-relative POSIX path")
        raw = PurePosixPath(value)
        if raw.is_absolute() or value in {".", ".."} or raw.as_posix() != value or any(part in {"", ".", ".."} for part in raw.parts):
            raise AtomicEditError("invalid_arguments", "path 不能是绝对路径或包含 . / .. component")
        relative = raw.as_posix()
        self._checked_path(relative)
        return relative

    def _checked_path(self, relative: str) -> Path:
        current = self.context.active_root
        try:
            for part in PurePosixPath(relative).parts:
                current = current / part
                if current.is_symlink():
                    raise AtomicEditError("path_rejected", "编辑路径不能经过 symlink")
            return self.guard.resolve(relative)
        except PathGuardError as exc:
            raise AtomicEditError("path_rejected", str(exc)) from exc

    def _read_text_file(self, target: Path, relative: str) -> bytes:
        if target.is_symlink() or not target.is_file():
            raise AtomicEditError("path_rejected", f"目标不是普通文件: {relative}")
        content = target.read_bytes()
        if len(content) > self.MAX_FILE_BYTES:
            raise AtomicEditError("limit_exceeded", f"文件超过大小限制: {relative}")
        if b"\0" in content[:8192]:
            raise AtomicEditError("path_rejected", f"拒绝编辑二进制文件: {relative}")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AtomicEditError("unsupported_text_encoding", f"文件不是合法 UTF-8: {relative}") from exc
        return content

    def _plan_parent(self, relative: str, planned: dict[str, FileState], required: set[str]) -> None:
        parent = PurePosixPath(relative).parent
        chain: list[PurePosixPath] = []
        while parent.as_posix() != ".":
            chain.append(parent)
            parent = parent.parent
        for item in reversed(chain):
            rel = item.as_posix()
            target = self._checked_path(rel)
            if rel in planned and planned[rel].content is not None:
                raise AtomicEditError("invalid_arguments", f"parent 被计划为文件: {rel}")
            if target.exists():
                if not target.is_dir() or target.is_symlink():
                    raise AtomicEditError("path_rejected", f"parent 不是普通目录: {rel}")
            else:
                required.add(rel)

    @staticmethod
    def _require_existing(path: str, state: FileState) -> None:
        if state.content is None:
            raise AtomicEditError("not_found", f"source 不存在: {path}")

    @staticmethod
    def _check_expected(value: Any, state: FileState, path: str) -> None:
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise AtomicEditError("invalid_arguments", "expected_sha256 必须是 64 位小写十六进制")
        if state.origin_sha256 is None or value != state.origin_sha256:
            raise AtomicEditError("edit_conflict", f"{path} SHA-256 已变化；请重新 read_file 后重试")

    @staticmethod
    def _text(value: Any, name: str) -> str:
        if not isinstance(value, str):
            raise AtomicEditError("invalid_arguments", f"{name} 必须是字符串")
        return value

    def _text_bytes(self, value: Any, name: str) -> bytes:
        return self._text(value, name).encode("utf-8")

    def _revalidate(self, plan: EditPlan) -> None:
        for relative, expected in plan.before.items():
            target = self._checked_path(relative)
            actual = target.read_bytes() if target.exists() and target.is_file() and not target.is_symlink() else None
            if actual != expected:
                raise AtomicEditError("edit_conflict", f"提交前路径状态发生变化: {relative}")
        for relative in plan.created_directories:
            target = self._checked_path(relative)
            if target.exists():
                raise AtomicEditError("edit_conflict", f"提交前 parent directory 状态发生变化: {relative}")

    def _verify_after(self, plan: EditPlan) -> None:
        for relative, expected in plan.after.items():
            target = self._checked_path(relative)
            actual = target.read_bytes() if target.exists() and target.is_file() and not target.is_symlink() else None
            if actual != expected:
                raise OSError(f"提交后校验失败: {relative}")

    def _restore(self, plan: EditPlan) -> None:
        for relative, content in sorted(plan.before.items(), reverse=True):
            target = self.context.active_root / relative
            if content is None:
                if target.exists() or target.is_symlink():
                    self._remove_path(target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                self._write_path(target, content)
        for relative in sorted(plan.created_directories, key=lambda p: (p.count("/"), p), reverse=True):
            target = self.context.active_root / relative
            if target.is_dir() and not any(target.iterdir()):
                target.rmdir()
        for relative, expected in plan.before.items():
            target = self.context.active_root / relative
            actual = target.read_bytes() if target.exists() and target.is_file() else None
            if actual != expected:
                raise OSError(f"rollback 校验失败: {relative}")

    def _write_path(self, target: Path, content: bytes) -> None:
        temp = target.parent / f".{target.name}.codeagent-{uuid.uuid4().hex}.tmp"
        try:
            temp.write_bytes(content)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _remove_path(target: Path) -> None:
        target.unlink()

    def _plan_record(self, plan: EditPlan) -> dict[str, Any]:
        return {
            "transaction_id": plan.transaction_id,
            "operations": plan.operations,
            "before": {path: _identity(content) for path, content in plan.before.items()},
            "after": {path: _identity(content) for path, content in plan.after.items()},
            "created_directories": plan.created_directories,
        }

    def _receipt(self, plan: EditPlan, revision: int) -> dict[str, Any]:
        file_changes = [
            {"path": path, "before_sha256": _sha_or_none(plan.before[path]), "after_sha256": _sha_or_none(plan.after[path]),
             "before_exists": plan.before[path] is not None, "after_exists": plan.after[path] is not None}
            for path in sorted(plan.after)
        ]
        moves = [
            {"source": item["source"], "destination": item["destination"]}
            for item in plan.operations if item["op"] == "move_file"
        ]
        return {
            "schema_version": 1, "transaction_id": plan.transaction_id, "candidate_revision": revision,
            "session_id": self.session_store.session_id, "workspace_revision": self.context.workspace_revision,
            "base_commit": self.context.base_commit, "file_changes": file_changes,
            "changed_files": sorted(plan.after), "created_directories": plan.created_directories,
            "moved_files": moves, "before_tree_sha256": _tree_hash(plan.before), "after_tree_sha256": _tree_hash(plan.after),
            "patch_sha256": _sha(plan.patch.encode()),
            "committed_at": datetime.now(timezone.utc).isoformat(),
        }

    def _next_revision(self) -> int:
        return self.session_store.next_candidate_revision()

    def _write_state(self, tx_dir: Path, state: str, **extra: Any) -> None:
        self._write_json(tx_dir / "transaction.json", {"state": state, "updated_at": datetime.now(timezone.utc).isoformat(), **extra})

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)

    @staticmethod
    def _state(tx_dir: Path) -> str | None:
        try:
            return json.loads((tx_dir / "transaction.json").read_text(encoding="utf-8")).get("state")
        except (OSError, json.JSONDecodeError):
            return None

    def _load_plan(self, tx_dir: Path) -> EditPlan:
        record = json.loads((tx_dir / "plan.json").read_text(encoding="utf-8"))
        backup = tx_dir / "backup"
        before = {
            path: (backup / path).read_bytes() if identity.get("exists") else None
            for path, identity in record["before"].items()
        }
        after: dict[str, bytes | None] = {}
        for path, identity in record["after"].items():
            if not identity.get("exists"):
                after[path] = None
            else:
                target = self.context.active_root / path
                after[path] = target.read_bytes() if target.is_file() and _sha(target.read_bytes()) == identity.get("sha256") else b""
        return EditPlan(record["transaction_id"], record["operations"], before, after, record["created_directories"], "")

    def _repair_event(self, tx_dir: Path) -> None:
        receipt_path = tx_dir / "receipt.json"
        if not receipt_path.is_file():
            raise AtomicEditError("rollback_failed", "committed transaction 缺少 receipt")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if not any(event.type == "edit_transaction" and event.payload.get("transaction_id") == receipt.get("transaction_id") for event in self.session_store.read_events()):
            self.session_store.append_event("edit_transaction", receipt)


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha_or_none(content: bytes | None) -> str | None:
    return _sha(content) if content is not None else None


def _identity(content: bytes | None) -> dict[str, Any]:
    return {"exists": content is not None, "sha256": _sha_or_none(content)}


def _tree_hash(states: dict[str, bytes | None]) -> str:
    digest = hashlib.sha256()
    for path in sorted(states):
        digest.update(path.encode() + b"\0" + (b"file\0" + _sha(states[path]).encode() if states[path] is not None else b"absent\0"))
    return digest.hexdigest()


def _build_review_patch(before: dict[str, bytes | None], after: dict[str, bytes | None]) -> str:
    chunks: list[str] = []
    for path in sorted(after):
        old = before[path].decode("utf-8").splitlines(keepends=True) if before[path] is not None else []
        new = after[path].decode("utf-8").splitlines(keepends=True) if after[path] is not None else []
        chunks.extend(difflib.unified_diff(old, new, fromfile=f"a/{path}" if before[path] is not None else "/dev/null", tofile=f"b/{path}" if after[path] is not None else "/dev/null"))
    return "".join(chunks)


def _adapt_patch_newlines(before: str, old_text: str, new_text: str) -> tuple[str, str]:
    without_crlf = before.replace("\r\n", "")
    styles = [("\r\n", "\r\n" in before), ("\n", "\n" in without_crlf), ("\r", "\r" in without_crlf)]
    found = [newline for newline, present in styles if present]
    if len(found) != 1:
        return old_text, new_text
    newline = found[0]
    adapt = lambda value: value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)
    return adapt(old_text), adapt(new_text)


def _operation_argument_errors(arguments: Any) -> list[dict[str, Any]]:
    """只报告可机械确定的字段错误，不从自然语言错误中反推。"""
    if not isinstance(arguments, dict) or not isinstance(arguments.get("operations"), list):
        return [{"field": "operations", "reason": "required_array"}]
    errors = []
    for index, operation in enumerate(arguments["operations"]):
        if not isinstance(operation, dict):
            errors.append({"operation_index": index, "field": None, "reason": "object_required"})
        elif not isinstance(operation.get("op"), str):
            errors.append({"operation_index": index, "field": "op", "reason": "required_string"})
    return errors
