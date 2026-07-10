from __future__ import annotations

from codeagent.config import ModelConfig
from codeagent.model_gateway.base import BaseModelClient
from codeagent.model_gateway.deepseek_client import DeepSeekClient
from codeagent.model_gateway.fake import FakeLLM


def build_model_client(config: ModelConfig) -> BaseModelClient:
    if config.provider == "fake":
        return FakeLLM()
    if config.provider == "deepseek":
        return DeepSeekClient(model=config.resolved_model)
    raise ValueError(f"unknown provider: {config.provider}")
