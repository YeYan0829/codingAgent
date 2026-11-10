from __future__ import annotations

from codeagent.config import ModelConfig
from codeagent.model_gateway.base import BaseModelClient
from codeagent.model_gateway.deepseek_client import DeepSeekClient
from codeagent.model_gateway.fake import FakeLLM
from codeagent.model_gateway.glm_client import GLMClient


def build_model_client(config: ModelConfig) -> BaseModelClient:
    if config.provider == "fake":
        if config.model == "ui-fixture":
            from codeagent.model_gateway.ui_fixture import UIAcceptanceFixture
            return UIAcceptanceFixture()
        if config.model == "c3-fixture":
            from codeagent.model_gateway.orchestration_fixture import OrchestrationFixture
            return OrchestrationFixture()
        return FakeLLM()
    if config.provider == "deepseek":
        return DeepSeekClient(model=config.resolved_model, reasoning_enabled=config.reasoning_enabled,
                              reasoning_effort=config.resolved_reasoning_effort)
    if config.provider == "glm":
        return GLMClient(model=config.resolved_model, reasoning_enabled=config.reasoning_enabled,
                         reasoning_effort=config.resolved_reasoning_effort)
    raise ValueError(f"unknown provider: {config.provider}")
