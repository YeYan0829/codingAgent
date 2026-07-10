from __future__ import annotations

from codeagent.model_gateway.base import BaseModelClient, LLMResponse, ModelRequest


class OpenAIClient(BaseModelClient):
    """预留 OpenAI 适配器；v0.2 只实现 DeepSeek。"""

    def complete(self, request: ModelRequest) -> LLMResponse:
        raise RuntimeError("OpenAIClient is a placeholder in v0.2. Use FakeLLM or DeepSeekClient.")
