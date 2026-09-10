from __future__ import annotations

import json
from collections import OrderedDict

from codeagent.context.models import (
    ContextHistoryItem,
    ContextProjection,
    ManipulationAtom,
    ModelStepView,
    ToolExchange,
    UserTurnView,
)
from codeagent.session.events import SessionEvent


CCES_EVENT_TYPES = frozenset({
    "user_message",
    "assistant_tool_calls",
    "tool_result",
    "tool_denied",
    "assistant_message",
    "turn_terminated",
})


class ProjectionError(RuntimeError):
    pass


class ContextNotReady(ProjectionError):
    """最新主模型响应仍在等待 Runtime 完成工具结果或原子恢复。"""


def event_id(seq: int) -> str:
    return f"seq:{seq}"


def _require_closed(step: ModelStepView | None, seq: int) -> None:
    if step is not None and not step.closed:
        raise ContextNotReady(f"open ModelStep {step.step_id!r} before event {seq}; Runtime recovery required")


def _result_payload(event: SessionEvent) -> dict:
    result = event.payload.get("result")
    if isinstance(result, dict):
        return dict(result)
    if event.type == "tool_denied":
        return {
            "ok": False,
            "error": event.payload.get("reason", "tool denied"),
            "error_code": "tool_denied",
        }
    try:
        parsed = json.loads(str(event.payload.get("content") or "{}"))
    except json.JSONDecodeError:
        parsed = {"ok": True, "content": str(event.payload.get("content") or "")}
    return parsed if isinstance(parsed, dict) else {"ok": True, "content": str(parsed)}


def project_events(events: list[SessionEvent]) -> list[UserTurnView]:
    """兼容新 identity 与旧 JSONL 行序，机械恢复 conversation/tool protocol。"""
    turns: list[UserTurnView] = []
    current: UserTurnView | None = None
    current_step: ModelStepView | None = None
    calls: dict[str, ToolExchange] = {}
    for line, event in enumerate(events, 1):
        seq = event.seq or line
        source_id = event_id(seq)
        if event.type == "user_message":
            _require_closed(current_step, seq)
            current = UserTurnView(
                turn_id=event.turn_id or f"legacy-turn-{seq}",
                user_message=str(event.payload.get("message", "")),
                source_seq=seq,
                source_event_id=source_id,
            )
            turns.append(current)
            current_step = None
            calls = {}
            continue
        if current is None:
            continue
        if event.type == "assistant_tool_calls":
            _require_closed(current_step, seq)
            step_id = event.model_step_id or f"legacy-step-{seq}"
            current_step = ModelStepView(
                step_id=step_id,
                source_seq=seq,
                source_event_id=source_id,
                atom_id=f"model-step:{step_id}",
                turn_id=current.turn_id,
                message=str(event.payload.get("message") or ""),
                reasoning_content=(
                    str(event.payload["reasoning_content"])
                    if event.payload.get("reasoning_content") is not None else None
                ),
            )
            current.model_steps.append(current_step)
            for item in event.payload.get("tool_calls", []):
                call_id = str(item.get("call_id", ""))
                if not call_id or call_id in calls:
                    raise ProjectionError(f"invalid or duplicate call_id at event {seq}")
                exchange = ToolExchange(
                    call_id=call_id,
                    tool_name=str(item.get("name", "")),
                    arguments=dict(item.get("arguments") or {}),
                    source_seq=seq,
                    source_event_id=source_id,
                )
                current_step.exchanges.append(exchange)
                calls[call_id] = exchange
        elif event.type in {"tool_result", "tool_denied"}:
            call_id = str(event.payload.get("call_id", ""))
            exchange = calls.get(call_id)
            if exchange is None:
                raise ProjectionError(f"unknown call_id {call_id!r} at event {seq}")
            if exchange.terminal_kind is not None:
                raise ProjectionError(f"duplicate terminal outcome for {call_id}")
            if event.model_step_id and current_step and event.model_step_id != current_step.step_id:
                raise ProjectionError(f"terminal outcome for {call_id} has mismatched model_step_id at event {seq}")
            exchange.terminal_kind = "result" if event.type == "tool_result" else "denied"
            exchange.terminal_source_seq = seq
            exchange.terminal_source_event_id = source_id
            exchange.result = _result_payload(event)
        elif event.type == "model_protocol_error":
            _require_closed(current_step, seq)
            step_id = event.model_step_id or f"legacy-step-{seq}"
            current_step = ModelStepView(
                step_id=step_id, source_seq=seq, source_event_id=source_id,
                atom_id=f"protocol:{source_id}", turn_id=current.turn_id,
                protocol_error=dict(event.payload),
            )
            current.model_steps.append(current_step)
        elif event.type == "model_output_truncated":
            _require_closed(current_step, seq)
            step_id = event.model_step_id or f"legacy-step-{seq}"
            reasoning = event.payload.get("reasoning_content")
            current_step = ModelStepView(
                step_id=step_id, source_seq=seq, source_event_id=source_id,
                atom_id=f"truncation:{source_id}", turn_id=current.turn_id,
                message=str(event.payload.get("message") or ""),
                reasoning_content=str(reasoning) if reasoning is not None else None,
                output_truncated=True,
            )
            current.model_steps.append(current_step)
        elif event.type == "assistant_message":
            _require_closed(current_step, seq)
            reasoning = event.payload.get("reasoning_content")
            current.model_steps.append(ModelStepView(
                step_id=event.model_step_id or f"legacy-step-{seq}",
                source_seq=seq, source_event_id=source_id, atom_id=f"event:{source_id}",
                turn_id=current.turn_id, message=str(event.payload.get("message", "")),
                reasoning_content=str(reasoning) if reasoning is not None else None,
            ))
            current.final_message = str(event.payload.get("message", ""))
            current.final_reasoning_content = str(reasoning) if reasoning is not None else None
            current_step = None
        elif event.type == "turn_terminated":
            _require_closed(current_step, seq)
            current.final_message = str(event.payload.get("message", "UserTurn terminated"))
            current_step = None
    return turns


def project_context_events(events: list[SessionEvent]) -> ContextProjection:
    """生成严格 allowlist 的 CCES，并为可操作历史建立不可拆分 atom。"""
    turns = project_events(events)
    normalized: list[tuple[int, SessionEvent]] = [
        (event.seq or line, event) for line, event in enumerate(events, 1)
    ]
    step_calls: dict[str, set[str]] = {}
    step_terminals: dict[str, set[str]] = {}
    step_reasoning: dict[str, str | None] = {}
    call_step: dict[str, str] = {}
    for seq, event in normalized:
        if event.type == "assistant_tool_calls":
            step_id = event.model_step_id or f"legacy-step-{seq}"
            calls = {str(item.get("call_id", "")) for item in event.payload.get("tool_calls", [])}
            step_calls[step_id] = calls
            step_terminals.setdefault(step_id, set())
            value = event.payload.get("reasoning_content")
            step_reasoning[step_id] = str(value) if value is not None else None
            for call_id in calls:
                call_step[call_id] = step_id
        elif event.type in {"tool_result", "tool_denied"}:
            call_id = str(event.payload.get("call_id", ""))
            step_id = event.model_step_id or call_step.get(call_id)
            if step_id:
                step_terminals.setdefault(step_id, set()).add(call_id)

    item_groups: OrderedDict[str, list[ContextHistoryItem]] = OrderedDict()
    atom_closed: dict[str, bool] = {}
    atom_reasoning: dict[str, str | None] = {}
    for seq, event in normalized:
        if event.type not in CCES_EVENT_TYPES:
            continue
        source_id = event_id(seq)
        payload: dict = {}
        if event.type == "user_message":
            payload = {"message": str(event.payload.get("message", ""))}
            atom_id = f"event:{source_id}"
        elif event.type == "assistant_tool_calls":
            payload = {
                "message": str(event.payload.get("message") or ""),
                "tool_calls": [
                    {
                        "call_id": str(item.get("call_id", "")),
                        "name": str(item.get("name", "")),
                        "arguments": dict(item.get("arguments") or {}),
                    }
                    for item in event.payload.get("tool_calls", [])
                ],
            }
            step_id = event.model_step_id or f"legacy-step-{seq}"
            atom_id = f"model-step:{step_id}"
            atom_closed[atom_id] = bool(step_calls.get(step_id)) and (
                step_calls[step_id] == step_terminals.get(step_id, set())
            )
            atom_reasoning[atom_id] = step_reasoning.get(step_id)
        elif event.type in {"tool_result", "tool_denied"}:
            call_id = str(event.payload.get("call_id", ""))
            step_id = event.model_step_id or call_step.get(call_id) or f"orphan-{seq}"
            atom_id = f"model-step:{step_id}"
            payload = {
                "call_id": call_id,
                "name": str(event.payload.get("name", "")),
                "terminal_kind": "result" if event.type == "tool_result" else "denied",
                "result": _result_payload(event),
            }
        elif event.type == "assistant_message":
            payload = {"message": str(event.payload.get("message", ""))}
            atom_id = f"event:{source_id}"
        else:
            payload = {
                "message": str(event.payload.get("message", "UserTurn terminated")),
                "reason": event.payload.get("reason"),
            }
            atom_id = f"event:{source_id}"
        item = ContextHistoryItem(
            source_seq=seq,
            source_event_id=source_id,
            source_type=event.type,
            turn_id=event.turn_id,
            model_step_id=event.model_step_id,
            atom_id=atom_id,
            payload=payload,
        )
        item_groups.setdefault(atom_id, []).append(item)
        atom_closed.setdefault(atom_id, True)

    atoms = tuple(
        ManipulationAtom(
            atom_id=atom_id,
            items=tuple(items),
            closed=atom_closed[atom_id],
            reasoning_content=atom_reasoning.get(atom_id),
        )
        for atom_id, items in item_groups.items()
    )
    items = tuple(item for atom in atoms for item in atom.items)
    return ContextProjection(tuple(turns), items, atoms)
