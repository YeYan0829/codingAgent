from __future__ import annotations

from codeagent.config import ModelConfig
from codeagent.model_gateway.base import BaseModelClient
from codeagent.model_gateway.deepseek_client import DeepSeekClient
from codeagent.model_gateway.fake import FakeLLM
from codeagent.model_gateway.glm_client import GLMClient


def build_model_client(config: ModelConfig) -> BaseModelClient:
    if config.provider == "fake":
        return FakeLLM()
    if config.provider == "deepseek":
        return DeepSeekClient(model=config.resolved_model, reasoning_enabled=config.reasoning_enabled,
                              reasoning_effort=config.resolved_reasoning_effort)
    if config.provider == "glm":
        return GLMClient(model=config.resolved_model, reasoning_enabled=config.reasoning_enabled,
                         reasoning_effort=config.resolved_reasoning_effort)
    raise ValueError(f"unknown provider: {config.provider}")
