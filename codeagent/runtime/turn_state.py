"""从历史投影当前用户轮次；开销计数不参与运行准入。"""
from dataclasses import dataclass
from codeagent.session.events import SessionEvent
from codeagent.session.store import SessionStore


@dataclass(frozen=True)
class TurnState:
    turn_id: str | None
    used: int
    closed: bool


def current_turn_state(events: list[SessionEvent]) -> TurnState:
    start = next((i for i in range(len(events) - 1, -1, -1) if events[i].type == "user_message"), None)
    if start is None:
        return TurnState(None, 0, True)
    turn = events[start:]
    used = sum(event.type in {"assistant_tool_calls", "assistant_message", "model_protocol_error",
                              "model_output_truncated", "task_plan_final_deferred"} for event in turn)
    activation = next((e for e in turn if e.type == "request_executor_started"), None)
    if activation:
        used = int(activation.payload.get("legacy_steps", 0)) + sum(e.type == "model_request_reserved" for e in turn)
    return TurnState(turn[0].turn_id, used, any(e.type in {"assistant_message", "turn_terminated"} for e in turn))


def store_turn_state(store: SessionStore) -> TurnState:
    return current_turn_state(store.read_events())
