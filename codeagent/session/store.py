from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codeagent.config import default_session_root
from codeagent.session.events import SessionEvent
from codeagent.workspace.workspace import WorkspaceContext


class SessionStoreError(RuntimeError):
    pass


class SessionStore:
    UNTITLED = "Untitled session"

    def __init__(self, workspace_root: Path | str, session_id: str | None = None, session_root: Path | str | None = None) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.session_root = Path(session_root).expanduser().resolve() if session_root is not None else default_session_root().expanduser().resolve()
        self.base_dir = self.session_root / self.workspace_key(self.workspace_root)
        self.legacy_base_dir = self.workspace_root / ".codeagent" / "sessions"
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.session_dir = self.base_dir / self.session_id
        self.meta_path = self.session_dir / "meta.json"
        self.events_path = self.session_dir / "events.jsonl"
        self.transcript_path = self.session_dir / "transcript.md"

    def create(self, *, mode: str = "readonly", provider: str = "fake", model: str = "fake", title: str | None = None, workspace_context: WorkspaceContext | None = None) -> "SessionStore":
        now = datetime.now(timezone.utc).isoformat()
        self.session_dir.mkdir(parents=True, exist_ok=False)
        session_title = title or self.UNTITLED
        context = workspace_context or WorkspaceContext.source(self.workspace_root)
        if context.source_root != self.workspace_root:
            raise SessionStoreError("workspace context 的 source_root 与 session workspace 不一致")
        meta = {
            "session_id": self.session_id,
            "title": session_title,
            "workspace": str(self.workspace_root),
            "session_root": str(self.session_root),
            "workspace_key": self.workspace_key(self.workspace_root),
            "created_at": now,
            "last_active_at": now,
            "mode": mode,
            "provider": provider,
            "model": model,
            **context.to_metadata(),
            "worktree_lifecycle_state": "active" if context.workspace_kind == "git_worktree" else "not_applicable",
        }
        self.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        self.events_path.touch()
        self.transcript_path.write_text(f"# {session_title}\n\nSession: {self.session_id}\nWorkspace: {self.workspace_root}\n\n", encoding="utf-8")
        self.append_event("session_created", {"session_id": self.session_id, "title": session_title, "workspace": str(self.workspace_root)})
        return self

    def workspace_context(self) -> WorkspaceContext:
        """从新 metadata 恢复 context；旧 session 自动退化为 source context。"""
        return WorkspaceContext.from_metadata(self.read_meta())

    def update_workspace_context(self, context: WorkspaceContext, lifecycle_state: str = "active") -> None:
        if context.source_root != self.workspace_root:
            raise SessionStoreError("workspace context 的 source_root 与 session workspace 不一致")
        meta = self.read_meta()
        meta.update(context.to_metadata())
        meta["worktree_lifecycle_state"] = lifecycle_state
        self.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def update_worktree_lifecycle(self, lifecycle_state: str) -> None:
        meta = self.read_meta()
        meta["worktree_lifecycle_state"] = lifecycle_state
        self.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def update_workspace_state(self, state: str, reason: str = "") -> None:
        meta = self.read_meta()
        meta["workspace_state"] = state
        meta["workspace_state_reason"] = reason
        self.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self) -> "SessionStore":
        if not self.meta_path.exists() and (self.legacy_base_dir / self.session_id / "meta.json").exists():
            self.session_dir = self.legacy_base_dir / self.session_id
            self.meta_path = self.session_dir / "meta.json"
            self.events_path = self.session_dir / "events.jsonl"
            self.transcript_path = self.session_dir / "transcript.md"
        if not self.meta_path.exists() or not self.events_path.exists():
            raise SessionStoreError(f"session not found: {self.session_id}")
        self.read_meta()
        self.read_events()
        return self

    def append_event(self, event_type: str, payload: dict[str, Any] | None = None) -> SessionEvent:
        event = SessionEvent(type=event_type, payload=payload or {})
        if event.type == "user_message":
            self._maybe_set_title(event.payload.get("message", ""))
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(event.model_dump_json() + "\n")
        self._append_transcript(event)
        self._touch_meta(event.ts)
        return event

    def read_meta(self) -> dict[str, Any]:
        try:
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SessionStoreError(f"meta.json is corrupted: {exc}") from exc

    def read_events(self) -> list[SessionEvent]:
        events: list[SessionEvent] = []
        for line_no, line in enumerate(self.events_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                events.append(SessionEvent.model_validate_json(line))
            except Exception as exc:
                raise SessionStoreError(f"events.jsonl is corrupted at line {line_no}: {exc}") from exc
        return events

    @classmethod
    def list_sessions(cls, workspace_root: Path | str, session_root: Path | str | None = None, *, include_legacy: bool = True) -> list[dict[str, Any]]:
        workspace = Path(workspace_root).expanduser().resolve()
        root = Path(session_root).expanduser().resolve() if session_root is not None else default_session_root().expanduser().resolve()
        bases = [root / cls.workspace_key(workspace)]
        legacy = workspace / ".codeagent" / "sessions"
        if include_legacy and legacy not in bases:
            bases.append(legacy)
        sessions = []
        for base in bases:
            if not base.exists():
                continue
            sessions.extend(cls._read_session_metas(base))
        sessions.sort(key=lambda meta: meta.get("last_active_at", ""), reverse=True)
        return sessions

    @classmethod
    def list_all_sessions(cls, session_root: Path | str | None = None) -> list[dict[str, Any]]:
        root = Path(session_root).expanduser().resolve() if session_root is not None else default_session_root().expanduser().resolve()
        if not root.exists():
            return []
        sessions = []
        for meta_path in sorted(root.glob("*/*/meta.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                meta.setdefault("session_root", str(root))
                meta.setdefault("workspace_key", meta_path.parent.parent.name)
                sessions.append(meta)
            except json.JSONDecodeError:
                sessions.append({"session_id": meta_path.parent.name, "error": "corrupted meta.json"})
        sessions.sort(key=lambda meta: meta.get("last_active_at", ""), reverse=True)
        return sessions

    @classmethod
    def find_session(cls, session_id: str, session_root: Path | str | None = None) -> dict[str, Any] | None:
        for meta in cls.list_all_sessions(session_root=session_root):
            if meta.get("session_id") == session_id:
                return meta
        return None

    @staticmethod
    def workspace_key(workspace_root: Path | str) -> str:
        resolved = str(Path(workspace_root).expanduser().resolve())
        digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:10]
        name = re.sub(r"[^A-Za-z0-9_.-]+", "-", Path(resolved).name).strip("-") or "workspace"
        return f"{name}-{digest}"

    @classmethod
    def title_from_message(cls, message: str, limit: int = 48) -> str:
        title = " ".join(str(message).split())
        title = re.sub(r"^请[你 ]*", "", title)
        if not title:
            return cls.UNTITLED
        if len(title) > limit:
            title = title[: limit - 3].rstrip() + "..."
        return title

    @staticmethod
    def _read_session_metas(base: Path) -> list[dict[str, Any]]:
        sessions = []
        for meta_path in sorted(base.glob("*/meta.json")):
            try:
                sessions.append(json.loads(meta_path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                sessions.append({"session_id": meta_path.parent.name, "error": "corrupted meta.json"})
        return sessions

    def _maybe_set_title(self, message: str) -> None:
        if not self.meta_path.exists():
            return
        meta = self.read_meta()
        if meta.get("title") and meta.get("title") != self.UNTITLED:
            return
        title = self.title_from_message(message)
        if title == self.UNTITLED:
            return
        meta["title"] = title
        self.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        self._rewrite_transcript_title(title)

    def _rewrite_transcript_title(self, title: str) -> None:
        if not self.transcript_path.exists():
            return
        lines = self.transcript_path.read_text(encoding="utf-8").splitlines()
        if lines and lines[0].startswith("# "):
            lines[0] = f"# {title}"
            self.transcript_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _touch_meta(self, ts: str) -> None:
        if not self.meta_path.exists():
            return
        meta = self.read_meta()
        meta["last_active_at"] = ts
        self.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def _append_transcript(self, event: SessionEvent) -> None:
        labels = {
            "user_message": "User",
            "assistant_message": "Assistant",
            "tool_requested": "Tool Requested",
            "tool_result": "Tool Result",
            "tool_denied": "Tool Denied",
            "approval_requested": "Approval Requested",
            "approval_decision": "Approval Decision",
            "assistant_tool_calls": "Assistant Tool Calls",
            "cli_command": "CLI Command",
            "error": "Error",
        }
        label = labels.get(event.type)
        if not label:
            return
        content = event.payload.get("message") or event.payload.get("command") or event.payload.get("content") or json.dumps(event.payload, ensure_ascii=False)
        with self.transcript_path.open("a", encoding="utf-8") as fh:
            fh.write(f"## {label}\n\n{content}\n\n")
