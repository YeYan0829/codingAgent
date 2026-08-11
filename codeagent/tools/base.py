from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from codeagent.model_gateway.base import ModelTool


class PermissionLevel(StrEnum):
    READ = "READ"
    EXEC_READONLY = "EXEC_READONLY"
    WRITE = "WRITE"
    CANDIDATE_WRITE = "CANDIDATE_WRITE"
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

    def to_model_tool(self) -> ModelTool:
        return ModelTool(name=self.name, description=self.description, parameters=self.schema)
