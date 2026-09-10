from __future__ import annotations

from dataclasses import dataclass

from codeagent.session.events import SessionEvent
from codeagent.session.store import SessionStore
from codeagent.workspace.workspace import SessionWorkspaceState


MAX_USER_TURN_BUDGET = 1000


@dataclass(frozen=True)
class TurnBudget:
    turn_id: str | None
    used: int
    limit: int
    closed: bool
    waiting: bool


def current_turn_budget(events: list[SessionEvent], default_limit: int = 48) -> TurnBudget:
    """从本轮事件投影预算；扩额不修改 Session 默认配置，也不重置计数。"""
    start = next((i for i in range(len(events) - 1, -1, -1) if events[i].type == "user_message"), None)
    if start is None:
        return TurnBudget(None, 0, default_limit, True, False)
    turn = events[start:]
    first = turn[0]
    limit = int(first.payload.get("max_model_steps_per_user_turn", default_limit))
    for event in turn:
        if event.type == "turn_budget_exhausted":
            limit = int(event.payload["max_model_steps_per_user_turn"])
        elif event.type == "turn_budget_increased":
            limit = int(event.payload["new_limit"])
    used = sum(event.type in {
        "assistant_tool_calls", "assistant_message", "model_protocol_error", "model_output_truncated",
    } for event in turn)
    closed = any(event.type in {"assistant_message", "turn_terminated"} for event in turn)
    waiting = not closed and used >= limit
    return TurnBudget(first.turn_id, used, limit, closed, waiting)


def store_turn_budget(store: SessionStore) -> TurnBudget:
    options = store.read_meta().get("runtime_options") or {}
    return current_turn_budget(store.read_events(), int(options.get("max_model_steps_per_user_turn", 48)))


def increase_turn_budget(
    store: SessionStore,
    *,
    turn_id: str,
    expected_limit: int,
    new_limit: int | None = None,
    additional_steps: int | None = None,
) -> TurnBudget:
    """仅用户控制入口调用；产品调用方须持有 Session 执行互斥锁。"""
    budget = store_turn_budget(store)
    if store.workspace_state() not in {SessionWorkspaceState.SOURCE_ONLY, SessionWorkspaceState.CHANGES_ACTIVE}:
        raise ValueError("workspace requires recovery before increasing the budget")
    if not turn_id or budget.turn_id != turn_id or budget.closed or not budget.waiting:
        raise ValueError("current UserTurn is not waiting for a budget increase")
    if type(expected_limit) is not int or expected_limit != budget.limit:
        raise ValueError("budget changed; refresh before increasing it")
    if (new_limit is None) == (additional_steps is None):
        raise ValueError("provide exactly one of new limit or additional steps")
    if additional_steps is not None:
        if type(additional_steps) is not int or additional_steps <= 0:
            raise ValueError("additional steps must be a positive integer")
        resolved_limit = budget.limit + additional_steps
        mode = "additional_steps"
    else:
        if type(new_limit) is not int:
            raise ValueError("new limit must be an integer")
        resolved_limit = new_limit
        mode = "total_limit"
    if not budget.limit < resolved_limit <= MAX_USER_TURN_BUDGET:
        raise ValueError(f"new limit must be greater than {budget.limit} and at most {MAX_USER_TURN_BUDGET}")
    store.append_event("turn_budget_increased", {
        "previous_limit": budget.limit, "new_limit": resolved_limit,
        "additional_steps": resolved_limit - budget.limit,
        "mode": mode, "steps_used_in_turn": budget.used, "source": "user",
    })
    return store_turn_budget(store)
