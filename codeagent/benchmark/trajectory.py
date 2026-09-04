from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from codeagent.session.events import SessionEvent


TOOL_GROUPS = {
    "search_text": "search", "find_files": "search", "read_file": "read",
    "apply_workspace_edit": "edit", "run_command": "command",
    "git_status": "git_read", "git_diff": "git_read", "git_diff_stat": "git_read",
}


def load_events(path: str | Path) -> list[SessionEvent]:
    events = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(SessionEvent.model_validate_json(line))
    return events


def analyze_trajectory(events: Iterable[SessionEvent], result: dict[str, Any] | None = None) -> dict[str, Any]:
    values = list(events)
    step = 0
    call_step: dict[str, int] = {}
    tool_counts: dict[str, int] = {}
    effective_edits: list[int] = []
    validation_steps: list[int] = []
    successful_validations: list[int] = []
    repeated_reads = 0
    repeated_commands = 0
    read_seen: dict[tuple[Any, ...], int] = {}
    command_seen: dict[str, int] = {}
    usage_by_step: dict[int, dict[str, Any] | None] = {}
    pending_usage: list[dict[str, Any] | None] = []
    revision = 0

    for event in values:
        payload = event.payload
        if event.type == "model_usage":
            pending_usage.append(payload.get("usage") if isinstance(payload.get("usage"), dict) else None)
        elif event.type in {"assistant_tool_calls", "assistant_message", "model_protocol_error"}:
            step += 1
            if pending_usage:
                usage_by_step[step] = pending_usage.pop(0)
            if event.type == "assistant_tool_calls":
                for call in payload.get("tool_calls", []):
                    if isinstance(call, dict):
                        call_step[str(call.get("call_id", ""))] = step
        elif event.type == "tool_result":
            name = str(payload.get("name", "unknown"))
            group = TOOL_GROUPS.get(name, "other")
            tool_counts[group] = tool_counts.get(group, 0) + 1
            current = call_step.get(str(payload.get("call_id", "")), step)
            args = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
            result_value = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            metadata = result_value.get("metadata") if isinstance(result_value.get("metadata"), dict) else {}
            if group == "read" and result_value.get("ok") is True:
                returned = metadata.get("returned_range") if isinstance(metadata.get("returned_range"), dict) else {}
                digest = metadata.get("sha256") or metadata.get("file_sha256")
                key = (metadata.get("path") or args.get("path"),
                       returned.get("start_line", metadata.get("start_line", args.get("start_line", 1))),
                       returned.get("end_line", metadata.get("end_line", args.get("end_line"))), digest)
                if digest is not None and key in read_seen:
                    repeated_reads += 1
                elif digest is not None:
                    read_seen[key] = current
            if group == "command":
                normalized = " ".join(str(args.get("command", "")).split())
                if normalized:
                    if normalized in command_seen:
                        repeated_commands += 1
                    else:
                        command_seen[normalized] = current
                if args.get("purpose") == "validation":
                    validation_steps.append(current)
                    if result_value.get("ok") is True:
                        if current not in successful_validations:
                            successful_validations.append(current)
        elif event.type == "edit_transaction":
            candidate = payload.get("candidate_revision")
            if isinstance(candidate, int) and candidate > revision:
                revision = candidate
                effective_edits.append(_event_step(event, call_step, step))
        elif event.type == "command_completed":
            before, after = payload.get("before_candidate_revision"), payload.get("after_candidate_revision")
            if isinstance(before, int) and isinstance(after, int) and after > before:
                revision = max(revision, after)
                effective_edits.append(_event_step(event, call_step, step))
        elif event.type == "validation_completed":
            current = _event_step(event, call_step, step)
            if current not in successful_validations:
                successful_validations.append(current)

    termination = _termination(values, result)
    last_edit = max(effective_edits) if effective_edits else None
    last_validation = max(validation_steps) if validation_steps else None
    last_success = max(successful_validations) if successful_validations else None
    return {
        "schema_version": 1,
        "model_steps": step,
        "termination_reason": termination,
        "completed": termination == "completed",
        "context_exceeded": termination == "context_budget_exceeded",
        "step_exhausted": termination == "model_step_budget_exhausted",
        "tool_usage": {name: tool_counts.get(name, 0) for name in (*sorted(set(TOOL_GROUPS.values())), "other")},
        "first_edit_step": min(effective_edits) if effective_edits else None,
        "last_edit_step": last_edit,
        "edit_count": len(effective_edits),
        "first_nonempty_patch_step": min(effective_edits) if effective_edits else None,
        "post_last_edit_steps": step - last_edit if last_edit is not None else None,
        "post_last_edit_token_usage": _usage_after(usage_by_step, last_edit),
        "validation_count": len(validation_steps),
        "validation_success_count": len(successful_validations),
        "validation_failure_count": max(0, len(validation_steps) - len(successful_validations)),
        "last_validation_step": last_validation,
        "last_successful_validation_step": last_success,
        "post_successful_validation_steps": step - last_success if last_success is not None else None,
        "edit_after_successful_validation": bool(last_success is not None and any(x > last_success for x in effective_edits)),
        "exact_same_revision_read_count": repeated_reads,
        "normalized_identical_command_repeat_count": repeated_commands,
        "context_detail": _context_detail(values),
    }


def save_trajectory(path: str | Path, summary: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _event_step(event: SessionEvent, calls: dict[str, int], fallback: int) -> int:
    call_id = str(event.payload.get("call_id", ""))
    return calls.get(call_id, fallback)


def _usage_after(by_step: dict[int, dict[str, Any] | None], after: int | None) -> dict[str, Any] | None:
    if after is None:
        return None
    selected = [value for index, value in by_step.items() if index > after]
    if not selected:
        return {"requests": 0, "requests_with_usage": 0, "total_tokens": 0}
    known = [x for x in selected if isinstance(x, dict)]
    totals = [x.get("total_tokens") for x in known if isinstance(x.get("total_tokens"), int)]
    return {"requests": len(selected), "requests_with_usage": len(known),
            "total_tokens": sum(totals) if len(totals) == len(selected) else None}


def _termination(events: list[SessionEvent], result: dict[str, Any] | None) -> str:
    terminated = next((x for x in reversed(events) if x.type == "turn_terminated"), None)
    if terminated:
        return str(terminated.payload.get("reason", "terminated"))
    if result and result.get("agent_status"):
        return str(result["agent_status"])
    return "completed" if any(x.type == "assistant_message" for x in events) else "unknown"


def _context_detail(events: list[SessionEvent]) -> dict[str, Any] | None:
    item = next((x for x in reversed(events) if x.type == "turn_terminated" and
                 x.payload.get("reason") == "context_budget_exceeded"), None)
    if not item:
        return None
    components = item.payload.get("minimum_set_components")
    available = isinstance(components, list) and bool(components)
    return {"estimated_tokens": item.payload.get("estimated_tokens"),
            "usable_tokens": item.payload.get("usable_tokens"),
            "reductions": item.payload.get("reductions"),
            "minimum_set_composition": components if available else None,
            "largest_observations": item.payload.get("largest_observations") if available else None,
            "component_estimated_tokens": item.payload.get("component_estimated_tokens") if available else None,
            "estimation_residual": item.payload.get("estimation_residual") if available else None,
            "unavailable_reason": None if available else "历史事件未记录最低集合的分项组成"}
