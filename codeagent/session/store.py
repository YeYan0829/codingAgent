from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codeagent.config import default_session_root
from codeagent.session.events import SessionEvent
from codeagent.workspace.workspace import SessionWorkspaceState, WorkspaceContext


class SessionStoreError(RuntimeError):
    pass


_LOCKS_GUARD = threading.Lock()
_STORE_LOCKS: dict[str, threading.RLock] = {}


def _store_lock(path: Path) -> threading.RLock:
    key = str(path)
    with _LOCKS_GUARD:
        return _STORE_LOCKS.setdefault(key, threading.RLock())


class SessionStore:
    """Session 的唯一业务状态与 append-only 关键历史。"""

    UNTITLED = "Untitled session"
    SCHEMA_VERSION = 3

    def __init__(self, workspace_root: Path | str, session_id: str | None = None, session_root: Path | str | None = None) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.session_root = Path(session_root).expanduser().resolve() if session_root is not None else default_session_root().expanduser().resolve()
        self.base_dir = self.session_root / self.workspace_key(self.workspace_root)
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.session_dir = self.base_dir / self.session_id
        self.meta_path = self.session_dir / "session.json"
        self.events_path = self.session_dir / "events.jsonl"
        self.diagnostics_dir = self.session_dir / "diagnostics"

    def create(
        self,
        *,
        provider: str = "fake",
        model: str = "fake",
        title: str | None = None,
        model_options: dict[str, Any] | None = None,
        runtime_options: dict[str, Any] | None = None,
    ) -> "SessionStore":
        now = _now()
        self.session_dir.mkdir(parents=True, exist_ok=False)
        context = WorkspaceContext.source(self.workspace_root)
        state = {
            "schema_version": self.SCHEMA_VERSION,
            "session_id": self.session_id,
            "title": title or self.UNTITLED,
            "workspace": str(self.workspace_root),
            "session_root": str(self.session_root),
            "workspace_key": self.workspace_key(self.workspace_root),
            "created_at": now,
            "last_active_at": now,
            "provider": provider,
            "model": model,
            "model_options": model_options or {},
            "runtime_options": runtime_options or {},
            "workspace_state": SessionWorkspaceState.SOURCE_ONLY.value,
            "workspace_state_reason": "",
            "candidate_revision": 0,
            "accepted_candidate_revision": 0,
            **context.to_metadata(),
        }
        self._write_state(state)
        self.events_path.touch()
        self.append_event("session_created", {"session_id": self.session_id, "title": state["title"], "workspace": str(self.workspace_root)})
        return self

    def load(self) -> "SessionStore":
        if not self.meta_path.exists() or not self.events_path.exists():
            raise SessionStoreError(f"session not found: {self.session_id}")
        self.read_meta()
        self.read_events()
        return self

    def workspace_context(self) -> WorkspaceContext:
        return WorkspaceContext.from_metadata(self.read_meta())

    def workspace_state(self) -> SessionWorkspaceState:
        try:
            return SessionWorkspaceState(self.read_meta()["workspace_state"])
        except (KeyError, ValueError) as exc:
            raise SessionStoreError("session workspace_state 无效") from exc

    def begin_workspace_upgrade(self, revision: int) -> None:
        meta = self.read_meta()
        if meta.get("workspace_state") != SessionWorkspaceState.SOURCE_ONLY.value:
            raise SessionStoreError("只有 source_only Session 可以创建 Agent worktree")
        meta["workspace_state"] = SessionWorkspaceState.PREPARING_WORKSPACE.value
        meta["pending_workspace_revision"] = revision
        meta["workspace_state_reason"] = "正在创建 Agent worktree"
        self._write_state(meta)
        self.append_event("workspace_preparing", {"workspace_revision": revision})

    def activate_workspace(self, context: WorkspaceContext) -> None:
        if context.source_root != self.workspace_root or context.workspace_kind != "git_worktree":
            raise SessionStoreError("active workspace identity 无效")
        meta = self.read_meta()
        if meta.get("workspace_state") != SessionWorkspaceState.PREPARING_WORKSPACE.value:
            raise SessionStoreError("Session 不在 preparing_workspace")
        meta.update(context.to_metadata())
        meta.pop("pending_workspace_revision", None)
        meta["workspace_state"] = SessionWorkspaceState.CHANGES_ACTIVE.value
        meta["workspace_state_reason"] = ""
        self._write_state(meta)
        self.append_event("workspace_activated", {
            "workspace_revision": context.workspace_revision,
            "base_commit": context.base_commit,
            "active_workspace": str(context.active_root),
            "baseline_workspace": str(context.source_root),
        })

    def cancel_workspace_upgrade(self, reason: str) -> None:
        meta = self.read_meta()
        meta.update(WorkspaceContext.source(self.workspace_root).to_metadata())
        meta.pop("pending_workspace_revision", None)
        meta["workspace_state"] = SessionWorkspaceState.SOURCE_ONLY.value
        meta["workspace_state_reason"] = reason
        self._write_state(meta)
        self.append_event("workspace_upgrade_failed", {"reason": reason})

    def begin_resolution(self, state: SessionWorkspaceState) -> None:
        if state not in {SessionWorkspaceState.ACCEPTING, SessionWorkspaceState.DISCARDING}:
            raise SessionStoreError("无效的修改处理状态")
        meta = self.read_meta()
        allowed = {SessionWorkspaceState.CHANGES_ACTIVE.value}
        if state == SessionWorkspaceState.DISCARDING:
            allowed.add(SessionWorkspaceState.WORKSPACE_TAINTED.value)
        if meta.get("workspace_state") not in allowed:
            raise SessionStoreError("当前没有可处理的修改")
        meta["workspace_state"] = state.value
        self._write_state(meta)

    def finish_resolution(self, result: str, summary: dict[str, Any]) -> None:
        meta = self.read_meta()
        revision = int(meta.get("workspace_revision", 0))
        meta.update(WorkspaceContext.source(self.workspace_root).to_metadata())
        meta["workspace_revision"] = revision
        meta["workspace_state"] = SessionWorkspaceState.SOURCE_ONLY.value
        meta["workspace_state_reason"] = ""
        self._write_state(meta)
        self.append_event(f"changes_{result}", {"workspace_revision": revision, **summary})

    def next_candidate_revision(self) -> int:
        meta = self.read_meta()
        revision = int(meta.get("candidate_revision", 0)) + 1
        meta["candidate_revision"] = revision
        self._write_state(meta)
        return revision

    def permission_grants(self):
        from codeagent.runtime.permissions import PermissionGrant
        return tuple(PermissionGrant.from_dict(item) for item in self.read_meta().get("permission_grants", []))

    def add_permission_grants(self, grants) -> None:
        meta = self.read_meta()
        existing = list(meta.get("permission_grants", []))
        existing.extend(grant.to_dict() for grant in grants)
        meta["permission_grants"] = existing
        self._write_state(meta)

    def finish_accept(self, context: WorkspaceContext, *, candidate_revision: int, patch_sha256: str, changed_files: list[str]) -> None:
        meta = self.read_meta()
        meta.update(context.to_metadata())
        meta["accepted_candidate_revision"] = candidate_revision
        meta["workspace_state"] = SessionWorkspaceState.CHANGES_ACTIVE.value
        meta["workspace_state_reason"] = ""
        self._write_state(meta)
        self.append_event("changes_accepted", {
            "workspace_revision": context.workspace_revision,
            "candidate_revision": candidate_revision,
            "baseline_commit": context.base_commit,
            "patch_sha256": patch_sha256,
            "changed_files": changed_files,
        })

    def finish_discard(self, *, candidate_revision: int) -> None:
        meta = self.read_meta()
        meta["accepted_candidate_revision"] = candidate_revision
        meta["workspace_state"] = SessionWorkspaceState.CHANGES_ACTIVE.value
        meta["workspace_state_reason"] = ""
        self._write_state(meta)
        self.append_event("changes_discarded", {
            "workspace_revision": int(meta.get("workspace_revision", 0)),
            "candidate_revision": candidate_revision,
        })

    def restore_changes_active(self, reason: str = "") -> None:
        meta = self.read_meta()
        meta["workspace_state"] = SessionWorkspaceState.CHANGES_ACTIVE.value
        meta["workspace_state_reason"] = reason
        self._write_state(meta)

    def mark_workspace_tainted(self, reason: str, changed_files: list[str]) -> None:
        meta = self.read_meta()
        meta["workspace_state"] = SessionWorkspaceState.WORKSPACE_TAINTED.value
        meta["workspace_state_reason"] = reason
        self._write_state(meta)
        self.append_event("workspace_tainted", {"reason": reason, "changed_files": changed_files[:100]})

    def mark_recovery_required(self, reason: str) -> None:
        meta = self.read_meta()
        meta["workspace_state"] = SessionWorkspaceState.RECOVERY_REQUIRED.value
        meta["workspace_state_reason"] = reason
        self._write_state(meta)
        self.append_event("recovery_required", {"reason": reason})

    def update_workspace_state(self, state: str, reason: str = "") -> None:
        if state == SessionWorkspaceState.RECOVERY_REQUIRED.value:
            self.mark_recovery_required(reason)
            return
        meta = self.read_meta()
        meta["workspace_state"] = state
        meta["workspace_state_reason"] = reason
        self._write_state(meta)

    def append_event(self, event_type: str, payload: dict[str, Any] | None = None) -> SessionEvent:
        with _store_lock(self.meta_path):
            return self._append_event(event_type, payload)

    def _append_event(self, event_type: str, payload: dict[str, Any] | None = None) -> SessionEvent:
        meta = self.read_meta()
        if "event_seq" in meta:
            seq = int(meta["event_seq"]) + 1
        else:
            existing = self.read_events() if self.events_path.exists() else []
            seq = max((event.seq or index for index, event in enumerate(existing, 1)), default=0) + 1
        turn_id = meta.get("current_turn_id")
        model_step_id = meta.get("current_model_step_id")
        if event_type == "user_message":
            turn_id = f"turn-{seq}"
            model_step_id = None
        elif event_type in {"assistant_tool_calls", "assistant_message", "model_protocol_error"}:
            model_step_id = f"step-{seq}"
        event = SessionEvent(seq=seq, turn_id=turn_id, model_step_id=model_step_id, type=event_type, payload=payload or {})
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(event.model_dump_json() + "\n")
        if event.type == "user_message" and meta.get("title") == self.UNTITLED:
            title = self.title_from_message(str(event.payload.get("message", "")))
            if title != self.UNTITLED:
                meta["title"] = title
        meta["event_seq"] = seq
        meta["current_turn_id"] = turn_id
        meta["current_model_step_id"] = model_step_id
        meta["last_active_at"] = event.ts
        self._write_state(meta)
        return event

    def read_meta(self) -> dict[str, Any]:
        try:
            value = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SessionStoreError(f"session.json is corrupted: {exc}") from exc
        if value.get("schema_version") not in {2, self.SCHEMA_VERSION}:
            raise SessionStoreError("unsupported session schema")
        return value

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
    def list_sessions(cls, workspace_root: Path | str, session_root: Path | str | None = None, **_: Any) -> list[dict[str, Any]]:
        workspace = Path(workspace_root).expanduser().resolve()
        root = Path(session_root).expanduser().resolve() if session_root is not None else default_session_root().expanduser().resolve()
        return cls._read_session_states(root / cls.workspace_key(workspace))

    @classmethod
    def list_all_sessions(cls, session_root: Path | str | None = None) -> list[dict[str, Any]]:
        root = Path(session_root).expanduser().resolve() if session_root is not None else default_session_root().expanduser().resolve()
        states = []
        for path in sorted(root.glob("*/*/session.json")) if root.exists() else []:
            try:
                states.append(json.loads(path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                states.append({"session_id": path.parent.name, "error": "corrupted session.json"})
        states.sort(key=lambda item: item.get("last_active_at", ""), reverse=True)
        return states

    @classmethod
    def find_session(cls, session_id: str, session_root: Path | str | None = None) -> dict[str, Any] | None:
        return next((item for item in cls.list_all_sessions(session_root) if item.get("session_id") == session_id), None)

    @staticmethod
    def workspace_key(workspace_root: Path | str) -> str:
        resolved = str(Path(workspace_root).expanduser().resolve())
        digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:10]
        name = re.sub(r"[^A-Za-z0-9_.-]+", "-", Path(resolved).name).strip("-") or "workspace"
        return f"{name}-{digest}"

    @classmethod
    def title_from_message(cls, message: str, limit: int = 48) -> str:
        title = re.sub(r"^请[你 ]*", "", " ".join(str(message).split()))
        if not title:
            return cls.UNTITLED
        return title if len(title) <= limit else title[: limit - 3].rstrip() + "..."

    @staticmethod
    def _read_session_states(base: Path) -> list[dict[str, Any]]:
        states = []
        if base.exists():
            for path in sorted(base.glob("*/session.json")):
                try:
                    states.append(json.loads(path.read_text(encoding="utf-8")))
                except json.JSONDecodeError:
                    states.append({"session_id": path.parent.name, "error": "corrupted session.json"})
        states.sort(key=lambda item: item.get("last_active_at", ""), reverse=True)
        return states

    def _maybe_set_title(self, message: str) -> None:
        meta = self.read_meta()
        if meta.get("title") != self.UNTITLED:
            return
        title = self.title_from_message(message)
        if title != self.UNTITLED:
            meta["title"] = title
            self._write_state(meta)

    def _write_state(self, value: dict[str, Any]) -> None:
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.meta_path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.meta_path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
