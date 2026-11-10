from __future__ import annotations

import os
from typing import Any

from codeagent.model_gateway.openai_client import OpenAICompatibleClient

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"


class DeepSeekClient(OpenAICompatibleClient):
    def __init__(self, model: str = DEFAULT_DEEPSEEK_MODEL, api_key: str | None = None,
                 client: Any | None = None, reasoning_enabled: bool = False,
                 reasoning_effort: str | None = None) -> None:
        resolved_key = api_key if api_key is not None else os.environ.get(DEEPSEEK_API_KEY_ENV)
        if client is None and not resolved_key:
            raise RuntimeError(f"{DEEPSEEK_API_KEY_ENV} is required when provider=deepseek")
        effort = (reasoning_effort or "high") if reasoning_enabled else None
        if reasoning_enabled and model not in {"deepseek-v4-flash", "deepseek-v4-pro"}:
            raise ValueError(f"reasoning is not supported for DeepSeek model={model}")
        if effort is not None and effort not in {"low", "high", "max"}:
            raise ValueError("DeepSeek reasoning_effort must be low, high or max")
        super().__init__(
            model=model,
            api_key=resolved_key or "injected-client",
            base_url=DEEPSEEK_BASE_URL,
            provider_name="DeepSeek",
            client=client,
            extra_body={"thinking": {"type": "enabled" if reasoning_enabled else "disabled"}},
            reasoning_effort=effort,
            allow_extra_body_fallback=not reasoning_enabled,
            include_tool_choice=not reasoning_enabled,
        )
