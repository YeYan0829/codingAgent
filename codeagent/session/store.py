from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codeagent.session.events import SessionEvent


class SessionStoreError(RuntimeError):
    pass


class SessionStore:
    def __init__(self, workspace_root: Path | str, session_id: str | None = None) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.base_dir = self.workspace_root / ".codeagent" / "sessions"
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.session_dir = self.base_dir / self.session_id
        self.meta_path = self.session_dir / "meta.json"
        self.events_path = self.session_dir / "events.jsonl"
        self.transcript_path = self.session_dir / "transcript.md"

    def create(self, *, mode: str = "readonly", model: str = "fake") -> "SessionStore":
        now = datetime.now(timezone.utc).isoformat()
        self.session_dir.mkdir(parents=True, exist_ok=False)
        meta = {
            "session_id": self.session_id,
            "workspace": str(self.workspace_root),
            "created_at": now,
            "last_active_at": now,
            "mode": mode,
            "model": model,
        }
        self.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        self.events_path.touch()
        self.transcript_path.write_text(f"# CodeAgent Session {self.session_id}\n\n", encoding="utf-8")
        self.append_event("session_created", {"session_id": self.session_id, "workspace": str(self.workspace_root)})
        return self

    def load(self) -> "SessionStore":
        if not self.meta_path.exists() or not self.events_path.exists():
            raise SessionStoreError(f"session not found: {self.session_id}")
        self.read_meta()
        self.read_events()
        return self

    def append_event(self, event_type: str, payload: dict[str, Any] | None = None) -> SessionEvent:
        event = SessionEvent(type=event_type, payload=payload or {})
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
    def list_sessions(cls, workspace_root: Path | str) -> list[dict[str, Any]]:
        base = Path(workspace_root).expanduser().resolve() / ".codeagent" / "sessions"
        if not base.exists():
            return []
        sessions = []
        for meta_path in sorted(base.glob("*/meta.json")):
            try:
                sessions.append(json.loads(meta_path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                sessions.append({"session_id": meta_path.parent.name, "error": "corrupted meta.json"})
        return sessions

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
            "cli_command": "CLI Command",
            "error": "Error",
        }
        label = labels.get(event.type)
        if not label:
            return
        content = event.payload.get("message") or event.payload.get("command") or event.payload.get("content") or json.dumps(event.payload, ensure_ascii=False)
        with self.transcript_path.open("a", encoding="utf-8") as fh:
            fh.write(f"## {label}\n\n{content}\n\n")
