from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ModelCapabilities:
    context_limit: int = 32_000
    generation_reserve: int = 4_000
    continuation_reserve: int = 4_000
    safety_margin: int = 2_000
    preferred_recent_raw_steps: int = 4

    @property
    def usable_input_budget(self) -> int:
        return self.context_limit - self.generation_reserve - self.continuation_reserve - self.safety_margin


@dataclass
class ToolExchange:
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    terminal_kind: str | None = None
    result: dict[str, Any] | None = None


@dataclass
class ModelStepView:
    step_id: str
    message: str = ""
    reasoning_content: str | None = None
    exchanges: list[ToolExchange] = field(default_factory=list)
    protocol_error: dict[str, Any] | None = None

    @property
    def closed(self) -> bool:
        """主模型响应已获得全部 terminal outcome，或本身无需执行工具。"""
        return not self.exchanges or all(exchange.terminal_kind is not None for exchange in self.exchanges)


@dataclass
class UserTurnView:
    turn_id: str
    user_message: str
    model_steps: list[ModelStepView] = field(default_factory=list)
    final_message: str | None = None
    final_reasoning_content: str | None = None

    @property
    def completed(self) -> bool:
        return self.final_message is not None


@dataclass(frozen=True)
class RuntimeSnapshot:
    workspace_state: str
    workspace_revision: int
    candidate_revision: int
    workspace_kind: str
    active_workspace: str
    baseline_workspace: str
    base_commit: str | None
    subject_tree: str | None
    changed_paths: tuple[dict[str, str], ...]
    validation_state: str
    environment: dict[str, Any]


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


class ContextBudgetExceeded(RuntimeError):
    def __init__(self, report: BudgetReport) -> None:
        super().__init__(f"context minimum set exceeds budget: {report.estimated_tokens} > {report.usable_tokens}")
        self.report = report
