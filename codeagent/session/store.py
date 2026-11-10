from __future__ import annotations

from functools import wraps

import hashlib
import fcntl
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


def _session_locked(method):
    """主状态变更与子事件追加共享元数据临界区，避免覆盖旧 frontier/workspace。"""
    @wraps(method)
    def locked(self, *args, **kwargs):
        with _store_lock(self.meta_path):
            return method(self, *args, **kwargs)
    return locked


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
        from dataclasses import replace
        from codeagent.safety.repository_access import RepositoryAccessStore
        access = RepositoryAccessStore(self.workspace_root, self.session_root).read()
        context = replace(WorkspaceContext.source(self.workspace_root), denied_paths=tuple(access["deniedPaths"]))
        state = {
            "repository_access_revision": access["revision"],
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
            "workspace_state": SessionWorkspaceState.CURRENT_PROJECT.value,
            "workspace_mode": "direct",
            "workspace_state_reason": "",
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

    @_session_locked
    def open_current_workspace(self) -> None:
        """打开当前同项目目录；旧worktree及其未采纳修改从不拷回项目。"""
        if not self.workspace_root.is_dir():
            raise SessionStoreError("当前项目目录不存在；会话历史仍可读取")
        meta = self.read_meta()
        if meta.get("workspace_mode") == "direct":
            return
        legacy = {key: meta.get(key) for key in ("active_workspace", "workspace_kind", "workspace_state")}
        for key in ("base_commit", "source_head", "snapshot_ref", "workspace_revision", "pending_workspace_revision",
                    "candidate_revision", "accepted_candidate_revision"):
            meta.pop(key, None)
        meta.update(WorkspaceContext.source(self.workspace_root).to_metadata())
        meta["denied_paths"] = self.read_meta().get("denied_paths", [])
        meta.update(workspace_mode="direct", workspace_state=SessionWorkspaceState.CURRENT_PROJECT.value, workspace_state_reason="")
        self._write_state(meta)
        self.append_event("legacy_workspace_detached", legacy)

    def update_runtime_options(self, options: dict[str, Any]) -> None:
        from codeagent.config import RuntimeConfig
        RuntimeConfig.from_options(options)
        with _store_lock(self.meta_path):
            meta = self.read_meta()
            meta["runtime_options"] = {**meta.get("runtime_options", {}), **options}
            self._write_state(meta)

    def workspace_state(self) -> SessionWorkspaceState:
        try:
            return SessionWorkspaceState(self.read_meta()["workspace_state"])
        except (KeyError, ValueError) as exc:
            raise SessionStoreError("session workspace_state 无效") from exc


    def permission_grants(self):
        from codeagent.runtime.permissions import PermissionGrant
        return tuple(PermissionGrant.from_dict(item) for item in self.read_meta().get("permission_grants", []))

    @_session_locked
    def add_permission_grants(self, grants) -> None:
        meta = self.read_meta()
        existing = list(meta.get("permission_grants", []))
        existing.extend(grant.to_dict() for grant in grants)
        meta["permission_grants"] = existing
        self._write_state(meta)


    @_session_locked
    def mark_recovery_required(self, reason: str) -> None:
        meta = self.read_meta()
        meta["workspace_state"] = SessionWorkspaceState.RECOVERY_REQUIRED.value
        meta["workspace_state_reason"] = reason
        self._write_state(meta)
        self.append_event("recovery_required", {"reason": reason})

    @_session_locked
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

    def append_derived_events(
        self,
        events: list[tuple[str, dict[str, Any]]],
        *,
        turn_id: str | None,
    ) -> list[SessionEvent]:
        """在一个 store critical section 中追加不属于主 ModelStep 的派生事件。

        该路径只推进 Event seq/last_active_at，不读取或改写 current turn/model-step identity。
        """
        if not events:
            return []
        with _store_lock(self.meta_path):
            meta = self.read_meta()
            current_seq = int(meta.get("event_seq", 0))
            if not current_seq and self.events_path.exists():
                current_seq = max(
                    (event.seq or index for index, event in enumerate(self.read_events(), 1)), default=0,
                )
            appended: list[SessionEvent] = []
            seq = current_seq
            for event_type, payload in events:
                seq += 1
                appended.append(SessionEvent(
                    seq=seq,
                    turn_id=turn_id,
                    model_step_id=None,
                    type=event_type,
                    payload=payload,
                ))
            with self.events_path.open("a", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                for event in appended:
                    fh.write(event.model_dump_json() + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            meta["event_seq"] = seq
            meta["last_active_at"] = appended[-1].ts
            self._write_state(meta)
            return appended

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
        elif event_type in {
            "assistant_tool_calls", "assistant_message", "model_protocol_error", "model_output_truncated",
        }:
            model_step_id = f"step-{seq}"
        event = SessionEvent(seq=seq, turn_id=turn_id, model_step_id=model_step_id, type=event_type, payload=payload or {})
        with self.events_path.open("a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            fh.write(event.model_dump_json() + "\n")
            fh.flush()
            os.fsync(fh.fileno())
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

    def reconcile_event_frontier(self) -> None:
        """仅持有进程执行权时调用。隔离未完成尾行，以完整事件恢复落后的 meta。"""
        with _store_lock(self.meta_path):
            with self.events_path.open("r+b") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                raw = fh.read()
                if raw and not raw.endswith(b"\n"):
                    end = raw.rfind(b"\n") + 1
                    tail = self.session_dir / ("events-tail-" + uuid.uuid4().hex + ".bin")
                    tail.write_bytes(raw[end:])
                    fh.truncate(end)
                    fh.flush()
                    os.fsync(fh.fileno())
            events = self.read_events()
            seqs = [e.seq or i for i, e in enumerate(events, 1)]
            if seqs != list(range(1, len(events) + 1)):
                raise SessionStoreError("invalid graph event sequence")
            meta = self.read_meta()
            last = events[-1] if events else None
            cursor = seqs[-1] if last else 0
            if int(meta.get("event_seq", 0)) > cursor:
                raise SessionStoreError("event log is behind metadata")
            meta["event_seq"] = cursor
            if last:
                meta["last_active_at"] = last.ts
            users = [e for e in events if e.type == "user_message"]
            if users:
                meta["current_turn_id"] = users[-1].turn_id
            steps = [e for e in events if e.turn_id == meta.get("current_turn_id") and e.model_step_id]
            meta["current_model_step_id"] = steps[-1].model_step_id if steps else None
            self._write_state(meta)

    def read_meta(self) -> dict[str, Any]:
        try:
            value = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SessionStoreError(f"session.json is corrupted: {exc}") from exc
        if value.get("schema_version") not in {2, self.SCHEMA_VERSION}:
            raise SessionStoreError("unsupported session schema")
        return value

    def read_events(self) -> list[SessionEvent]:
        # RLock 保护同进程线程；文件共享锁还需与另一进程的追加/尾部恢复互斥。
        with _store_lock(self.meta_path):
            with self.events_path.open("r", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
                content = fh.read()
        events: list[SessionEvent] = []
        for line_no, line in enumerate(content.splitlines(), 1):
            if not line.strip():
                continue
            try:
                event = SessionEvent.model_validate_json(line)
                # 只读兼容旧框架的执行日志，原始文件不迁移、不重写。
                names = {"graph_execution_acquired": "execution_acquired", "graph_model_intent": "model_request_intent", "graph_model_response": "model_response",
                    "graph_model_undispatched": "model_request_undispatched", "graph_tool_intent": "tool_execution_intent",
                    "graph_tool_result": "tool_execution_result", "graph_truncation_projection": "output_truncation_projection",
                    "execution_budget_started": "request_executor_started"}
                payload = event.payload
                if "_graph_operation" in payload:
                    payload = {**payload, "_execution_operation": payload["_graph_operation"]}
                events.append(event.model_copy(update={"type": names.get(event.type, event.type), "payload": payload}))
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

    @_session_locked
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
