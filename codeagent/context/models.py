from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ModelCapabilities:
    context_limit: int = 32_000
    generation_reserve: int = 4_000
    continuation_reserve: int = 4_000
    safety_margin: int = 2_000
    active_code_budget: int = 4_000

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
    exchanges: list[ToolExchange] = field(default_factory=list)
    protocol_error: dict[str, Any] | None = None


@dataclass
class UserTurnView:
    turn_id: str
    user_message: str
    model_steps: list[ModelStepView] = field(default_factory=list)
    final_message: str | None = None

    @property
    def completed(self) -> bool:
        return self.final_message is not None


@dataclass(frozen=True)
class RuntimeSnapshot:
    workspace_state: str
    workspace_revision: int
    candidate_revision: int
    base_commit: str | None
    subject_tree: str | None
    changed_paths: tuple[dict[str, str], ...]
    validation_state: str
    environment: dict[str, Any]


@dataclass(frozen=True)
class ActiveCodeSlice:
    path: str
    start_line: int
    end_line: int
    content: str
    reason: str
    range_provenance: str


@dataclass(frozen=True)
class BudgetReport:
    estimated_tokens: int
    usable_tokens: int
    dropped_turn_ids: tuple[str, ...] = ()
    reductions: tuple[str, ...] = ()


class ContextBudgetExceeded(RuntimeError):
    def __init__(self, report: BudgetReport) -> None:
        super().__init__(f"context minimum set exceeds budget: {report.estimated_tokens} > {report.usable_tokens}")
        self.report = report
