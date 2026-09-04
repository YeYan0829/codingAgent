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
                 client: Any | None = None, extra_body: dict[str, Any] | None = None) -> None:
        self.model = model
        self.api_key = api_key
        self.provider_name = provider_name
        self.extra_body = extra_body
        self.client = client or OpenAI(api_key=api_key, base_url=base_url)

    def complete(self, request: ModelRequest) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": request.model or self.model,
            "messages": request.messages,
            "tool_choice": request.tool_choice,
            "stream": False,
        }
        if self.extra_body is not None:
            kwargs["extra_body"] = self.extra_body
        if request.tools:
            kwargs["tools"] = [_to_chat_completion_tool(tool) for tool in request.tools]
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens

        try:
            response = self.client.chat.completions.create(**kwargs)
        except TypeError:
            if "extra_body" not in kwargs:
                raise
            kwargs.pop("extra_body")
            response = self.client.chat.completions.create(**kwargs)

        message = response.choices[0].message
        usage = _parse_usage(getattr(response, "usage", None))
        request_id = _get(response, "id")
        try:
            tool_calls = self._parse_tool_calls(getattr(message, "tool_calls", None))
        except MalformedToolArgumentsError as exc:
            exc.usage = usage
            exc.provider_request_id = request_id
            raise
        if isinstance(tool_calls, LLMResponse):
            tool_calls.usage = usage
            tool_calls.provider_request_id = request_id
            return tool_calls
        if tool_calls:
            return LLMResponse(text=getattr(message, "content", None) or None, tool_calls=tool_calls,
                               usage=usage, provider_request_id=request_id)
        return LLMResponse(text=getattr(message, "content", None) or "", usage=usage,
                           provider_request_id=request_id)

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
    completion_details = _get(raw, "completion_tokens_details")
    return TokenUsage(
        input_tokens=_first_int(_get(raw, "prompt_tokens"), _get(raw, "input_tokens")),
        output_tokens=_first_int(_get(raw, "completion_tokens"), _get(raw, "output_tokens")),
        total_tokens=_first_int(_get(raw, "total_tokens")),
        cached_input_tokens=_first_int(
            _get(raw, "prompt_cache_hit_tokens"), _get(prompt_details, "cached_tokens")
        ),
        cache_miss_input_tokens=_first_int(_get(raw, "prompt_cache_miss_tokens")),
        reasoning_tokens=_first_int(_get(completion_details, "reasoning_tokens")),
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
