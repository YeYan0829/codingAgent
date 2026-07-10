from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class LLMToolCall(BaseModel):
    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    text: str | None = None
    tool_calls: list[LLMToolCall] = Field(default_factory=list)


class ModelRequest(BaseModel):
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = Field(default_factory=list)
    tool_choice: str | dict[str, Any] = "auto"
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None


class BaseModelClient(ABC):
    @abstractmethod
    def complete(self, request: ModelRequest) -> LLMResponse:
        """根据上下文和工具定义返回文本或工具请求。"""
