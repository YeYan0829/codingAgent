from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class LLMToolCall(BaseModel):
    call_id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class TokenUsage(BaseModel):
    """Provider 返回的单次请求 token 计量；未知字段保持为 None。"""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_miss_input_tokens: int | None = None
    reasoning_tokens: int | None = None


class LLMResponse(BaseModel):
    text: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[LLMToolCall] = Field(default_factory=list)
    usage: TokenUsage | None = None
    provider_request_id: str | None = None
    finish_reason: str | None = None


class ModelTool(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class ModelRequest(BaseModel):
    messages: list[dict[str, Any]]
    tools: list[ModelTool] = Field(default_factory=list)
    tool_choice: str | dict[str, Any] = "auto"
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    purpose: str = "main_agent"
    reasoning_effort: str | None = None


class BaseModelClient(ABC):
    @abstractmethod
    def complete(self, request: ModelRequest) -> LLMResponse:
        """根据上下文和工具定义返回文本或工具请求。"""


class MalformedToolArgumentsError(ValueError):
    def __init__(self, tool_name: str, message: str, line: int, column: int, raw_arguments: str,
                 *, usage: TokenUsage | None = None, provider_request_id: str | None = None,
                 finish_reason: str | None = None, text: str | None = None,
                 reasoning_content: str | None = None) -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.message = message
        self.line = line
        self.column = column
        self.raw_arguments = raw_arguments
        self.usage = usage
        self.provider_request_id = provider_request_id
        self.finish_reason = finish_reason
        self.text = text
        self.reasoning_content = reasoning_content
