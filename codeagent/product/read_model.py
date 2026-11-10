from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from codeagent.session.events import SessionEvent
from codeagent.session.store import SessionStore
from codeagent.runtime.turn_budget import MAX_USER_TURN_BUDGET, current_turn_budget
from codeagent.runtime.applied_changes import AppliedChangesError, AppliedChangesStore
from codeagent.workspace.workspace import SessionWorkspaceState


_ACTIVITY_LABELS = {
    "session_created": "Session created",
    "assistant_tool_calls": "Agent requested tools",
    "tool_requested": "Tool requested",
    "tool_result": "Tool completed",
    "tool_denied": "Tool denied",
    "command_requested": "Command started",
    "command_completed": "Command completed",
    "validation_completed": "Validation command succeeded",
    "edit_transaction": "Workspace edited",
    "edit_receipt": "Workspace edited",
    "workspace_preparing": "Preparing isolated workspace",
    "workspace_activated": "Isolated workspace ready",
    "workspace_upgrade_failed": "Workspace preparation failed",
    "changes_frozen": "Changes prepared for review",
    "changes_apply_result": "Changes review completed",
    "changes_accepted": "Changes accepted",
    "changes_discarded": "Changes discarded",
    "workspace_tainted": "Changes need attention",
    "recovery_required": "Manual recovery required",
    "approval_requested": "Approval requested",
    "approval_decision": "Approval resolved",
    "permission_denied": "Permission denied",
    "execution_slice_exhausted": "Internal execution checkpoint",
    "turn_budget_exhausted": "Step budget reached",
    "turn_budget_increased": "User increased step budget",
    "turn_terminated": "Execution stopped before completion",
    "model_output_truncated": "Model output reached its length limit",
    "model_protocol_error": "Model response could not be processed",
}


def build_session_summary(meta: dict[str, Any]) -> dict[str, Any]:
    if meta.get("error"):
        return {
            "sessionId": str(meta.get("session_id", "unknown")),
            "title": "Unreadable session",
            "workspace": "",
            "workspaceLabel": "",
            "executionState": "idle",
            "attentionSummary": "Session data needs attention",
            "lastActiveAt": None,
            "changedFileCount": 0,
            "changesState": "none",
        }
    store = _store_from_meta(meta)
    events = store.read_events()
    changes = inspect_changes(store)
    source_workspace = str(meta.get("source_workspace") or meta.get("workspace") or "")
    model_options = meta.get("model_options") if isinstance(meta.get("model_options"), dict) else {}
    return {
        "sessionId": store.session_id,
        "title": str(meta.get("title") or SessionStore.UNTITLED),
        "provider": str(meta.get("provider", "fake")),
        "model": str(meta.get("model", "fake")),
        "reasoningEnabled": model_options.get("reasoning_enabled") is True,
        "reasoningEffort": model_options.get("reasoning_effort"),
        "workspace": source_workspace,
        "workspaceLabel": Path(source_workspace).name,
        "executionState": "idle",
        "attentionSummary": attention_summary(meta, events, changes),
        "lastActiveAt": meta.get("last_active_at"),
        "changedFileCount": len(changes["files"]),
        "changesState": changes["state"],
    }


def build_session_detail(store: SessionStore) -> dict[str, Any]:
    meta = store.read_meta()
    events = store.read_events()
    changes = inspect_changes(store)
    summary = build_session_summary(meta)
    validation = validation_summary(events, meta)
    if changes["state"] == "applied" and validation is not None:
        validation = {**validation, "appliesToCurrentChanges": False, "historical": True}
    actions = available_actions(meta, events, changes)
    return {
        **summary,
        "turns": timeline_turns(events, meta),
        "conversation": conversation_view(events),
        "activitySummary": activity_summary(events, changes),
        "recentActivity": [activity_item(event) for event in events if event.type not in {
            "user_message", "assistant_message", "model_usage",
            "context_condensation_attempt", "context_condensed",
        }][-50:],
        "changesSummary": {**changes, "fileCount": len(changes["files"])},
        "validationSummary": validation,
        "deliveryReceipt": delivery_receipt(events),
        "globalAttention": global_attention(meta, events),
        "availableActions": actions,
        "turnBudget": turn_budget_view(meta, events),
        "actionReasons": action_reasons(meta, events, changes, actions, validation),
        "lastSeq": max((event.seq or 0 for event in events), default=0),
    }


def delivery_receipt(events: list[SessionEvent]) -> dict[str, str] | None:
    latest = next((event for event in reversed(events)
                   if event.type in {"changes_accepted", "changes_discarded"}), None)
    if latest is None:
        return None
    return {
        "kind": "accepted" if latest.type == "changes_accepted" else "discarded",
        "message": (
            "Changes applied to your repository"
            if latest.type == "changes_accepted"
            else "Agent changes discarded; source unchanged"
        ),
        "timestamp": latest.ts,
    }


def global_attention(meta: dict[str, Any], events: list[SessionEvent]) -> dict[str, str] | None:
    state = meta.get("workspace_state")
    if state == SessionWorkspaceState.RECOVERY_REQUIRED.value:
        return {
            "kind": "recovery", "title": "Manual recovery required",
            "message": "CodeAgent cannot verify the managed workspace. Protected actions remain disabled; inspect diagnostics before continuing.",
            "action": "Open CodeAgent Runtime output for details.",
        }
    if state == SessionWorkspaceState.WORKSPACE_TAINTED.value:
        return {
            "kind": "tainted", "title": "Changes need attention",
            "message": "CodeAgent could not fully verify the current workspace changes. Your source repository has not been automatically overwritten.",
            "action": "Review the changes or discard the managed workspace changes.",
        }
    terminated = next((event for event in reversed(events) if event.type == "turn_terminated"), None)
    if terminated and terminated.payload.get("reason") == "runtime_error":
        return {
            "kind": "execution_error", "title": "Agent execution stopped",
            "message": str(terminated.payload.get("message") or "The model or Runtime failed before producing a final response."),
            "action": "Retry the request or open CodeAgent Runtime output for details.",
        }
    if terminated and terminated.payload.get("reason") == "context_budget_exceeded":
        return {
            "kind": "context_limit", "title": "Agent context limit reached",
            "message": "Runtime could not build a safe model request after summarizing older context.",
            "action": "Retry with a narrower request or open CodeAgent Runtime output for diagnostics.",
        }
    return None


def timeline_turns(events: list[SessionEvent], meta: dict[str, Any]) -> list[dict[str, Any]]:
    """把持久 Event 机械投影为按 UserTurn 排序的产品时间线。"""
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    calls: dict[str, dict[str, Any]] = {}
    active_group: dict[str, Any] | None = None
    truncation_notice: dict[str, Any] | None = None
    latest_condenser_usage: dict[str, Any] | None = None
    latest_condensation_attempt: dict[str, Any] | None = None
    pending_context_summary: dict[str, Any] | None = None
    latest_main_usage: SessionEvent | None = None
    pending_truncation_record: dict[str, Any] | None = None
    condensation_failures = 0

    def ensure_turn(event: SessionEvent) -> dict[str, Any] | None:
        nonlocal current
        if current is not None:
            return current
        if not event.turn_id:
            return None
        current = _new_timeline_turn(event.turn_id, "", event.seq, event.ts)
        turns.append(current)
        return current

    def close_group() -> None:
        nonlocal active_group
        active_group = None

    for event in events:
        if event.type == "user_message":
            current = _new_timeline_turn(
                event.turn_id or f"legacy-turn-{event.seq}",
                str(event.payload.get("message", "")), event.seq, event.ts,
            )
            turns.append(current)
            calls = {}
            active_group = None
            truncation_notice = None
            latest_condenser_usage = None
            latest_condensation_attempt = None
            pending_context_summary = None
            latest_main_usage = None
            pending_truncation_record = None
            condensation_failures = 0
            continue
        turn = ensure_turn(event)
        if turn is None:
            continue
        if event.type == "assistant_tool_calls":
            message = str(event.payload.get("message") or "")
            if message:
                close_group()
                turn["items"].append(_markdown_item(message, event))
            active_group = _activity_group(event)
            turn["items"].append(active_group)
            for raw_call in event.payload.get("tool_calls", []):
                call = {
                    "callId": str(raw_call.get("call_id", "")),
                    "tool": str(raw_call.get("name", "unknown")),
                    "summary": _tool_summary(str(raw_call.get("name", "unknown")), dict(raw_call.get("arguments") or {})),
                    "argumentSummary": _tool_argument_summary(
                        str(raw_call.get("name", "unknown")), dict(raw_call.get("arguments") or {})
                    ),
                    "status": "running",
                    "detail": _bounded_payload(dict(raw_call.get("arguments") or {})),
                }
                active_group["tools"].append(call)
                if call["callId"]:
                    calls[call["callId"]] = call
        elif event.type in {"tool_result", "tool_denied"}:
            call = calls.get(str(event.payload.get("call_id", "")))
            if call is not None:
                result = event.payload.get("result") if isinstance(event.payload.get("result"), dict) else {}
                call["status"] = (
                    "denied" if event.type == "tool_denied"
                    else "succeeded" if result.get("ok", True) else "failed"
                )
                call["result"] = _tool_result_summary(result, event.payload)
                if call["status"] in {"failed", "denied"}:
                    call["failure"] = _tool_failure_details(result, event.payload)
                if call["status"] in {"failed", "denied"} and active_group is not None:
                    active_group["defaultExpanded"] = True
        elif event.type == "command_completed":
            command = _latest_tool(calls, "run_command")
            if command is not None:
                command["status"] = "succeeded" if event.payload.get("status") == "completed" and event.payload.get("exit_code") == 0 else "failed"
                command["command"] = event.payload.get("command")
                command["exitCode"] = event.payload.get("exit_code")
                command["durationMs"] = event.payload.get("duration_ms")
                if command["status"] == "failed" and active_group is not None:
                    active_group["defaultExpanded"] = True
            _add_changed_files(turn, event.payload.get("changed_paths", []))
            if event.payload.get("purpose") == "validation":
                turn["validation"] = {
                    "command": event.payload.get("command"),
                    "status": "passed" if event.payload.get("status") == "completed" and event.payload.get("exit_code") == 0 else "failed",
                    "exitCode": event.payload.get("exit_code"),
                    "durationMs": event.payload.get("duration_ms"),
                    "appliesToCurrentChanges": False,
                }
        elif event.type == "validation_completed":
            if turn.get("validation") is None:
                turn["validation"] = {
                    "command": event.payload.get("command"), "status": "passed",
                    "exitCode": event.payload.get("exit_code"), "durationMs": None,
                    "appliesToCurrentChanges": False,
                }
            turn["validation"]["appliesToCurrentChanges"] = (
                int(event.payload.get("candidate_revision", -1)) == int(meta.get("candidate_revision", 0))
            )
        elif event.type in {"edit_transaction", "edit_receipt"}:
            paths = event.payload.get("changed_files") or ([event.payload.get("path")] if event.payload.get("path") else [])
            _add_changed_files(turn, paths)
            edit = _latest_tool(calls, "apply_workspace_edit")
            if edit is not None:
                edit["changedFiles"] = sorted({*edit.get("changedFiles", []), *[str(path) for path in paths if path]})
        elif event.type == "assistant_message":
            close_group()
            turn["items"].append(_markdown_item(str(event.payload.get("message", "")), event, final=True))
        elif event.type == "model_usage" and event.payload.get("purpose") == "context_condenser":
            latest_condenser_usage = {
                "model": event.payload.get("model"), "usage": event.payload.get("usage"),
                "finishReason": event.payload.get("finish_reason"),
            }
        elif event.type == "model_usage":
            if pending_context_summary is not None:
                context = event.payload.get("context")
                if isinstance(context, dict):
                    pending_context_summary["rebuildRequestEstimatedTokens"] = context.get("estimated_tokens")
                pending_context_summary = None
            if pending_truncation_record is not None:
                pending_truncation_record.update({
                    "recoveryReasoningEffort": event.payload.get("reasoning_effort"),
                    "recoveryFinishReason": event.payload.get("finish_reason"),
                    "recoveredInOneRequest": event.payload.get("finish_reason") in {"tool_calls", "stop"},
                })
                pending_truncation_record = None
            latest_main_usage = event
        elif event.type == "context_condensation_attempt":
            latest_condensation_attempt = event.payload
            if event.payload.get("status") != "valid_completion":
                condensation_failures += 1
        elif event.type == "context_condensed":
            close_group()
            covered = event.payload.get("newly_covered_event_ids")
            count = len(covered) if isinstance(covered, list) else event.payload.get("logical_covered_event_count")
            summary_tokens = event.payload.get("summary_estimated_tokens")
            facts = []
            if isinstance(count, int):
                facts.append(f"{count} earlier events")
            if isinstance(summary_tokens, int):
                facts.append(f"~{summary_tokens} token summary")
            context_summary = {
                "type": "contextSummary", "tone": "system",
                "title": "Historical context summarized",
                "message": " · ".join(facts) or "Older Runtime history was summarized.",
                "summary": str(event.payload.get("summary") or ""),
                "coveredEventCount": count, "summaryEstimatedTokens": summary_tokens,
                "inputEstimatedTokens": event.payload.get("input_estimated_tokens"),
                "requestEstimatedTokensBefore": (
                    latest_condensation_attempt.get("request_estimated_tokens_before")
                    if latest_condensation_attempt else None
                ),
                "reason": event.payload.get("reason"), "retryCount": condensation_failures,
                "condenser": latest_condenser_usage, "eventSeq": event.seq, "timestamp": event.ts,
            }
            turn["items"].append(context_summary)
            pending_context_summary = context_summary
            condensation_failures = 0
        elif event.type == "model_output_truncated":
            close_group()
            if truncation_notice is None:
                truncation_notice = {
                    "type": "notice", "tone": "info", "kind": "truncationRecovery",
                    "message": "Model output was truncated; Runtime continued automatically.",
                    "count": 1, "records": [], "eventSeq": event.seq,
                }
                turn["items"].append(truncation_notice)
            else:
                truncation_notice["count"] += 1
                truncation_notice["message"] = (
                    f"Runtime recovered from {truncation_notice['count']} truncated model outputs."
                )
            record = {
                "finishReason": event.payload.get("finish_reason"),
                "originalReasoningEffort": (
                    latest_main_usage.payload.get("reasoning_effort") if latest_main_usage else None
                ),
            }
            truncation_notice["records"].append(record)
            pending_truncation_record = record
        elif event.type in {"turn_budget_exhausted", "turn_budget_increased"}:
            close_group()
            message = (f"Step budget reached: {event.payload.get('steps_used_in_turn')}/{event.payload.get('max_model_steps_per_user_turn')}"
                       if event.type == "turn_budget_exhausted" else
                       f"User increased step budget: {event.payload.get('previous_limit')} → {event.payload.get('new_limit')}")
            turn["items"].append({"type": "notice", "tone": "info", "message": message, "eventSeq": event.seq})
        elif event.type == "turn_terminated":
            close_group()
            if event.payload.get("reason") in {"user_stop", "cancelled"}:
                for call in calls.values():
                    if call.get("status") == "running":
                        call["status"] = "cancelled"
            reason = event.payload.get("reason")
            message = str(event.payload.get("message") or "Execution stopped before completion")
            if reason == "context_budget_exceeded":
                message = "Runtime could not build a safe model request after summarizing older context."
            elif reason == "output_truncated":
                message = "Model output remained truncated after automatic recovery; execution stopped."
                if truncation_notice is not None:
                    truncation_notice["tone"] = "warning"
                    truncation_notice["message"] = (
                        f"Runtime attempted recovery from {truncation_notice['count']} truncated model outputs."
                    )
            turn["items"].append({
                "type": "notice", "tone": "warning", "message": message,
                "eventSeq": event.seq,
            })
        elif event.type in {"workspace_tainted", "recovery_required"}:
            close_group()
            turn["items"].append({
                "type": "notice", "tone": "error", "message": _ACTIVITY_LABELS[event.type], "eventSeq": event.seq,
            })
        elif event.type == "approval_requested":
            if active_group is not None:
                active_group["defaultExpanded"] = True
            turn["items"].append({
                "type": "notice", "tone": "warning", "message": "Approval requested", "eventSeq": event.seq,
            })

    for turn in turns:
        turn["changedFiles"] = sorted(set(turn["changedFiles"]))
        for item in turn["items"]:
            if item.get("type") == "activityGroup":
                item["status"] = "failed" if any(tool.get("status") == "failed" for tool in item["tools"]) else (
                    "denied" if any(tool.get("status") == "denied" for tool in item["tools"]) else (
                    "cancelled" if any(tool.get("status") == "cancelled" for tool in item["tools"]) else (
                    "running" if any(tool.get("status") == "running" for tool in item["tools"]) else "succeeded"
                )))
                item["summary"] = _activity_group_summary(item["tools"])
    return turns


def _new_timeline_turn(turn_id: str, message: str, seq: int | None, ts: str) -> dict[str, Any]:
    return {
        "turnId": turn_id, "eventSeq": seq, "timestamp": ts, "userMessage": message,
        "items": [], "changedFiles": [], "validation": None, "attention": [],
    }


def _markdown_item(message: str, event: SessionEvent, *, final: bool = False) -> dict[str, Any]:
    return {"type": "markdown", "text": message, "final": final, "eventSeq": event.seq, "timestamp": event.ts}


def _activity_group(event: SessionEvent) -> dict[str, Any]:
    return {
        "type": "activityGroup", "summary": "Working", "status": "running", "tools": [],
        "defaultExpanded": False, "eventSeq": event.seq, "timestamp": event.ts,
    }


def _tool_summary(name: str, arguments: dict[str, Any]) -> str:
    path = arguments.get("path")
    if name == "read_file" and path:
        return f"Read {path}"
    if name in {"list_dir", "find_files"}:
        return f"Inspected {path or 'repository'}"
    if name == "search_text":
        return f"Searched {_bounded(str(arguments.get('query', 'repository')), 80)}"
    if name == "apply_workspace_edit":
        return "Edited workspace"
    if name == "run_command":
        return f"Ran {_bounded(str(arguments.get('command', 'command')), 100)}"
    return name.replace("_", " ").capitalize()


def _tool_argument_summary(name: str, arguments: dict[str, Any]) -> str:
    path = arguments.get("path")
    if name == "read_file":
        parts = [str(path or "<unknown>")]
        start, end = arguments.get("start_line"), arguments.get("end_line")
        if start is not None or end is not None:
            parts.append(f"lines {start or 1}–{end or 'end'}")
        return " · ".join(parts)
    if name in {"list_dir", "find_files"}:
        return f"path: {path or '.'}"
    if name == "search_text":
        query = _bounded(str(arguments.get("query", "")), 80)
        return f"query: {query}" + (f" · path: {path}" if path else "")
    if name == "apply_workspace_edit":
        operations = arguments.get("operations")
        return f"{len(operations)} operations" if isinstance(operations, list) else "workspace edit"
    if name == "run_command":
        return _bounded(str(arguments.get("command", "")), 160)
    visible = [f"{key}: {_bounded(str(value), 80)}" for key, value in arguments.items() if key not in {"content", "expected_sha256"}]
    return " · ".join(visible[:3])


def _tool_result_summary(result: dict[str, Any], payload: dict[str, Any]) -> str | None:
    failure = _tool_failure_details(result, payload)
    if failure is not None:
        return failure["summary"]
    error = result.get("error") or payload.get("reason")
    if error:
        return _bounded(str(error), 240)
    return None


def _tool_failure_details(result: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
    error = str(result.get("error") or payload.get("reason") or "").strip()
    error_code = str(result.get("error_code") or "").strip()
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    status = str(metadata.get("status") or "").strip()
    exit_code = metadata.get("exit_code")
    failed = result.get("ok") is False or bool(error) or bool(payload.get("reason"))
    if not failed:
        return None

    if error_code == "command_exit_nonzero" or (status == "completed" and exit_code not in {None, 0}):
        kind = "command_exit_nonzero"
        title = f"Command exited with code {exit_code}"
    elif error_code == "execution_timed_out" or status == "execution_timed_out":
        kind, title = "timeout", "Command timed out"
    elif error_code == "approval_denied":
        kind, title = "approval_rejected", "Approval rejected"
    elif error_code in {"policy_denied", "permission_denied"}:
        kind, title = "policy_denied", "Operation blocked by policy"
    elif error_code in {"sandbox_unavailable", "sandbox_setup_failed"} or status in {
        "sandbox_unavailable", "sandbox_setup_failed",
    }:
        kind, title = "sandbox_unavailable", "Sandbox unavailable"
    else:
        kind, title = "tool_error", "Tool could not run"

    stderr_tail = str(metadata.get("stderr_tail") or "").strip()
    diagnostic = str(metadata.get("diagnostic") or "").strip()
    summary = _important_error_line(stderr_tail or diagnostic or error or title)
    raw_output = str(result.get("content") or "").strip()
    if not raw_output:
        raw_output = "\n".join(value for value in (stderr_tail, diagnostic, error) if value)
    output, output_truncated = _bounded_output(raw_output)
    return {
        "kind": kind,
        "title": title,
        "summary": summary,
        "exitCode": exit_code,
        "output": output,
        "outputTruncated": bool(result.get("truncated")) or output_truncated,
    }


def _important_error_line(value: str) -> str:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    return _bounded(lines[-1] if lines else "No error details were reported.", 300)


def _bounded_output(value: str, limit: int = 12_000) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    head = value[:4_000].rstrip()
    tail = value[-7_900:].lstrip()
    return f"{head}\n\n… output truncated …\n\n{tail}", True


def _latest_tool(calls: dict[str, dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((call for call in reversed(list(calls.values())) if call.get("tool") == name), None)


def _add_changed_files(turn: dict[str, Any], paths: Any) -> None:
    if isinstance(paths, (list, tuple)):
        turn["changedFiles"].extend(str(path) for path in paths if path)


def _activity_group_summary(tools: list[dict[str, Any]]) -> str:
    failures = sum(tool.get("status") == "failed" for tool in tools)
    denied = sum(tool.get("status") == "denied" for tool in tools)
    cancelled = sum(tool.get("status") == "cancelled" for tool in tools)
    label = f"{len(tools)} tool call" + ("" if len(tools) == 1 else "s")
    suffix = []
    if failures:
        suffix.append(f"{failures} failed")
    if denied:
        suffix.append(f"{denied} denied")
    if cancelled:
        suffix.append(f"{cancelled} cancelled")
    return f"{label} · {' · '.join(suffix)}" if suffix else label


def conversation_view(events: list[SessionEvent]) -> list[dict[str, Any]]:
    roles = {"user_message": "user", "assistant_message": "assistant"}
    return [
        {
            "role": roles[event.type],
            "message": str(event.payload.get("message", "")),
            "timestamp": event.ts,
            "eventSeq": event.seq,
        }
        for event in events if event.type in roles
    ]


def activity_item(event: SessionEvent) -> dict[str, Any]:
    summary = _ACTIVITY_LABELS.get(event.type, event.type.replace("_", " ").capitalize())
    if event.type in {"tool_requested", "tool_result", "tool_denied"} and event.payload.get("name"):
        summary = f"{summary}: {event.payload['name']}"
    elif event.type in {"command_requested", "command_completed"} and event.payload.get("command"):
        summary = f"{summary}: {_bounded(str(event.payload['command']), 160)}"
    return {
        "eventSeq": event.seq,
        "timestamp": event.ts,
        "kind": event.type,
        "summary": summary,
        "detail": _bounded_payload(event.payload),
    }


def activity_summary(events: list[SessionEvent], changes: dict[str, Any]) -> dict[str, Any]:
    visible = [event for event in events if event.type not in {
        "user_message", "assistant_message", "model_usage",
        "context_condensation_attempt", "context_condensed",
    }]
    latest = activity_item(visible[-1])["summary"] if visible else "No activity yet"
    tools = [event for event in events if event.type == "tool_requested"]
    inspected = {
        str(event.payload.get("arguments", {}).get("path"))
        for event in tools
        if event.payload.get("name") == "read_file" and event.payload.get("arguments", {}).get("path")
    }
    milestones = [activity_item(event) for event in visible if event.type in {
        "edit_transaction", "edit_receipt", "command_completed", "validation_completed",
        "changes_accepted", "changes_discarded", "workspace_tainted", "recovery_required",
    }][-10:]
    return {
        "currentActivity": latest,
        "recentMilestones": milestones,
        "counters": {
            "inspectedFiles": len(inspected),
            "changedFiles": len(changes["files"]),
            "commandsExecuted": sum(event.type == "command_completed" for event in events),
        },
    }


def validation_summary(events: list[SessionEvent], meta: dict[str, Any]) -> dict[str, Any] | None:
    commands = [event for event in events if event.type == "command_completed" and event.payload.get("purpose") == "validation"]
    if not commands:
        return None
    latest = commands[-1].payload
    passed = latest.get("status") == "completed" and latest.get("exit_code") == 0
    current_revision = int(meta.get("candidate_revision", 0))
    evidence = next((event.payload for event in reversed(events)
                     if event.type == "validation_completed" and event.payload.get("execution_id") == latest.get("execution_id")), None)
    current = bool(evidence and int(evidence.get("candidate_revision", -1)) == current_revision)
    return {
        "command": latest.get("command"),
        "status": "passed" if passed else "failed",
        "exitCode": latest.get("exit_code"),
        "durationMs": latest.get("duration_ms"),
        "appliesToCurrentChanges": current,
    }


def turn_budget_view(meta: dict[str, Any], events: list[SessionEvent]) -> dict[str, Any]:
    default = int((meta.get("runtime_options") or {}).get("max_model_steps_per_user_turn", 48))
    budget = current_turn_budget(events, default)
    return {"turnId": budget.turn_id, "used": budget.used, "limit": budget.limit,
            "waiting": budget.waiting, "closed": budget.closed, "maxLimit": MAX_USER_TURN_BUDGET}


def available_actions(meta: dict[str, Any], events: list[SessionEvent], changes: dict[str, Any]) -> dict[str, bool]:
    state = meta.get("workspace_state")
    validation = validation_summary(events, meta)
    budget = turn_budget_view(meta, events)
    can_continue = bool(budget["turnId"] and not budget["closed"] and not budget["waiting"])
    safe = state in {SessionWorkspaceState.SOURCE_ONLY.value, SessionWorkspaceState.CHANGES_ACTIVE.value}
    return {
        "canRun": budget["closed"],
        "canContinue": can_continue and safe,
        "canIncreaseBudget": budget["waiting"] and budget["limit"] < MAX_USER_TURN_BUDGET and safe,
        "canStop": not budget["closed"],
        "canResolveApproval": False,
        "canViewChanges": changes["available"] and changes["state"] in {"pending", "applied"},
        "canAcceptChanges": bool(
            changes["state"] == "pending" and state == SessionWorkspaceState.CHANGES_ACTIVE.value
            and validation and validation["status"] == "passed" and validation["appliesToCurrentChanges"]
        ),
        "canDiscardChanges": changes["state"] == "pending" and state in {
            SessionWorkspaceState.CHANGES_ACTIVE.value, SessionWorkspaceState.WORKSPACE_TAINTED.value,
        },
    }


def action_reasons(
    meta: dict[str, Any], events: list[SessionEvent], changes: dict[str, Any],
    actions: dict[str, bool], validation: dict[str, Any] | None,
) -> dict[str, str]:
    state = meta.get("workspace_state")
    reasons: dict[str, str] = {}
    if not actions["canContinue"]:
        reasons["canContinue"] = "There is no unfinished request to continue."
    if not changes["available"]:
        reasons["canViewChanges"] = "There are no current pending changes."
    if not actions["canAcceptChanges"]:
        if state == SessionWorkspaceState.RECOVERY_REQUIRED.value:
            reasons["canAcceptChanges"] = "Manual recovery is required before changes can be applied."
        elif state == SessionWorkspaceState.WORKSPACE_TAINTED.value:
            reasons["canAcceptChanges"] = "Unverified changes cannot be applied safely."
        elif not changes["available"]:
            reasons["canAcceptChanges"] = "There are no pending changes to apply."
        elif not validation or not validation["appliesToCurrentChanges"] or validation["status"] != "passed":
            reasons["canAcceptChanges"] = "A successful validation command for the current revision is required before changes can be applied."
    if not actions["canDiscardChanges"]:
        reasons["canDiscardChanges"] = "There are no managed changes that can be discarded."
    return reasons


def attention_summary(meta: dict[str, Any], events: list[SessionEvent], changes: dict[str, Any]) -> str:
    state = meta.get("workspace_state")
    if state == SessionWorkspaceState.RECOVERY_REQUIRED.value:
        return "Manual recovery required"
    if state == SessionWorkspaceState.WORKSPACE_TAINTED.value:
        return "Changes need attention"
    actions = available_actions(meta, events, changes)
    if turn_budget_view(meta, events)["waiting"]:
        return "Step budget reached — increase budget or stop"
    if actions["canContinue"]:
        return "Continue available"
    validation = validation_summary(events, meta)
    if validation and validation["status"] == "failed":
        return "Validation failed"
    if changes["state"] == "pending":
        return "Changes ready for review"
    if changes["state"] == "applied":
        return "Changes applied"
    return "Ready"


def inspect_changes(store: SessionStore) -> dict[str, Any]:
    meta = store.read_meta()
    context = store.workspace_context()
    if context.workspace_kind == "git_worktree" and context.active_root.is_dir():
        try:
            proc = subprocess.run(
                ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                cwd=context.active_root, capture_output=True, check=False, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            proc = None
        if proc is not None and proc.returncode == 0:
            files = []
            for entry in proc.stdout.decode("utf-8", errors="replace").split("\0"):
                if not entry:
                    continue
                path = entry[3:] if len(entry) > 3 else entry
                if " -> " in path:
                    path = path.split(" -> ", 1)[1]
                files.append(path)
            if files:
                return {
                    "state": "pending", "available": True, "reviewable": True,
                    "files": sorted(set(files)), "workspaceState": meta.get("workspace_state"),
                }
    latest = next((event for event in reversed(store.read_events())
                   if event.type in {"changes_accepted", "changes_discarded"}), None)
    if latest is not None and latest.type == "changes_accepted" and latest.payload.get("delivery_id"):
        try:
            return AppliedChangesStore(store).summary(str(latest.payload["delivery_id"]))
        except AppliedChangesError:
            pass
    state = "discarded" if latest is not None and latest.type == "changes_discarded" else "none"
    return {"state": state, "available": False, "reviewable": False, "files": []}


def _store_from_meta(meta: dict[str, Any]) -> SessionStore:
    source = meta.get("source_workspace") or meta.get("workspace")
    return SessionStore(source, session_id=str(meta["session_id"]), session_root=meta.get("session_root")).load()


def _bounded(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _bounded_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("name", "command", "cwd", "purpose", "status", "exit_code", "duration_ms", "reason", "changed_paths"):
        if key in payload:
            value = payload[key]
            result[key] = _bounded(value, 1000) if isinstance(value, str) else value
    return result
