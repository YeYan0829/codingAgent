from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class PermissionLevel(StrEnum):
    READ = "READ"
    EXEC_READONLY = "EXEC_READONLY"
    WRITE = "WRITE"
    NETWORK = "NETWORK"
    DANGEROUS = "DANGEROUS"


class ToolResult(BaseModel):
    ok: bool
    content: str = ""
    error: str | None = None
    truncated: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


ToolHandler = Callable[[dict[str, Any]], ToolResult]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    permission_level: PermissionLevel
    schema: dict[str, Any]
    handler: ToolHandler
