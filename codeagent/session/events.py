from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class SessionEvent(BaseModel):
    seq: int | None = None
    turn_id: str | None = None
    model_step_id: str | None = None
    ts: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
