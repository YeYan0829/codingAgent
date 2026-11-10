from __future__ import annotations

import difflib
import hashlib
import json
import os
import stat
import re
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from codeagent.safety.path_guard import PathGuardError
from codeagent.session.store import SessionStore
from codeagent.tools.base import ToolResult
from codeagent.workspace.workspace import SessionWorkspaceState, WorkspaceContext


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OPERATION_FIELDS = {
    "create_file": {"op", "path", "content", "overwrite"},
    "replace_text": {"op", "path", "expected_sha256", "old_text", "new_text"},
    "delete_file": {"op", "path", "expected_sha256"},
    "move_file": {"op", "source", "destination", "expected_sha256"},
}
# 可省略字段：op 可由字段唯一推断；replace_text 由 old_text 唯一匹配定位，hash 只作可选的整文件版本约束。
_OPTIONAL_FIELDS = {
    "create_file": {"op", "overwrite"},
    "replace_text": {"op", "expected_sha256"},
    "delete_file": {"expected_sha256"},
    "move_file": {"expected_sha256"},
}


def _infer_op(raw: dict) -> Any:
    if "op" in raw:
        return raw["op"]
    # 模型有时把操作名写成键（{"replace_text": ..., "path": ...}）；键名唯一对应操作时按该操作处理。
    named = [key for key in raw if key in _OPERATION_FIELDS]
    if len(named) == 1:
        return named[0]
    if {"old_text", "new_text"} <= set(raw):
        return "replace_text"
    if "content" in raw and "old_text" not in raw:
        return "create_file"
    return None


class AtomicEditError(RuntimeError):
    def __init__(self, code: str, message: str, *, workspace_changed: bool | None = False) -> None:
        super().__init__(message)
        self.code = code
        self.workspace_changed = workspace_changed


@dataclass(frozen=True)
class FileState:
    content: bytes | None
    origin_sha256: str | None
    mode: int | None = None


@dataclass(frozen=True)
class EditPlan:
    transaction_id: str
    operations: list[dict[str, Any]]
    before: dict[str, bytes | None]
    after: dict[str, bytes | None]
    created_directories: list[str]
    patch: str
    before_modes: dict[str, int | None] = field(default_factory=dict)
    after_modes: dict[str, int | None] = field(default_factory=dict)
    undo_of: tuple[str, ...] = ()


class AtomicEditService:
    """在当前项目执行可恢复的局部文件事务，并保留持久撤销数据。"""

    MAX_OPERATIONS = 50
    MAX_FILE_BYTES = 512_000
    MAX_TEXT_BYTES = 256_000
    MAX_PATCH_BYTES = 512_000
    _lock = threading.Lock()

    def __init__(self, context: WorkspaceContext, session_store: SessionStore) -> None:
        self.context = context
        self.session_store = session_store
        self.guard = context.path_guard()
        self.root = session_store.diagnostics_dir / "transactions"

    def apply_workspace_edit(self, arguments: dict[str, Any]) -> ToolResult:
        state = self.session_store.workspace_state()
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
                    f"changed_files: {', '.join(receipt['changed_files'])}"
                ),
                metadata=receipt,
            )
        except AtomicEditError as exc:
            return ToolResult(ok=False, error_code=exc.code, error=f"workspace edit 被拒绝: {exc}", metadata={
                "workspace_changed": exc.workspace_changed,
                "operation_errors": _operation_argument_errors(arguments) if exc.code == "invalid_arguments" else [],
            })
        except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
            return ToolResult(ok=False, error_code="internal_error", error=f"workspace edit 失败: {exc}")

    def edit_file(self, arguments: dict[str, Any]) -> ToolResult:
        """单文件文本替换的扁平入口；事务、审计与恢复沿用 apply_workspace_edit。"""
        if isinstance(arguments, dict) and not arguments.get("path") and isinstance(arguments.get("edits"), list):
            # 真实运行中模型会把 path 写进每个 edit 项；同一文件时无歧义，直接提升到顶层。
            paths = {item.get("path") for item in arguments["edits"] if isinstance(item, dict)}
            if len(paths) == 1 and isinstance(next(iter(paths)), str) and next(iter(paths)):
                arguments = {**arguments, "path": paths.pop(),
                             "edits": [{k: v for k, v in item.items() if k != "path"} for item in arguments["edits"]]}
        if not isinstance(arguments, dict) or not isinstance(arguments.get("path"), str) or not arguments["path"]:
            return ToolResult(ok=False, error_code="invalid_arguments", error="edit_file 缺少 path：传入工作区相对文件路径。本次未写入文件",
                              metadata={"workspace_changed": False})
        edits = arguments.get("edits")
        if edits is None and {"old_text", "new_text"} <= set(arguments):
            edits = [{"old_text": arguments["old_text"], "new_text": arguments["new_text"]}]
        unknown = set(arguments) - {"path", "edits", "old_text", "new_text", "expected_sha256"}
        if (unknown or not isinstance(edits, list) or not edits
                or not all(isinstance(item, dict) and set(item) == {"old_text", "new_text"} for item in edits)):
            return ToolResult(ok=False, error_code="invalid_arguments", error=(
                "edit_file 需要 path 和 edits=[{old_text, new_text}, ...]"
                + (f"；未知字段：{sorted(map(str, unknown))[:10]}" if unknown else "") + "。本次未写入文件"),
                metadata={"workspace_changed": False})
        extra = {"expected_sha256": arguments["expected_sha256"]} if "expected_sha256" in arguments else {}
        return self.apply_workspace_edit({"operations": [
            {"op": "replace_text", "path": arguments["path"], **extra, **item} for item in edits]})

    def undo(self, turn_id: str | None = None, *, expected_edit_seq: int | None = None) -> ToolResult:
        """撤销最近一轮的受控文件编辑；shell副作用不在此范围。"""
        from codeagent.orchestration.lease import session_lease
        from codeagent.safety.repository_access import session_access_lease
        try:
            with session_access_lease(self.session_store), session_lease(self.session_store), self._lock:
                self.ensure_recovered()
                if self.session_store.workspace_state() == SessionWorkspaceState.RECOVERY_REQUIRED:
                    raise AtomicEditError("recovery_required", "存在未确认的恢复，先保留现场")
                events = [e for e in self.session_store.read_events() if e.type == "edit_transaction"]
                if expected_edit_seq is not None and max((e.seq or 0 for e in events), default=0) != expected_edit_seq:
                    raise AtomicEditError("undo_changed", "受控编辑已变化，请刷新后重新确认撤销范围")
                undone = {ident for e in events for ident in e.payload.get("undo_of", [])}
                candidates = [e for e in events if not e.payload.get("undo_of")
                    and e.payload["transaction_id"] not in undone
                    and (turn_id is None or e.turn_id == turn_id)
                    and (self.root / e.payload["transaction_id"] / "plan.json").is_file()
                    and json.loads((self.root / e.payload["transaction_id"] / "plan.json").read_text()).get("workspace") == str(self.context.active_root)]
                if not candidates:
                    raise AtomicEditError("nothing_to_undo", "没有可撤销的受控编辑")
                latest = candidates[-1]
                selected = [e for e in candidates if e.turn_id == latest.turn_id] if latest.turn_id else [latest]
                before, after, before_modes, after_modes = {}, {}, {}, {}
                directories = set()
                for event in reversed(selected):
                    original = self._load_plan(self.root / event.payload["transaction_id"])
                    directories.update(original.created_directories)
                    for relative, expected in original.after.items():
                        if relative not in before:
                            target = self._checked_path(relative)
                            actual = self._read_text_file(target, relative) if target.exists() else None
                            mode = stat.S_IMODE(target.stat().st_mode) if actual is not None else None
                            before[relative] = after[relative] = actual
                            before_modes[relative] = after_modes[relative] = mode
                        if (after[relative] != expected or (expected is not None and relative in original.after_modes
                                and after_modes[relative] != original.after_modes[relative])):
                            raise AtomicEditError("undo_conflict", f"{relative} 已被后续操作改变；本次未撤销任何文件")
                        after[relative] = original.before[relative]
                        after_modes[relative] = original.before_modes.get(relative)
                plan = EditPlan(uuid.uuid4().hex, [], before, after, [], _build_review_patch(before, after),
                    before_modes, after_modes, tuple(e.payload["transaction_id"] for e in selected))
                receipt = self.commit(plan)
                # 只删除本轮创建且现在为空的目录，外部新增内容保留。
                for relative in sorted(directories, key=lambda p: (p.count("/"), p), reverse=True):
                    target = self._checked_path(relative)
                    if target.is_dir() and not any(target.iterdir()):
                        target.rmdir()
                return ToolResult(ok=True, content="已撤销受控编辑；命令产生的其他修改保留。", metadata=receipt)
        except AtomicEditError as exc:
            return ToolResult(ok=False, error_code=exc.code, error=str(exc))
        except (OSError, ValueError, RuntimeError) as exc:
            return ToolResult(ok=False, error_code="undo_unavailable", error=str(exc))

    def preview_file(self, relative: str, transaction_id: str) -> dict[str, Any]:
        """读取受控编辑起点与当前文件，沿用编辑路径和备份完整性检查。"""
        if not re.fullmatch(r"[a-f0-9]{32}", transaction_id):
            raise AtomicEditError("invalid_transaction", "编辑事务标识无效")
        target = self._checked_path(relative)
        plan = self._load_plan(self.root / transaction_id)
        if relative not in plan.before:
            raise AtomicEditError("unknown_edit_file", "路径不属于该受控编辑")
        before = plan.before[relative]
        after = self._read_text_file(target, relative) if target.exists() else None
        return {"path": relative, "before": (before or b"").decode("utf-8"),
                "after": (after or b"").decode("utf-8"), "beforeExists": before is not None,
                "afterExists": after is not None, "scope": "controlled_start_to_current"}

    def preview_patch(self) -> bytes:
        """按需显示受控编辑所涉及文件的当前差异，不冻结项目或创建候选。"""
        events = [e for e in self.session_store.read_events() if e.type == "edit_transaction"]
        undone = {ident for e in events for ident in e.payload.get("undo_of", [])}
        before = {}
        for event in events:
            ident = event.payload["transaction_id"]
            if ident in undone or event.payload.get("undo_of"):
                continue
            record_path = self.root / ident / "plan.json"
            if not record_path.exists() or json.loads(record_path.read_text()).get("workspace") != str(self.context.active_root):
                continue
            plan = self._load_plan(record_path.parent)
            for path, content in plan.before.items():
                before.setdefault(path, content)
        after = {}
        for path in before:
            target = self._checked_path(path)
            after[path] = self._read_text_file(target, path) if target.exists() else None
        return _build_review_patch(before, after).encode("utf-8")

    def prepare(self, arguments: dict[str, Any]) -> EditPlan:
        self._validate_workspace()
        if not isinstance(arguments, dict) or set(arguments) != {"operations"}:
            raise AtomicEditError("invalid_arguments", "只允许 operations 参数")
        operations = arguments.get("operations")
        if not isinstance(operations, list) or not 1 <= len(operations) <= self.MAX_OPERATIONS:
            raise AtomicEditError("invalid_arguments", f"operations 数量必须为 1..{self.MAX_OPERATIONS}")

        normalized: list[dict[str, Any]] = []
        before: dict[str, bytes | None] = {}
        before_modes: dict[str, int | None] = {}
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
                state = FileState(content, digest, stat.S_IMODE(target.stat().st_mode))
            before.setdefault(relative, state.content)
            before_modes.setdefault(relative, state.mode)
            planned[relative] = state
            return state

        for index, raw in enumerate(operations):
            if not isinstance(raw, dict):
                raise AtomicEditError("invalid_arguments", f"operations[{index}] 必须是 object。本次事务未写入文件")
            op = _infer_op(raw)
            if not isinstance(op, str):
                raise AtomicEditError("invalid_arguments", (
                    f"operations[{index}] 缺少 op 且无法从字段推断：替换文本提供 path/old_text/new_text，"
                    f"新建文件提供 path/content，删除/移动需显式 op。本次事务未写入文件"))
            allowed = _OPERATION_FIELDS.get(op)
            if allowed is None:
                raise AtomicEditError("invalid_arguments", f"operations[{index}].op 不支持；可用值：{', '.join(_OPERATION_FIELDS)}")
            present = (set(raw) | {"op"}) - ({op} if "op" not in raw else set())
            required = allowed - _OPTIONAL_FIELDS.get(op, set())
            if not required <= present or present - allowed:
                missing = sorted(required - present)
                extra = sorted(str(key)[:80] for key in present - allowed)[:10]
                hint = "；expected_sha256 须来自 read_file 的完整文件 hash" if "expected_sha256" in missing else ""
                raise AtomicEditError("invalid_arguments", f"operations[{index}] ({op}) 缺少字段：{missing}；未知字段：{extra}{hint}。本次事务未写入文件")
            item = {k: v for k, v in raw.items() if k != op or k == "op"}
            item["op"] = op
            if op == "move_file":
                source = self._normalize_path(item["source"])
                destination = self._normalize_path(item["destination"])
                if source == destination:
                    raise AtomicEditError("invalid_arguments", "move source/destination 不能相同")
                source_state = load(source)
                self._require_existing(source, source_state)
                if "expected_sha256" in item:
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
                    overwrite = item.get("overwrite", False)
                    if not isinstance(overwrite, bool):
                        raise AtomicEditError("invalid_arguments", "overwrite 必须是布尔值")
                    if state.content is not None and not overwrite:
                        raise AtomicEditError("already_exists", f"create path 已存在: {path}；整体重写已有文件时设置 overwrite=true，局部修改用 replace_text")
                    content = self._text_bytes(item["content"], "content")
                    text_bytes += len(content)
                    if state.content is None:
                        self._plan_parent(path, planned, required_dirs)
                    planned[path] = FileState(content, state.origin_sha256, state.mode if state.mode is not None else 0o644)
                elif op == "replace_text":
                    self._require_existing(path, state)
                    if "expected_sha256" in item:
                        self._check_expected(item["expected_sha256"], state, path)
                    old_text = self._text(item["old_text"], "old_text")
                    new_text = self._text(item["new_text"], "new_text")
                    text_bytes += len(old_text.encode()) + len(new_text.encode())
                    current = state.content.decode("utf-8")  # prepare 已验证编码
                    if not old_text and current:
                        raise AtomicEditError("edit_conflict", f"{path} 空 old_text 仅可填充空文件；当前文件非空，请重新读取并提供唯一匹配片段")
                    matched, replacement = _adapt_patch_newlines(current, old_text, new_text)
                    matches = current.count(matched)
                    if matches != 1:
                        raise AtomicEditError("edit_conflict", f"{path} old_text 必须唯一匹配，实际 {matches} 次；{_match_hint(current, matched, matches)}")
                    content = current.replace(matched, replacement, 1).encode("utf-8")
                    planned[path] = FileState(content, state.origin_sha256, state.mode if state.mode is not None else 0o644)
                elif op == "delete_file":
                    self._require_existing(path, state)
                    if "expected_sha256" in item:
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
        return EditPlan(uuid.uuid4().hex, normalized, before, after, sorted(required_dirs, key=lambda p: (p.count("/"), p)), patch,
            {path: before_modes[path] for path in after}, {path: planned[path].mode for path in after})

    def commit(self, plan: EditPlan) -> dict[str, Any]:
        tx_dir = self.root / plan.transaction_id
        try:
            tx_dir.mkdir(parents=True, exist_ok=False)
            backup = tx_dir / "backup"
            backup.mkdir()
            self._write_json(tx_dir / "plan.json", self._plan_record(plan))
            for relative, content in plan.before.items():
                if content is not None:
                    target = backup / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
            for relative, content in plan.after.items():
                if content is not None:
                    target = tx_dir / "after" / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
            self._write_state(tx_dir, "prepared")
            self._revalidate(plan)
            self._write_state(tx_dir, "committing")
            for relative in plan.created_directories:
                self._checked_path(relative).mkdir()
            for relative in sorted(plan.after):
                target = self._checked_path(relative)
                content = plan.after[relative]
                if content is None:
                    self._remove_path(target)
                else:
                    self._write_path(target, content)
                    if plan.after_modes.get(relative) is not None:
                        self._chmod(target, plan.after_modes[relative])
            self._verify_after(plan)
        except Exception as exc:
            if tx_dir.exists() and self._state(tx_dir) in {"prepared", "committing"}:
                try:
                    self._restore(plan)
                    shutil.rmtree(tx_dir)
                except Exception as rollback_exc:
                    self._write_state(tx_dir, "recovery_required", error=f"{exc}; rollback: {rollback_exc}")
                    self.session_store.update_workspace_state("recovery_required", "atomic edit rollback 无法确认完整恢复")
                    raise AtomicEditError("rollback_failed", f"事务失败且 rollback 无法确认: {rollback_exc}", workspace_changed=None) from exc
            if isinstance(exc, AtomicEditError):
                raise
            raise AtomicEditError("internal_error", f"事务提交失败，workspace 已恢复: {exc}") from exc

        receipt = self._receipt(plan)
        self._write_json(tx_dir / "receipt.json", receipt)
        self._write_state(tx_dir, "committed")
        try:
            self.session_store.append_event("edit_transaction", receipt)
        except OSError as exc:
            raise AtomicEditError("internal_error", f"事务已提交但 event 待 resume 补建: {exc}", workspace_changed=True) from exc
        # 只保存实际涉及文件的前后内容，供持久undo；不构造或认证整个项目版本。
        return receipt

    def ensure_recovered(self) -> None:
        if not self.root.exists():
            return
        for tx_dir in sorted(path for path in self.root.iterdir() if path.is_dir()):
            plan_path = tx_dir / "plan.json"
            if plan_path.exists():
                record = json.loads(plan_path.read_text(encoding="utf-8"))
                if record.get("workspace") not in {None, str(self.context.active_root)}:
                    continue
                if record.get("workspace") is None and any(e.type == "legacy_workspace_detached" for e in self.session_store.read_events()):
                    continue  # 旧隔离目录的事务不能恢复到当前项目。
            state = self._state(tx_dir)
            if state == "recovery_required":
                raise AtomicEditError("rollback_failed", "存在 recovery_required transaction，execution capability 已关闭")
            if state not in {"prepared", "committing", "committed"}:
                continue
            if state == "committed":
                self._repair_event(tx_dir)
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
        if not self.context.active_root.is_dir():
            raise AtomicEditError("path_rejected", "当前项目目录不存在")

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
        if any(part in {".git", ".codeagent"} for part in PurePosixPath(relative).parts):
            raise AtomicEditError("path_rejected", "不能编辑Git或Runtime内部记录")
        current = self.context.active_root
        try:
            for part in PurePosixPath(relative).parts:
                current = current / part
                if current.is_symlink():
                    raise AtomicEditError("path_rejected", "编辑路径不能经过 symlink")
            target = self.guard.resolve(relative)
            if target.is_relative_to(self.session_store.session_root):
                raise AtomicEditError("path_rejected", "不能编辑Session存储")
            return target
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
            raise AtomicEditError("edit_conflict", (
                f"{path} 的 expected_sha256 与当前文件不一致（当前 sha256={state.origin_sha256}）。"
                "可能是之后的编辑或命令改写了文件，也可能是 hash 抄写有误；需要时重读相关片段。"
                "replace_text 也可以省略 expected_sha256，由 old_text 唯一匹配定位"))

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
            if actual != expected or (expected is not None and relative in plan.before_modes
                    and stat.S_IMODE(target.stat().st_mode) != plan.before_modes[relative]):
                raise AtomicEditError("edit_conflict", f"提交前路径状态发生变化: {relative}")
        for relative in plan.created_directories:
            target = self._checked_path(relative)
            if target.exists():
                raise AtomicEditError("edit_conflict", f"提交前 parent directory 状态发生变化: {relative}")

    def _verify_after(self, plan: EditPlan) -> None:
        for relative, expected in plan.after.items():
            target = self._checked_path(relative)
            actual = target.read_bytes() if target.exists() and target.is_file() and not target.is_symlink() else None
            if actual != expected or (expected is not None and relative in plan.after_modes
                    and stat.S_IMODE(target.stat().st_mode) != plan.after_modes[relative]):
                raise OSError(f"提交后校验失败: {relative}")

    def _restore(self, plan: EditPlan) -> None:
        # 恢复也不能覆盖崩溃后出现的外部修改，先检查全部路径再写入。
        for relative in plan.before:
            target = self._checked_path(relative)
            actual = self._read_text_file(target, relative) if target.exists() else None
            if actual not in (plan.before[relative], plan.after[relative]):
                raise AtomicEditError("edit_conflict", f"恢复路径已被外部修改: {relative}")
        for relative, content in sorted(plan.before.items(), reverse=True):
            target = self._checked_path(relative)
            if content is None:
                if target.exists() or target.is_symlink():
                    self._remove_path(target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                self._write_path(target, content)
                if plan.before_modes.get(relative) is not None:
                    self._chmod(target, plan.before_modes[relative])
        for relative in sorted(plan.created_directories, key=lambda p: (p.count("/"), p), reverse=True):
            target = self._checked_path(relative)
            if target.is_dir() and not any(target.iterdir()):
                target.rmdir()
        for relative, expected in plan.before.items():
            target = self._checked_path(relative)
            actual = target.read_bytes() if target.exists() and target.is_file() else None
            if actual != expected:
                raise OSError(f"rollback 校验失败: {relative}")

    def _write_path(self, target: Path, content: bytes) -> None:
        temp = target.parent / f".{target.name}.codeagent-{uuid.uuid4().hex}.tmp"
        try:
            temp.write_bytes(content)
            # os.replace 会用临时文件的默认权限替换 inode。编辑既有文件时应保留
            # 原文件 mode，否则一次纯文本修改会意外移除 executable 等权限位。
            if target.exists():
                os.chmod(temp, stat.S_IMODE(target.stat().st_mode))
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _remove_path(target: Path) -> None:
        target.unlink()

    @staticmethod
    def _chmod(target: Path, mode: int) -> None:
        os.chmod(target, mode)

    def _plan_record(self, plan: EditPlan) -> dict[str, Any]:
        return {
            "transaction_id": plan.transaction_id,
            "workspace": str(self.context.active_root),
            "before": {path: _identity(content) for path, content in plan.before.items()},
            "after": {path: _identity(content) for path, content in plan.after.items()},
            "created_directories": plan.created_directories,
            "before_modes": plan.before_modes, "after_modes": plan.after_modes, "undo_of": list(plan.undo_of),
        }

    def _receipt(self, plan: EditPlan) -> dict[str, Any]:
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
            "schema_version": 1, "transaction_id": plan.transaction_id, "undo_of": list(plan.undo_of),
            "session_id": self.session_store.session_id, "file_changes": file_changes,
            "changed_files": sorted(plan.after), "created_directories": plan.created_directories,
            "moved_files": moves,
            "committed_at": datetime.now(timezone.utc).isoformat(),
        }


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
        if record.get("workspace") not in {None, str(self.context.active_root)}:
            raise AtomicEditError("historical_transaction", "其他工作目录的事务仅保留历史，不恢复到当前项目")
        def content(kind):
            result = {}
            for path, identity in record[kind].items():
                self._normalize_path(path)
                target = tx_dir / ("backup" if kind == "before" else "after") / path
                if not identity.get("exists"):
                    result[path] = None
                elif target.is_file():
                    result[path] = target.read_bytes()
                    if _sha(result[path]) != identity["sha256"]:
                        raise AtomicEditError("rollback_failed", "事务文件内容损坏")
                elif kind == "after":  # 旧事务没有持久after，只有当前内容吻合才可恢复。
                    current = self._checked_path(path)
                    if not current.is_file() or _sha(current.read_bytes()) != identity["sha256"]:
                        raise AtomicEditError("edit_conflict", "旧事务路径已改变；保留现场")
                    result[path] = current.read_bytes()
                else:
                    raise AtomicEditError("rollback_failed", "事务备份缺失")
            return result
        return EditPlan(record["transaction_id"], record.get("operations", []), content("before"), content("after"),
            record["created_directories"], "", record.get("before_modes", {}), record.get("after_modes", {}), tuple(record.get("undo_of", [])))

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
        elif not isinstance(_infer_op(operation), str):
            errors.append({"operation_index": index, "field": "op", "reason": "required_string"})
        elif _infer_op(operation) not in _OPERATION_FIELDS:
            errors.append({"operation_index": index, "field": "op", "reason": "unsupported_operation"})
        else:
            op = _infer_op(operation)
            fields = _OPERATION_FIELDS[op]
            present = set(operation) | {"op"}
            errors.extend({"operation_index": index, "field": field, "reason": "required"}
                          for field in sorted(fields - _OPTIONAL_FIELDS.get(op, set()) - present))
            errors.extend({"operation_index": index, "field": str(field)[:80], "reason": "unexpected_field"}
                          for field in sorted(present - fields, key=str)[:10])
    return errors


def _match_hint(current: str, old_text: str, matches: int) -> str:
    """给出可直接用于修正 old_text 的位置信息，而不是只要求重读整个文件。"""
    lines = current.splitlines()
    if matches > 1:
        starts, offset = [], 0
        while len(starts) < 5 and (found := current.find(old_text, offset)) >= 0:
            starts.append(current.count("\n", 0, found) + 1)
            offset = found + 1
        return f"匹配起始行 {starts}；加入前后若干行上下文使其唯一"
    probe = next((line.strip() for line in old_text.splitlines() if line.strip()), "")
    candidates = [number for number, line in enumerate(lines, 1) if probe and line.strip() == probe][:5]
    if candidates:
        return (f"old_text 首个非空行在第 {candidates} 行出现，但后续内容或缩进/空白不一致；"
                "按 read_file 返回内容原样复制该区域后重试")
    return "old_text 首个非空行在当前文件中不存在；文件可能已被修改，请重读目标区域"
