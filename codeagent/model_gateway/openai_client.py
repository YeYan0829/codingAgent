from __future__ import annotations

import json
from typing import Any

from openai import OpenAI

from codeagent.model_gateway.base import (
    BaseModelClient,
    LLMResponse,
    LLMToolCall,
    MalformedToolArgumentsError,
    ModelRequest,
    ModelTool,
    TokenUsage,
)


class OpenAICompatibleClient(BaseModelClient):
    """OpenAI Chat Completions 兼容 provider 的公共 transport。"""

    def __init__(self, *, model: str, api_key: str, base_url: str, provider_name: str,
                 client: Any | None = None, extra_body: dict[str, Any] | None = None,
                 reasoning_effort: str | None = None, allow_extra_body_fallback: bool = True,
                 include_tool_choice: bool = True) -> None:
        self.model = model
        self.api_key = api_key
        self.provider_name = provider_name
        self.extra_body = extra_body
        self.reasoning_effort = reasoning_effort
        self.allow_extra_body_fallback = allow_extra_body_fallback
        self.include_tool_choice = include_tool_choice
        self.client = client or OpenAI(api_key=api_key, base_url=base_url)

    def complete(self, request: ModelRequest) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": request.model or self.model,
            "messages": request.messages,
            "stream": False,
        }
        if self.include_tool_choice and request.purpose != "context_condenser":
            kwargs["tool_choice"] = request.tool_choice
        if self.extra_body is not None:
            kwargs["extra_body"] = self.extra_body
        reasoning_effort = request.reasoning_effort or self.reasoning_effort
        if reasoning_effort is not None:
            kwargs["reasoning_effort"] = reasoning_effort
        if request.tools:
            kwargs["tools"] = [_to_chat_completion_tool(tool) for tool in request.tools]
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens

        try:
            response = self.client.chat.completions.create(**kwargs)
        except TypeError:
            if "extra_body" not in kwargs or not self.allow_extra_body_fallback:
                raise
            kwargs.pop("extra_body")
            response = self.client.chat.completions.create(**kwargs)

        choice = response.choices[0]
        message = choice.message
        finish_reason = _get(choice, "finish_reason")
        if finish_reason is not None and not isinstance(finish_reason, str):
            finish_reason = str(finish_reason)
        reasoning_content = _get(message, "reasoning_content")
        if reasoning_content is not None and not isinstance(reasoning_content, str):
            reasoning_content = str(reasoning_content)
        usage = _parse_usage(getattr(response, "usage", None))
        request_id = _get(response, "id")
        try:
            tool_calls = self._parse_tool_calls(getattr(message, "tool_calls", None))
        except MalformedToolArgumentsError as exc:
            exc.usage = usage
            exc.provider_request_id = request_id
            exc.finish_reason = finish_reason
            exc.text = getattr(message, "content", None)
            exc.reasoning_content = reasoning_content
            raise
        if isinstance(tool_calls, LLMResponse):
            tool_calls.usage = usage
            tool_calls.provider_request_id = request_id
            tool_calls.finish_reason = finish_reason
            return tool_calls
        if tool_calls:
            return LLMResponse(text=getattr(message, "content", None) or None,
                               reasoning_content=reasoning_content, tool_calls=tool_calls,
                               usage=usage, provider_request_id=request_id,
                               finish_reason=finish_reason)
        return LLMResponse(text=getattr(message, "content", None) or "", reasoning_content=reasoning_content, usage=usage,
                           provider_request_id=request_id, finish_reason=finish_reason)

    def _parse_tool_calls(self, raw_tool_calls: Any) -> list[LLMToolCall] | LLMResponse:
        if not raw_tool_calls:
            return []
        parsed: list[LLMToolCall] = []
        for raw_call in raw_tool_calls:
            call_id = _get(raw_call, "id") or ""
            function = _get(raw_call, "function") or {}
            name = _get(function, "name") or ""
            raw_arguments = _get(function, "arguments") or "{}"
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                raise MalformedToolArgumentsError(name, exc.msg, exc.lineno, exc.colno, raw_arguments) from exc
            if not isinstance(arguments, dict):
                return LLMResponse(text=f"{self.provider_name} returned non-object tool arguments for {name}")
            parsed.append(LLMToolCall(call_id=call_id, name=name, arguments=arguments))
        return parsed


def _get(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _parse_usage(raw: Any) -> TokenUsage | None:
    if raw is None:
        return None
    prompt_details = _get(raw, "prompt_tokens_details")
    completion_details = _get(raw, "completion_tokens_details") or _get(raw, "output_tokens_details")
    return TokenUsage(
        input_tokens=_first_int(_get(raw, "prompt_tokens"), _get(raw, "input_tokens")),
        output_tokens=_first_int(_get(raw, "completion_tokens"), _get(raw, "output_tokens")),
        total_tokens=_first_int(_get(raw, "total_tokens")),
        cached_input_tokens=_first_int(
            _get(raw, "prompt_cache_hit_tokens"), _get(prompt_details, "cached_tokens")
        ),
        cache_miss_input_tokens=_first_int(_get(raw, "prompt_cache_miss_tokens")),
        reasoning_tokens=_first_int(_get(raw, "reasoning_tokens"), _get(completion_details, "reasoning_tokens")),
    )


def _first_int(*values: Any) -> int | None:
    return next((value for value in values if isinstance(value, int) and not isinstance(value, bool)), None)


def _to_chat_completion_tool(tool: ModelTool) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }
