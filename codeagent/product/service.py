from __future__ import annotations

from pathlib import Path
from typing import Any

from codeagent.config import ModelConfig
from codeagent.product.execution import ExecutionConflictError, ExecutionSupervisor
from codeagent.product.read_model import activity_item, build_session_detail, build_session_summary
from codeagent.runtime.candidate import CandidateError
from codeagent.runtime.current_changes import CurrentChangesError, CurrentChangesService
from codeagent.safety.path_guard import PathGuard, PathGuardError
from codeagent.session.store import SessionStore, SessionStoreError


class ProductServiceError(RuntimeError):
    pass


class ProductApplicationService:
    """产品应用服务：读取 Session，并把执行交给进程内监管器。"""

    def __init__(self, session_root: Path | str | None = None, *, supervisor: ExecutionSupervisor | None = None) -> None:
        self.session_root = Path(session_root).expanduser().resolve() if session_root is not None else None
        self.supervisor = supervisor or ExecutionSupervisor()

    def list_sessions(self, workspace: Path | str | None = None) -> list[dict[str, Any]]:
        metas = (
            SessionStore.list_sessions(workspace, session_root=self.session_root)
            if workspace is not None
            else SessionStore.list_all_sessions(session_root=self.session_root)
        )
        summaries = []
        for meta in metas:
            try:
                summary = build_session_summary(meta)
                state = self.supervisor.state(summary["sessionId"])
                summary["executionState"] = state
                if state == "awaiting_approval":
                    summary["attentionSummary"] = "Approval required"
                elif state == "stopping":
                    summary["attentionSummary"] = "Stopping"
                elif state == "running":
                    summary["attentionSummary"] = "Running"
                summaries.append(summary)
            except (KeyError, OSError, SessionStoreError, ValueError) as exc:
                summaries.append({
                    "sessionId": str(meta.get("session_id", "unknown")),
                    "title": "Unreadable session",
                    "workspace": str(meta.get("workspace", "")),
                    "workspaceLabel": Path(str(meta.get("workspace", ""))).name,
                    "executionState": "idle",
                    "attentionSummary": f"Session data needs attention: {exc}",
                    "lastActiveAt": meta.get("last_active_at"),
                    "changedFileCount": 0,
                })
        return summaries

    def get_session(self, session_id: str) -> dict[str, Any]:
        store = self._load(session_id)
        detail = build_session_detail(store)
        detail["executionState"] = self.supervisor.state(session_id)
        detail["pendingApproval"] = self.supervisor.pending_approval(session_id)
        detail["availableActions"]["canRun"] &= detail["executionState"] == "idle"
        detail["availableActions"]["canContinue"] &= detail["executionState"] == "idle"
        detail["availableActions"]["canIncreaseBudget"] &= detail["executionState"] == "idle"
        detail["availableActions"]["canStop"] = (
            detail["executionState"] in {"running", "awaiting_approval"}
            or (detail["executionState"] == "idle" and detail["availableActions"]["canStop"])
        )
        detail["availableActions"]["canResolveApproval"] = detail["pendingApproval"] is not None
        if detail["executionState"] != "idle":
            detail["availableActions"]["canAcceptChanges"] = False
            detail["availableActions"]["canDiscardChanges"] = False
            detail["actionReasons"]["canAcceptChanges"] = "Wait for the current execution to stop before applying changes."
            detail["actionReasons"]["canDiscardChanges"] = "Wait for the current execution to stop before discarding changes."
        if detail["pendingApproval"] is not None:
            detail["attentionSummary"] = "Approval required"
        elif detail["executionState"] == "idle":
            interrupted = self._interrupted_control(store)
            detail["interruptedControl"] = interrupted
            if interrupted:
                detail["attentionSummary"] = interrupted
                detail["globalAttention"] = {
                    "kind": "interrupted", "title": "Previous execution was interrupted",
                    "message": interrupted,
                    "action": "No permission was preserved. Review the session and retry only if needed.",
                }
        return detail

    def create_session(
        self, workspace: Path | str, *, provider: str = "fake", model: str | None = None, title: str | None = None,
        temperature: float | None = None, max_tokens: int | None = None,
        max_steps_per_turn: int = 12, max_model_steps_per_user_turn: int = 48,
    ) -> dict[str, Any]:
        root = Path(workspace).expanduser().resolve()
        if not root.is_dir():
            raise ProductServiceError(f"workspace not found: {root}")
        config = ModelConfig(provider=provider, model=model)
        if provider not in {"fake", "deepseek", "glm"}:
            raise ProductServiceError(f"unknown provider: {provider}")
        if temperature is not None and not 0 <= temperature <= 2:
            raise ProductServiceError("temperature must be between 0 and 2")
        if max_tokens is not None and not 1 <= max_tokens <= 131_072:
            raise ProductServiceError("maxTokens must be between 1 and 131072")
        if not 1 <= max_steps_per_turn <= 100:
            raise ProductServiceError("maxStepsPerTurn must be between 1 and 100")
        if not max_steps_per_turn <= max_model_steps_per_user_turn <= 1000:
            raise ProductServiceError("maxModelStepsPerUserTurn must be between maxStepsPerTurn and 1000")
        store = SessionStore(root, session_root=self.session_root).create(
            provider=config.provider, model=config.resolved_model, title=title,
            model_options={"temperature": temperature, "max_tokens": max_tokens},
            runtime_options={
                "max_steps_per_turn": max_steps_per_turn,
                "max_model_steps_per_user_turn": max_model_steps_per_user_turn,
            },
        )
        return self.get_session(store.session_id)

    def run_session(self, session_id: str, message: str) -> dict[str, Any]:
        if not isinstance(message, str) or not message.strip() or len(message) > 20_000:
            raise ProductServiceError("message must be a non-empty string of at most 20000 characters")
        try:
            return self.supervisor.start(self._load(session_id), message=message.strip())
        except ExecutionConflictError as exc:
            raise ProductServiceError(str(exc)) from exc

    def continue_session(self, session_id: str) -> dict[str, Any]:
        detail = self.get_session(session_id)
        if not detail["availableActions"]["canContinue"]:
            raise ProductServiceError("current UserTurn cannot continue")
        try:
            return self.supervisor.start(self._load(session_id), continuation=True)
        except ExecutionConflictError as exc:
            raise ProductServiceError(str(exc)) from exc

    def stop_session(self, session_id: str) -> dict[str, Any]:
        return self.supervisor.stop(self._load(session_id))

    def increase_budget(
        self,
        session_id: str,
        *,
        turn_id: str,
        expected_limit: int,
        new_limit: int | None = None,
        additional_steps: int | None = None,
    ) -> dict[str, Any]:
        try:
            return self.supervisor.start(self._load(session_id), continuation=True, budget_increase={
                "turn_id": turn_id, "expected_limit": expected_limit,
                "new_limit": new_limit, "additional_steps": additional_steps,
            })
        except ExecutionConflictError as exc:
            raise ProductServiceError(str(exc)) from exc

    def resolve_approval(self, session_id: str, approval_id: str, allow: bool) -> dict[str, Any]:
        if not isinstance(allow, bool):
            raise ProductServiceError("allow must be a boolean")
        try:
            return self.supervisor.resolve_approval(session_id, approval_id, allow)
        except ExecutionConflictError as exc:
            raise ProductServiceError(str(exc)) from exc

    def get_changes(self, session_id: str) -> dict[str, Any]:
        detail = self.get_session(session_id)
        store = self._load(session_id)
        stats = self._change_stats(store, detail["changesSummary"]["files"])
        return {
            **detail["changesSummary"],
            **stats,
            "validation": detail["validationSummary"],
            "canAccept": detail["availableActions"]["canAcceptChanges"],
            "canDiscard": detail["availableActions"]["canDiscardChanges"],
        }

    def get_change_file(self, session_id: str, path: str) -> dict[str, Any]:
        store = self._load(session_id)
        detail = self.get_session(session_id)
        if path not in detail["changesSummary"]["files"]:
            raise ProductServiceError("path is not a current pending change")
        context = store.workspace_context()
        try:
            target = PathGuard(context.active_root).resolve(path)
        except PathGuardError as exc:
            raise ProductServiceError(str(exc)) from exc
        before = self._git_file(context.active_root, context.base_commit, path)
        after = target.read_bytes() if target.is_file() and not target.is_symlink() else b""
        if len(before) > 512_000 or len(after) > 512_000:
            raise ProductServiceError("change file exceeds preview size limit")
        try:
            return {"path": path, "before": before.decode("utf-8"), "after": after.decode("utf-8")}
        except UnicodeDecodeError as exc:
            raise ProductServiceError("binary changes cannot be opened as text diff") from exc

    def accept_changes(self, session_id: str) -> dict[str, Any]:
        self._require_idle(session_id)
        try:
            return CurrentChangesService(self._load(session_id)).accept(lambda _manifest, _patch: True)
        except (CurrentChangesError, CandidateError) as exc:
            raise ProductServiceError(str(exc)) from exc

    def discard_changes(self, session_id: str) -> dict[str, Any]:
        self._require_idle(session_id)
        try:
            return CurrentChangesService(self._load(session_id)).discard()
        except (CurrentChangesError, CandidateError) as exc:
            raise ProductServiceError(str(exc)) from exc

    def _require_idle(self, session_id: str) -> None:
        if self.supervisor.state(session_id) != "idle":
            raise ProductServiceError("cannot resolve changes while execution is running")

    @staticmethod
    def _interrupted_control(store: SessionStore) -> str | None:
        events = store.read_events()
        resolved_approvals = {event.payload.get("approval_id") for event in events if event.type == "approval_decision"}
        if any(event.type == "approval_requested" and event.payload.get("approval_id") not in resolved_approvals
               for event in events):
            return "Previous approval was interrupted; permission was not granted"
        requested_stops = {event.payload.get("job_id") for event in events if event.type == "execution_stop_requested"}
        acknowledged_stops = {event.payload.get("job_id") for event in events if event.type == "execution_stop_acknowledged"}
        if requested_stops - acknowledged_stops:
            return "Previous stop was interrupted before acknowledgement"
        return None

    @staticmethod
    def _change_stats(store: SessionStore, files: list[str]) -> dict[str, Any]:
        import subprocess
        context = store.workspace_context()
        if not files or context.workspace_kind != "git_worktree":
            return {"additions": 0, "deletions": 0, "fileStats": []}
        proc = subprocess.run(
            ["git", "diff", "--numstat", context.base_commit, "--", *files],
            cwd=context.active_root, text=True, capture_output=True, check=False, timeout=10,
        )
        tracked: dict[str, tuple[int | None, int | None]] = {}
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                parts = line.split("\t", 2)
                if len(parts) == 3:
                    tracked[parts[2]] = (
                        int(parts[0]) if parts[0].isdigit() else None,
                        int(parts[1]) if parts[1].isdigit() else None,
                    )
        rows = []
        for path in files:
            additions, deletions = tracked.get(path, (None, None))
            if path not in tracked:
                target = context.active_root / path
                try:
                    data = target.read_bytes()
                    additions, deletions = (len(data.splitlines()), 0) if b"\0" not in data[:8192] else (None, None)
                except OSError:
                    additions, deletions = 0, 0
            rows.append({"path": path, "additions": additions, "deletions": deletions})
        return {
            "additions": sum(row["additions"] or 0 for row in rows),
            "deletions": sum(row["deletions"] or 0 for row in rows),
            "fileStats": rows,
        }

    @staticmethod
    def _git_file(root: Path, revision: str, path: str) -> bytes:
        import subprocess
        proc = subprocess.run(
            ["git", "show", f"{revision}:{path}"], cwd=root, capture_output=True, check=False, timeout=10,
        )
        if proc.returncode == 0:
            return proc.stdout
        if b"does not exist" in proc.stderr or b"exists on disk" in proc.stderr or b"Path '" in proc.stderr:
            return b""
        raise ProductServiceError("failed to read baseline file")

    def get_events(self, session_id: str, *, after_seq: int = 0, limit: int = 200) -> dict[str, Any]:
        if after_seq < 0:
            raise ProductServiceError("afterSeq must be non-negative")
        if not 1 <= limit <= 1000:
            raise ProductServiceError("limit must be between 1 and 1000")
        events = [event for event in self._load(session_id).read_events() if (event.seq or 0) > after_seq]
        page = events[:limit]
        return {
            "events": [{
                "seq": event.seq,
                "turnId": event.turn_id,
                "modelStepId": event.model_step_id,
                "timestamp": event.ts,
                "type": event.type,
                "payload": event.payload,
                "activity": activity_item(event),
            } for event in page],
            "lastSeq": page[-1].seq if page else after_seq,
            "hasMore": len(events) > len(page),
        }

    def _load(self, session_id: str) -> SessionStore:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ProductServiceError("sessionId is required")
        meta = SessionStore.find_session(session_id, session_root=self.session_root)
        if meta is None or meta.get("error"):
            raise ProductServiceError(f"session not found: {session_id}")
        source = meta.get("source_workspace") or meta.get("workspace")
        try:
            return SessionStore(source, session_id=session_id, session_root=self.session_root).load()
        except (OSError, SessionStoreError, ValueError) as exc:
            raise ProductServiceError(str(exc)) from exc
