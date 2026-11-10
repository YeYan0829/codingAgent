from __future__ import annotations

from pathlib import Path
from typing import Any

from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.product.execution import ExecutionConflictError, ExecutionSupervisor
from codeagent.product.read_model import activity_item, build_session_detail, build_session_summary
from codeagent.runtime.applied_changes import AppliedChangesError, AppliedChangesStore
from codeagent.session.store import SessionStore, SessionStoreError
from codeagent.application.session_runtime import recover_lifecycle_transition


class ProductServiceError(RuntimeError):
    pass


class ProductApplicationService:
    """产品应用服务：读取 Session，并把执行交给进程内监管器。"""

    def __init__(self, session_root: Path | str | None = None, *, supervisor: ExecutionSupervisor | None = None) -> None:
        self.session_root = Path(session_root).expanduser().resolve() if session_root is not None else None
        self.supervisor = supervisor or ExecutionSupervisor()
        self._seen_sessions: set[str] = set()

    def get_repository_access(self, workspace):
        from codeagent.safety.repository_access import RepositoryAccessStore
        root = Path(workspace).expanduser().resolve()
        if not root.is_dir():
            raise ProductServiceError("仓库目录不存在")
        return RepositoryAccessStore(root, self.session_root).read()

    def save_repository_access(self, workspace, denied_paths, expected_revision):
        from codeagent.safety.repository_access import RepositoryAccessStore
        self.get_repository_access(workspace)
        return RepositoryAccessStore(workspace, self.session_root).save(denied_paths, expected_revision)

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
                if state == "idle":
                    from codeagent.orchestration.lease import execution_owner_busy
                    store = SessionStore(summary["workspace"], summary["sessionId"], self.session_root).load()
                    if execution_owner_busy(store):
                        state = "external_running"
                        self._seen_sessions.discard(summary["sessionId"])
                summary["executionState"] = state
                if state == "awaiting_approval":
                    summary["attentionSummary"] = "Approval required"
                elif state == "stopping":
                    summary["attentionSummary"] = "Stopping"
                elif state == "running":
                    summary["attentionSummary"] = "Running"
                elif state == "external_running":
                    summary["attentionSummary"] = "Running in another Runtime"
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
        from codeagent.orchestration.lease import execution_owner_busy
        detail = build_session_detail(store)
        detail["executionState"] = ("external_running" if self.supervisor.state(session_id) == "idle" and execution_owner_busy(store)
                                    else self.supervisor.state(session_id))
        detail["pendingApproval"] = self.supervisor.pending_approval(session_id)
        for task in detail["orchestration"]["tasks"]:
            for run in task["runs"]:
                run["awaitingApproval"] = bool(detail["pendingApproval"] and
                    detail["pendingApproval"].get("owner", {}).get("runId") == run["runId"])
        from codeagent.product.edits import edits_summary
        try:
            detail["workspaceEdits"] = edits_summary(store)
        except (OSError, ValueError, RuntimeError) as exc:
            detail["workspaceEdits"] = {"available": False, "files": [], "error": str(exc)}
        detail["availableActions"]["canUndo"] = bool(detail["workspaceEdits"].get("available")) and detail["executionState"] == "idle"
        from codeagent.safety.repository_access import assert_session_access, RepositoryAccessError
        try:
            assert_session_access(store)
        except RepositoryAccessError as exc:
            for action in ("canRun", "canContinue", "canIncreaseBudget", "canUndo"):
                detail["availableActions"][action] = False
                detail["actionReasons"][action] = str(exc)

        detail["availableActions"]["canRun"] &= detail["executionState"] == "idle"
        detail["availableActions"]["canContinue"] &= detail["executionState"] == "idle"
        detail["availableActions"]["canIncreaseBudget"] &= detail["executionState"] == "idle"
        detail["availableActions"]["canStop"] = (
            detail["executionState"] in {"running", "awaiting_approval"}
            or (detail["executionState"] == "idle" and detail["availableActions"]["canStop"])
        )
        detail["availableActions"]["canResolveApproval"] = detail["pendingApproval"] is not None
        if store.workspace_state().value == "recovery_required":
            detail["availableActions"]["canUndo"] = False
            detail["actionReasons"]["canUndo"] = "存在待核对操作，请先保留并检查现场"
        if detail["executionState"] != "idle":
            detail["actionReasons"]["canUndo"] = "请等待任务结束并回收资源后撤销"
        if detail["executionState"] != "idle":
            detail["availableActions"]["canAcceptChanges"] = False
            detail["availableActions"]["canDiscardChanges"] = False
            detail["actionReasons"]["canAcceptChanges"] = "Wait for the current execution to stop before applying changes."
            detail["actionReasons"]["canDiscardChanges"] = "Wait for the current execution to stop before discarding changes."
        if detail["pendingApproval"] is not None:
            detail["attentionSummary"] = "Approval required"
        elif detail["executionState"] == "external_running":
            self._seen_sessions.discard(session_id)
            detail["attentionSummary"] = "Running in another Runtime; use its Stop control"
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
        summary_max_tokens: int | None = None,
        reasoning_enabled: bool = False, reasoning_effort: str | None = None,
        delegation_enabled: bool = False,
        cost_per_token_microunits: int | None = None, fee_currency: str = "USD",
    ) -> dict[str, Any]:
        root = Path(workspace).expanduser().resolve()
        if not root.is_dir():
            raise ProductServiceError(f"workspace not found: {root}")
        try:
            runtime = RuntimeConfig(delegation_enabled=delegation_enabled, cost_per_token_microunits=cost_per_token_microunits,
                fee_currency=fee_currency)
            config = ModelConfig(
                provider=provider, model=model, reasoning_enabled=reasoning_enabled,
                reasoning_effort=reasoning_effort,
                max_tokens=max_tokens, summary_max_tokens=summary_max_tokens,
            )
        except ValueError as exc:
            raise ProductServiceError(str(exc)) from exc
        if provider not in {"fake", "deepseek", "glm"}:
            raise ProductServiceError(f"unknown provider: {provider}")
        if temperature is not None and not 0 <= temperature <= 2:
            raise ProductServiceError("temperature must be between 0 and 2")
        store = SessionStore(root, session_root=self.session_root).create(
            provider=config.provider, model=config.resolved_model, title=title,
            model_options={
                "temperature": temperature, "max_tokens": config.resolved_max_tokens,
                **({"summary_max_tokens": config.summary_max_tokens} if config.summary_max_tokens is not None else {}),
                "reasoning_enabled": config.reasoning_enabled,
                "reasoning_effort": config.resolved_reasoning_effort,
            },
            runtime_options={"delegation_enabled": runtime.delegation_enabled, "cost_per_token_microunits": runtime.cost_per_token_microunits,
                             "fee_currency": runtime.fee_currency},
        )
        return self.get_session(store.session_id)

    def run_session(self, session_id: str, message: str) -> dict[str, Any]:
        if not isinstance(message, str) or not message.strip() or len(message) > 20_000:
            raise ProductServiceError("message must be a non-empty string of at most 20000 characters")
        from codeagent.safety.repository_access import assert_session_access
        assert_session_access(self._load(session_id))
        try:
            return self.supervisor.start(self._load(session_id), message=message.strip())
        except ExecutionConflictError as exc:
            raise ProductServiceError(str(exc)) from exc

    def continue_session(self, session_id: str) -> dict[str, Any]:
        detail = self.get_session(session_id)
        if not detail["availableActions"]["canContinue"]:
            raise ProductServiceError("current UserTurn cannot continue")
        from codeagent.safety.repository_access import assert_session_access
        assert_session_access(self._load(session_id))
        try:
            return self.supervisor.start(self._load(session_id), continuation=True)
        except ExecutionConflictError as exc:
            raise ProductServiceError(str(exc)) from exc

    def stop_session(self, session_id: str) -> dict[str, Any]:
        store = self._load(session_id)
        if self.supervisor.state(session_id) != "idle":
            return self.supervisor.stop(store)
        from codeagent.orchestration.lease import session_lease
        try:
            with session_lease(store):
                return self.supervisor.stop(store)
        except RuntimeError as exc:
            raise ProductServiceError(str(exc)) from exc

    def resolve_approval(self, session_id: str, approval_id: str, allow: bool) -> dict[str, Any]:
        if not isinstance(allow, bool):
            raise ProductServiceError("allow must be a boolean")
        try:
            return self.supervisor.resolve_approval(session_id, approval_id, allow)
        except ExecutionConflictError as exc:
            raise ProductServiceError(str(exc)) from exc

    def get_edit_file(self, session_id: str, path: str) -> dict[str, Any]:
        from codeagent.product.edits import edit_file
        from codeagent.safety.repository_access import assert_session_access
        store = self._load(session_id)
        try:
            assert_session_access(store)
            return edit_file(store, path)
        except (OSError, ValueError, RuntimeError) as exc:
            raise ProductServiceError(str(exc)) from exc

    def undo_edits(self, session_id: str, turn_id: str | None, expected_edit_seq: int) -> dict[str, Any]:
        from codeagent.runtime.atomic_edit import AtomicEditService
        if type(expected_edit_seq) is not int or expected_edit_seq < 0:
            raise ProductServiceError("expectedEditSeq must be a non-negative integer")
        detail = self.get_session(session_id)
        if not detail["availableActions"]["canUndo"] or turn_id != detail["workspaceEdits"].get("turnId"):
            raise ProductServiceError("当前不可撤销或撤销轮次已变化，请刷新")
        store = self._load(session_id)
        result = AtomicEditService(store.workspace_context(), store).undo(turn_id, expected_edit_seq=expected_edit_seq)
        if not result.ok:
            raise ProductServiceError(result.error or "撤销失败")
        return {"undone": True, "message": result.content}

    def get_process(self, session_id: str, run_id: str, after_seq: int = 0, limit: int = 30) -> dict[str, Any]:
        from codeagent.product.orchestration_view import orchestration_view
        from codeagent.product.read_model import attach_child_timelines
        if type(after_seq) is not int or after_seq < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ProductServiceError("invalid process page")
        store = self._load(session_id)
        events = store.read_events()
        view = orchestration_view(events, store.read_meta())
        for task in view["tasks"]:
            task["runs"] = [run for run in task["runs"] if run["runId"] == run_id]
        attach_child_timelines(view, events, output_limit=512_000)
        run = next((r for task in view["tasks"] for r in task["runs"] if r["runId"] == run_id), None)
        if run is None:
            raise ProductServiceError("unknown runId")
        items = [i for i in run["items"] if i["eventSeq"] > after_seq]
        page = items[:limit]
        return {"runId": run_id, "items": page, "hasMore": len(items) > len(page), "afterSeq": after_seq,
                "snapshotSeq": max((e.seq or 0 for e in events), default=0),
                "lastSeq": page[-1]["eventSeq"] if page else after_seq}

    def get_changes(self, session_id: str) -> dict[str, Any]:
        detail = self.get_session(session_id)
        summary = detail["changesSummary"]
        stats = (
            {key: summary[key] for key in ("additions", "deletions", "fileStats")}
            if summary.get("state") == "applied"
            else {"additions": 0, "deletions": 0, "fileStats": []}
        )
        return {
            **summary,
            **stats,
            "validation": detail["validationSummary"],
            "canAccept": detail["availableActions"]["canAcceptChanges"],
            "canDiscard": detail["availableActions"]["canDiscardChanges"],
        }

    def get_change_file(self, session_id: str, path: str) -> dict[str, Any]:
        store = self._load(session_id)
        detail = self.get_session(session_id)
        summary = detail["changesSummary"]
        if path not in summary["files"]:
            raise ProductServiceError("path is not part of the available changes")
        if summary.get("state") == "applied":
            try:
                return AppliedChangesStore(store).read_file(str(summary.get("deliveryId")), path)
            except AppliedChangesError as exc:
                raise ProductServiceError(str(exc)) from exc
        raise ProductServiceError("旧待交付版本不再恢复；只能读取已保存的历史交付产物")

    def accept_changes(self, session_id: str) -> dict[str, Any]:
        raise ProductServiceError("修改已直接写入当前项目，无需Accept")

    def discard_changes(self, session_id: str) -> dict[str, Any]:
        raise ProductServiceError("旧Discard接口已停用；撤销受控编辑请使用undo")

    @staticmethod
    def _interrupted_control(store: SessionStore) -> str | None:
        events = store.read_events()
        resolved_approvals = {event.payload.get("approval_id") for event in events if event.type == "approval_decision"}
        if any(event.type == "approval_requested" and event.payload.get("approval_id") not in resolved_approvals
               and event.payload.get("kind") != "workspace_upgrade"
               and event.payload.get("reason") != "创建隔离 Agent worktree"
               for event in events):
            return "Previous approval was interrupted; permission was not granted"
        requested_stops = {event.payload.get("job_id") for event in events if event.type == "execution_stop_requested"}
        acknowledged_stops = {event.payload.get("job_id") for event in events if event.type == "execution_stop_acknowledged"}
        if requested_stops - acknowledged_stops:
            return "Previous stop was interrupted before acknowledgement"
        return None

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
            store = SessionStore(source, session_id=session_id, session_root=self.session_root).load()
            if session_id not in self._seen_sessions:
                if self.supervisor.state(session_id) == "idle":
                    from codeagent.product.recovery import reconcile_product_session
                    try:
                        if (store.session_dir / "execution.lock").exists():
                            reconcile_product_session(store)
                        else:
                            recover_lifecycle_transition(store)
                        self._seen_sessions.add(session_id)
                    except RuntimeError as exc:
                        if str(exc) != "session_busy":
                            raise
            return store
        except (OSError, SessionStoreError, ValueError) as exc:
            raise ProductServiceError(str(exc)) from exc
