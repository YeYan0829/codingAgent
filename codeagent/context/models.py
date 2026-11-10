from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from codeagent.config import DEFAULT_MODEL_LIMITS


TOKEN_COUNT_MARGIN = 2_000


@dataclass(frozen=True)
class ModelCapabilities:
    """本次请求的模型容量；不携带历史策略或第二套摘要预算。"""
    context_limit: int = DEFAULT_MODEL_LIMITS.context_tokens
    max_output_tokens: int = DEFAULT_MODEL_LIMITS.default_output_tokens

    @property
    def usable_input_budget(self) -> int:
        return self.context_limit - self.max_output_tokens - TOKEN_COUNT_MARGIN


@dataclass
class ToolExchange:
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    source_seq: int | None = None
    source_event_id: str | None = None
    terminal_kind: str | None = None
    terminal_source_seq: int | None = None
    terminal_source_event_id: str | None = None
    result: dict[str, Any] | None = None


@dataclass
class ModelStepView:
    step_id: str
    source_seq: int | None = None
    source_event_id: str | None = None
    atom_id: str | None = None
    turn_id: str | None = None
    message: str = ""
    reasoning_content: str | None = None
    exchanges: list[ToolExchange] = field(default_factory=list)
    protocol_error: dict[str, Any] | None = None
    output_truncated: bool = False

    @property
    def closed(self) -> bool:
        """主模型响应已获得全部 terminal outcome，或本身无需执行工具。"""
        return not self.exchanges or all(exchange.terminal_kind is not None for exchange in self.exchanges)


@dataclass
class UserTurnView:
    turn_id: str
    user_message: str
    source_seq: int | None = None
    source_event_id: str | None = None
    model_steps: list[ModelStepView] = field(default_factory=list)
    final_message: str | None = None
    final_reasoning_content: str | None = None

    @property
    def completed(self) -> bool:
        return self.final_message is not None


@dataclass(frozen=True)
class ContextHistoryItem:
    source_seq: int
    source_event_id: str
    source_type: str
    turn_id: str | None
    model_step_id: str | None
    atom_id: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ManipulationAtom:
    atom_id: str
    items: tuple[ContextHistoryItem, ...]
    closed: bool
    reasoning_content: str | None = None

    @property
    def first_seq(self) -> int:
        return self.items[0].source_seq

    @property
    def last_seq(self) -> int:
        return self.items[-1].source_seq

    @property
    def source_event_ids(self) -> tuple[str, ...]:
        return tuple(item.source_event_id for item in self.items)


@dataclass(frozen=True)
class ContextProjection:
    turns: tuple[UserTurnView, ...]
    items: tuple[ContextHistoryItem, ...]
    atoms: tuple[ManipulationAtom, ...]


@dataclass(frozen=True)
class RollingSummary:
    event_seq: int
    event_id: str
    previous_summary_event_id: str | None
    covered_event_ids: tuple[str, ...]
    logical_covered_event_count: int
    logical_frontier_source_event_id: str
    summary: str


@dataclass(frozen=True)
class ContextState:
    active_workspace: str
    environment: dict[str, Any]
    task_plan: dict[str, Any] | None = None


@dataclass(frozen=True)
class BudgetReport:
    estimated_tokens: int
    usable_tokens: int
    dropped_turn_ids: tuple[str, ...] = ()
    reductions: tuple[str, ...] = ()
    minimum_set_components: tuple["BudgetComponent", ...] = ()
    largest_observations: tuple["BudgetObservation", ...] = ()
    component_estimated_tokens: int | None = None
    estimation_residual: int | None = None
    fixed_request_tokens: int = 0
    available_history_budget: int = 0
    history_tokens: int = 0
    uncovered_context_event_count: int = 0
    chain_tip_event_id: str | None = None
    anchor_source_event_id: str | None = None
    retained_raw_source_event_ids: tuple[str, ...] = ()
    mandatory_protocol_atom_id: str | None = None


@dataclass(frozen=True)
class BudgetComponent:
    """最低 Context 集合中一类输入的有界计量，不保存输入正文。"""

    category: str
    estimated_tokens: int
    item_count: int


@dataclass(frozen=True)
class BudgetObservation:
    """占用较大的工具 Observation 来源摘要。"""

    tool: str
    call_id: str
    estimated_tokens: int
    path: str | None = None


@dataclass(frozen=True)
class CondensationPlan:
    plan_id: str
    previous_summary_event_id: str | None
    previous_logical_covered_event_count: int
    source_event_ids: tuple[str, ...]
    source_atoms: tuple[ManipulationAtom, ...]
    source_messages: tuple[dict[str, Any], ...]
    candidate_source_event_ids_sha256: str
    turn_id: str | None
    trigger: tuple[str, ...]
    hard: bool
    input_estimated_tokens: int
    request_estimated_tokens_before: int
    desired_source_atom_count: int


@dataclass(frozen=True)
class ReadyContext:
    messages: list[dict[str, Any]]
    budget_report: BudgetReport

    def __iter__(self):
        return iter(self.messages)

    def __getitem__(self, index):
        return self.messages[index]

    def __len__(self) -> int:
        return len(self.messages)


@dataclass(frozen=True)
class CondensationRequired:
    plan: CondensationPlan
    trigger: tuple[str, ...]
    budget_report: BudgetReport


class ContextBudgetExceeded(RuntimeError):
    def __init__(self, report: BudgetReport, reason: str = "minimum_set") -> None:
        super().__init__(f"context cannot fit safely ({reason}): {report.estimated_tokens} > {report.usable_tokens}")
        self.report = report
        self.reason = reason


ContextBuildResult = ReadyContext | CondensationRequired | ContextBudgetExceeded
CondensationAttemptStatus = Literal["request_failed", "invalid_completion", "valid_completion"]
