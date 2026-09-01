from __future__ import annotations

import os
from typing import Any

from codeagent.model_gateway.openai_client import OpenAICompatibleClient

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"


class DeepSeekClient(OpenAICompatibleClient):
    def __init__(self, model: str = DEFAULT_DEEPSEEK_MODEL, api_key: str | None = None,
                 client: Any | None = None) -> None:
        resolved_key = api_key if api_key is not None else os.environ.get(DEEPSEEK_API_KEY_ENV)
        if client is None and not resolved_key:
            raise RuntimeError(f"{DEEPSEEK_API_KEY_ENV} is required when provider=deepseek")
        super().__init__(
            model=model,
            api_key=resolved_key or "injected-client",
            base_url=DEEPSEEK_BASE_URL,
            provider_name="DeepSeek",
            client=client,
            extra_body={"thinking": {"type": "disabled"}},
        )
