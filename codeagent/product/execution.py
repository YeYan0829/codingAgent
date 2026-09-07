from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from codeagent.application.session_runtime import build_agent_runner
from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.model_gateway.base import LLMToolCall
from codeagent.runtime.command import CommandRequest
from codeagent.runtime.turn_budget import increase_turn_budget, store_turn_budget
from codeagent.runtime.sandbox_policy import PermissionEvaluation
from codeagent.tools.base import ToolSpec
from codeagent.session.store import SessionStore
from codeagent.workspace.workspace import SessionWorkspaceState


class ExecutionConflictError(RuntimeError):
    pass


@dataclass
class ExecutionRecord:
    job_id: str
    state: str
    stop_requested: bool = False
    stop_requested_at: str | None = None
    pending_approval_id: str | None = None


@dataclass
class PendingApproval:
    approval_id: str
    session_id: str
    job_id: str
    kind: str
    summary: dict[str, Any]
    decision: bool | None = None


class ProductApprovalGate:
    """把同步 ApprovalGate 调用桥接到可并发响应的产品监管器。"""

    manages_events = True

    def __init__(self, supervisor: "ExecutionSupervisor", store: SessionStore, record: ExecutionRecord) -> None:
        self.supervisor, self.store, self.record = supervisor, store, record
        self.workspace_context = store.workspace_context()

    def request(self, tool: ToolSpec, call: LLMToolCall | None = None) -> bool:
        return self.supervisor.await_approval(self.store, self.record, "tool", {
            "tool": tool.name, "permissionLevel": tool.permission_level.value,
            "arguments": _bounded_arguments(call.arguments if call else {}),
        })

    def request_workspace_upgrade(self, tool_name: str, arguments: dict) -> bool:
        return self.supervisor.await_approval(self.store, self.record, "workspace_upgrade", {
            "tool": tool_name, "arguments": _bounded_arguments(arguments),
            "message": "Create an isolated Agent worktree for protected operations",
        })

    def request_permissions(
        self, command: CommandRequest, evaluations: tuple[PermissionEvaluation, ...],
    ) -> bool:
        return self.supervisor.await_approval(self.store, self.record, "command_permissions", {
            "command": command.command[:500], "cwd": command.cwd,
            "permissions": [item.request.to_dict() for item in evaluations],
        })


class ExecutionSupervisor:
    """产品进程内执行、Stop 与 Approval 监管；不持久化或恢复 Python 执行栈。"""

    def __init__(
        self,
        notify: Callable[[str, dict[str, Any]], None] | None = None,
        *,
        runner_factory: Callable[..., Any] = build_agent_runner,
    ) -> None:
        self.notify = notify or (lambda _method, _params: None)
        self.runner_factory = runner_factory
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._records: dict[str, ExecutionRecord] = {}
        self._approvals: dict[str, PendingApproval] = {}

    def state(self, session_id: str) -> str:
        with self._lock:
            record = self._records.get(session_id)
            return record.state if record is not None else "idle"

    def pending_approval(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(session_id)
            pending = self._approvals.get(record.pending_approval_id) if record and record.pending_approval_id else None
            if pending is None:
                return None
            return {
                "approvalId": pending.approval_id, "jobId": pending.job_id,
                "kind": pending.kind, "summary": pending.summary,
            }

    def start(self, store: SessionStore, *, message: str | None = None, continuation: bool = False,
              budget_increase: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            existing = self._records.get(store.session_id)
            if existing is not None:
                raise ExecutionConflictError("session already has an active execution")
            budget = store_turn_budget(store)
            if budget_increase is not None:
                if not continuation:
                    raise ExecutionConflictError("budget increase requires continuation")
                try:
                    increase_turn_budget(store, **budget_increase)
                except ValueError as exc:
                    raise ExecutionConflictError(str(exc)) from exc
            elif continuation and (budget.closed or budget.waiting):
                raise ExecutionConflictError("UserTurn cannot continue without an explicit budget increase")
            elif message is not None and not budget.closed:
                raise ExecutionConflictError("Stop the unfinished UserTurn before sending a new request")
            record = ExecutionRecord(uuid.uuid4().hex, "running")
            self._records[store.session_id] = record
        thread = threading.Thread(
            target=self._run, args=(store, record), kwargs={"message": message, "continuation": continuation},
            name=f"codeagent-{store.session_id}", daemon=True,
        )
        self._notify_state(store.session_id, record)
        thread.start()
        return {"jobId": record.job_id, "executionState": "running", "approvalMode": "interactive"}

    def stop(self, store: SessionStore) -> dict[str, Any]:
        from datetime import datetime, timezone
        idle_seq = None
        with self._condition:
            record = self._records.get(store.session_id)
            if record is None:
                if store_turn_budget(store).closed:
                    return {"accepted": False, "executionState": "idle"}
                # 预算等待没有活跃 worker，Stop 仍可结束本轮，之后允许发送修正指令。
                job_id = uuid.uuid4().hex
                request = store.append_event("execution_stop_requested", {"job_id": job_id})
                idle_seq = (request.seq or 1) - 1
                store.append_event("turn_terminated", {"reason": "user_stop", "message": "用户已结束本轮执行。"})
                store.append_event("execution_stop_acknowledged", {
                    "job_id": job_id, "requested_at": request.ts,
                    "acknowledged_at": datetime.now(timezone.utc).isoformat(), "boundary": "idle_user_turn",
                })
            else:
                if not record.stop_requested:
                    record.stop_requested = True
                    record.stop_requested_at = datetime.now(timezone.utc).isoformat()
                    store.append_event("execution_stop_requested", {
                        "job_id": record.job_id, "requested_at": record.stop_requested_at,
                    })
                record.state = "stopping"
                if record.pending_approval_id:
                    pending = self._approvals.get(record.pending_approval_id)
                    if pending is not None:
                        pending.decision = False
                self._condition.notify_all()
        if record is None:
            self._publish_events(store, idle_seq or 0)
            self.notify("session/executionStateChanged", {"sessionId": store.session_id, "executionState": "idle"})
            return {"accepted": True, "executionState": "idle"}
        self._notify_state(store.session_id, record)
        return {"accepted": True, "jobId": record.job_id, "executionState": "stopping"}

    def resolve_approval(self, session_id: str, approval_id: str, allow: bool) -> dict[str, Any]:
        with self._condition:
            record = self._records.get(session_id)
            pending = self._approvals.get(approval_id)
            if record is None or pending is None or pending.job_id != record.job_id:
                raise ExecutionConflictError("approval is not active or does not match the current job")
            if record.stop_requested:
                raise ExecutionConflictError("execution is stopping; approval cannot be granted")
            pending.decision = bool(allow)
            self._condition.notify_all()
        return {"approvalId": approval_id, "resolved": True, "allowed": bool(allow)}

    def await_approval(
        self, store: SessionStore, record: ExecutionRecord, kind: str, summary: dict[str, Any],
    ) -> bool:
        approval = PendingApproval(uuid.uuid4().hex, store.session_id, record.job_id, kind, summary)
        with self._condition:
            current = self._records.get(store.session_id)
            if current is not record or record.stop_requested:
                return False
            record.state = "awaiting_approval"
            record.pending_approval_id = approval.approval_id
            self._approvals[approval.approval_id] = approval
            store.append_event("approval_requested", {
                "approval_id": approval.approval_id, "job_id": record.job_id,
                "kind": kind, "summary": summary,
            })
        self._notify_state(store.session_id, record)
        self.notify("approval/requested", {
            "sessionId": store.session_id, "approvalId": approval.approval_id,
            "jobId": record.job_id, "kind": kind, "summary": summary,
        })
        with self._condition:
            while approval.decision is None and not record.stop_requested:
                self._condition.wait()
            allowed = bool(approval.decision) and not record.stop_requested
            self._approvals.pop(approval.approval_id, None)
            record.pending_approval_id = None
            record.state = "stopping" if record.stop_requested else "running"
            store.append_event("approval_decision", {
                "approval_id": approval.approval_id, "job_id": record.job_id,
                "allowed": allowed, "aborted": record.stop_requested,
            })
        self._notify_state(store.session_id, record)
        return allowed

    def _run(self, store: SessionStore, record: ExecutionRecord, *, message: str | None, continuation: bool) -> None:
        last_seq = max((event.seq or 0 for event in store.read_events()), default=0)

        def publish_progress(_: dict[str, Any]) -> None:
            nonlocal last_seq
            last_seq = self._publish_events(store, last_seq)

        meta = store.read_meta()
        model_options = meta.get("model_options") if isinstance(meta.get("model_options"), dict) else {}
        runtime_options = meta.get("runtime_options") if isinstance(meta.get("runtime_options"), dict) else {}
        config = ModelConfig(
            provider=str(meta.get("provider", "fake")), model=meta.get("model"),
            temperature=model_options.get("temperature"), max_tokens=model_options.get("max_tokens"),
        )
        runtime_config = RuntimeConfig(
            max_steps_per_turn=int(runtime_options.get("max_steps_per_turn", 12)),
            max_model_steps_per_user_turn=int(runtime_options.get("max_model_steps_per_user_turn", 48)),
        )
        execution_allowed = store.workspace_state() == SessionWorkspaceState.CHANGES_ACTIVE
        status = "failed"
        error = None
        try:
            runner = self.runner_factory(
                store,
                model_config=config,
                approval_gate=ProductApprovalGate(self, store, record),
                execution_allowed=execution_allowed,
                progress_callback=publish_progress,
                runtime_config=runtime_config,
                cancellation_callback=lambda: record.stop_requested,
            )
            output = runner.continue_turn() if continuation else runner.run_turn(message or "")
            while output.status == "slice_exhausted":
                # 同一 job 自动跨切片；Runner 在下一调用前检查 Stop，预算由本轮事件累计。
                output = runner.continue_turn()
            status = output.status
        except Exception as exc:
            error = str(exc)
            events = store.read_events()
            turn_id = next((event.turn_id for event in reversed(events) if event.type == "user_message"), None)
            closed = any(event.turn_id == turn_id and event.type in {"assistant_message", "turn_terminated"}
                         for event in events) if turn_id else True
            if not closed:
                store.append_event("turn_terminated", {
                    "reason": "runtime_error",
                    "message": "The model or CodeAgent Runtime failed before producing a final response.",
                    "error_type": type(exc).__name__,
                })
        finally:
            # Stop 的最后检查、ack 与移除 job 共用互斥锁，避免切片/预算结束处丢失请求。
            with self._lock:
                if record.stop_requested:
                    from datetime import datetime, timezone
                    if not store_turn_budget(store).closed:
                        store.append_event("turn_terminated", {
                            "reason": "user_stop", "message": "用户已请求停止；本轮结束。",
                        })
                    status = "stopped"
                    store.append_event("execution_stop_acknowledged", {
                        "job_id": record.job_id, "requested_at": record.stop_requested_at,
                        "acknowledged_at": datetime.now(timezone.utc).isoformat(),
                        "boundary": "between_model_or_tool_steps",
                    })
                current = self._records.get(store.session_id)
                if current is not None and current.job_id == record.job_id:
                    self._records.pop(store.session_id, None)
            last_seq = self._publish_events(store, last_seq)
            self.notify("session/executionStateChanged", {
                "sessionId": store.session_id,
                "jobId": record.job_id,
                "executionState": "idle",
                "resultStatus": status,
                "error": error,
            })

    def _publish_events(self, store: SessionStore, after_seq: int) -> int:
        events = [event for event in store.read_events() if (event.seq or 0) > after_seq]
        for event in events:
            self.notify("session/event", {
                "sessionId": store.session_id,
                "seq": event.seq,
                "type": event.type,
            })
        return (events[-1].seq or after_seq) if events else after_seq

    def _notify_state(self, session_id: str, record: ExecutionRecord) -> None:
        self.notify("session/executionStateChanged", {
            "sessionId": session_id, "jobId": record.job_id, "executionState": record.state,
        })


def _bounded_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return _bounded_value(arguments)


def _bounded_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _bounded_value(item) for key, item in list(value.items())[:20]
                if key not in {"content", "expected_sha256"}}
    if isinstance(value, list):
        return [_bounded_value(item) for item in value[:20]]
    text = str(value)
    return value if len(text) <= 500 else text[:497] + "..."
