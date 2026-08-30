from __future__ import annotations

import json

from codeagent.context.models import ModelStepView, ToolExchange, UserTurnView
from codeagent.session.events import SessionEvent


class ProjectionError(RuntimeError):
    pass


def project_events(events: list[SessionEvent]) -> list[UserTurnView]:
    """兼容新 identity 与旧 JSONL 行序，机械恢复 conversation/tool protocol。"""
    turns: list[UserTurnView] = []
    current: UserTurnView | None = None
    current_step: ModelStepView | None = None
    calls: dict[str, ToolExchange] = {}
    for line, event in enumerate(events, 1):
        seq = event.seq or line
        if event.type == "user_message":
            current = UserTurnView(event.turn_id or f"legacy-turn-{seq}", str(event.payload.get("message", "")))
            turns.append(current)
            current_step = None
            calls = {}
            continue
        if current is None:
            continue
        if event.type == "assistant_tool_calls":
            current_step = ModelStepView(event.model_step_id or f"legacy-step-{seq}", str(event.payload.get("message") or ""))
            current.model_steps.append(current_step)
            for item in event.payload.get("tool_calls", []):
                call_id = str(item.get("call_id", ""))
                if not call_id or call_id in calls:
                    raise ProjectionError(f"invalid or duplicate call_id at event {seq}")
                exchange = ToolExchange(call_id, str(item.get("name", "")), dict(item.get("arguments") or {}))
                current_step.exchanges.append(exchange)
                calls[call_id] = exchange
        elif event.type in {"tool_result", "tool_denied"}:
            call_id = str(event.payload.get("call_id", ""))
            exchange = calls.get(call_id)
            if exchange is None:
                raise ProjectionError(f"unknown call_id {call_id!r} at event {seq}")
            if exchange.terminal_kind is not None:
                raise ProjectionError(f"duplicate terminal outcome for {call_id}")
            exchange.terminal_kind = "result" if event.type == "tool_result" else "denied"
            result = event.payload.get("result")
            if not isinstance(result, dict):
                if event.type == "tool_result":
                    try:
                        parsed = json.loads(str(event.payload.get("content") or "{}"))
                    except json.JSONDecodeError:
                        parsed = {"ok": True, "content": str(event.payload.get("content") or "")}
                    result = parsed if isinstance(parsed, dict) else {"ok": True, "content": str(parsed)}
                else:
                    result = {"ok": False, "error": event.payload.get("reason", "tool denied"), "error_code": "tool_denied"}
            exchange.result = result
        elif event.type == "model_protocol_error":
            current_step = ModelStepView(event.model_step_id or f"legacy-step-{seq}", protocol_error=dict(event.payload))
            current.model_steps.append(current_step)
        elif event.type == "assistant_message":
            current.model_steps.append(ModelStepView(event.model_step_id or f"legacy-step-{seq}",
                                                     message=str(event.payload.get("message", ""))))
            current.final_message = str(event.payload.get("message", ""))
            current_step = None
        elif event.type == "turn_terminated":
            current.final_message = str(event.payload.get("message", "UserTurn terminated"))
            current_step = None
    return turns
