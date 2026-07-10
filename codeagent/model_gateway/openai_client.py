from __future__ import annotations

from typing import Any

from codeagent.model_gateway.base import BaseModelClient, LLMResponse


class OpenAIClient(BaseModelClient):
    """预留真实模型适配器；v0.1 不主动调用网络 API。"""

    def complete(self, messages: list[dict[str, Any]]) -> LLMResponse:
        raise RuntimeError("OpenAIClient is a placeholder in v0.1. Use FakeLLM by default.")
