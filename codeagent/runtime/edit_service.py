from __future__ import annotations

import difflib
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codeagent.safety.path_guard import PathGuard, PathGuardError
from codeagent.session.store import SessionStore
from codeagent.tools.base import ToolResult
from codeagent.workspace.workspace import WorkspaceContext


class EditError(RuntimeError):
    """受控文本编辑被拒绝。"""


class TextPatchService:
    MAX_PATCH_CHARS = 64_000
    MAX_FILE_BYTES = 256_000

    def __init__(self, context: WorkspaceContext, session_store: SessionStore) -> None:
        self.context = context
        self.session_store = session_store
        self.guard = PathGuard(context.active_root)
        self.journal_root = session_store.session_dir / "artifacts" / "edits"

    def apply_text_patch(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            entry = self.apply(
                path=arguments["path"],
                old_text=arguments.get("old_text", ""),
                new_text=arguments.get("new_text", ""),
            )
            return ToolResult(
                ok=True,
                content=f"已更新 {entry['path']}\nedit_id: {entry['edit_id']}\nafter_sha256: {entry['after_sha256']}",
                metadata={
                    "edit_id": entry["edit_id"],
                    "path": entry["path"],
                    "before_sha256": entry["before_sha256"],
                    "after_sha256": entry["after_sha256"],
                    "journal": entry["journal_artifact"],
                },
            )
        except (KeyError, TypeError, ValueError, OSError, UnicodeError, PathGuardError, EditError) as exc:
            return ToolResult(ok=False, error=f"文本 patch 被拒绝: {exc}")

    def apply(self, *, path: str, old_text: str, new_text: str) -> dict[str, Any]:
        if self.context.workspace_kind != "git_worktree" or self.context.active_root == self.context.source_root:
            raise EditError("写工具只允许操作 Session 的 Agent worktree，不能直接写 source")
        if not isinstance(path, str) or not isinstance(old_text, str) or not isinstance(new_text, str):
            raise TypeError("path、old_text 和 new_text 必须是字符串")
        if not path or "\x00" in path:
            raise EditError("path 不能为空或包含 NUL")
        raw = Path(path)
        if raw.is_absolute():
            raise EditError("path 必须是 active workspace 内相对路径")
        if len(old_text) + len(new_text) > self.MAX_PATCH_CHARS:
            raise EditError("patch 超过大小限制")

        target = self._resolve_non_symlink(raw)
        relative = target.relative_to(self.context.active_root).as_posix()
        existed = target.exists()
        if existed and not target.is_file():
            raise EditError("目标不是普通文件")
        if not existed and old_text:
            raise EditError("新增文件的 old_text 必须为空")
        if not target.parent.is_dir():
            raise EditError("父目录不存在；第一版不隐式创建目录")

        before_bytes = target.read_bytes() if existed else b""
        if b"\0" in before_bytes[:8192]:
            raise EditError("拒绝编辑二进制文件")
        try:
            before = before_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EditError("目标不是 UTF-8 文本文件") from exc
        if existed:
            if not old_text:
                raise EditError("修改已有文件必须提供非空 old_text 上下文")
            matched_old, replacement = _adapt_patch_newlines(before, old_text, new_text)
            matches = before.count(matched_old)
            if matches != 1:
                raise EditError(f"old_text 上下文必须且只能匹配一次，实际 {matches} 次")
            after = before.replace(matched_old, replacement, 1)
        else:
            after = new_text
        after_bytes = after.encode("utf-8")
        if len(after_bytes) > self.MAX_FILE_BYTES:
            raise EditError("编辑后的文件超过大小限制")
        if before_bytes == after_bytes:
            raise EditError("patch 未产生变化")

        edit_id = uuid.uuid4().hex
        patch = "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{relative}" if existed else "/dev/null",
                tofile=f"b/{relative}",
            )
        )
        target.write_bytes(after_bytes)
        entry = {
            "edit_id": edit_id,
            "path": relative,
            "before_sha256": hashlib.sha256(before_bytes).hexdigest(),
            "after_sha256": hashlib.sha256(after_bytes).hexdigest(),
            "patch": patch,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "session_id": self.session_store.session_id,
            "source_root": str(self.context.source_root),
            "active_root": str(self.context.active_root),
            "base_commit": self.context.base_commit,
        }
        self.journal_root.mkdir(parents=True, exist_ok=True)
        journal = self.journal_root / f"{edit_id}.json"
        entry["journal_artifact"] = str(journal)
        try:
            journal.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            if existed:
                target.write_bytes(before_bytes)
            else:
                target.unlink(missing_ok=True)
            raise
        self.session_store.append_event("edit_receipt", entry)
        entry["candidate_revision"] = self.session_store.next_candidate_revision()
        return entry

    def _resolve_non_symlink(self, relative: Path) -> Path:
        current = self.context.active_root
        for part in relative.parts:
            if part in {"", "."}:
                continue
            if part == "..":
                raise EditError("path 不能包含父目录跳转")
            current = current / part
            if current.is_symlink():
                raise EditError("拒绝通过 symlink 编辑")
        return self.guard.resolve(relative)


def _adapt_patch_newlines(before: str, old_text: str, new_text: str) -> tuple[str, str]:
    """让模型看到的通用 LF 上下文适配文件的统一行尾，同时保留原行尾风格。"""
    newline = _uniform_newline(before)
    if newline is None:
        return old_text, new_text

    def adapt(value: str) -> str:
        normalized = value.replace("\r\n", "\n").replace("\r", "\n")
        return normalized.replace("\n", newline)

    return adapt(old_text), adapt(new_text)


def _uniform_newline(value: str) -> str | None:
    without_crlf = value.replace("\r\n", "")
    has_crlf = "\r\n" in value
    has_lf = "\n" in without_crlf
    has_cr = "\r" in without_crlf
    styles = sum((has_crlf, has_lf, has_cr))
    if styles != 1:
        return None
    if has_crlf:
        return "\r\n"
    if has_lf:
        return "\n"
    return "\r"
