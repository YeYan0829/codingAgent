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


class BaseModelClient(ABC):
    @abstractmethod
    def complete(self, messages: list[dict[str, Any]]) -> LLMResponse:
        """根据上下文返回文本或工具请求。"""
