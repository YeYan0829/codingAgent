from __future__ import annotations

import os
from typing import Any

from codeagent.model_gateway.openai_client import OpenAICompatibleClient

GLM_BASE_URL_ENV = "GLM_BASE_URL"
GLM_API_KEY_ENV = "GLM_API_KEY"
DEFAULT_GLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_GLM_MODEL = "glm-5.2"
GLM_REASONING_EFFORTS = {
    "glm-5.2": {"high", "max"},
    "glm-5.3": {"low", "high", "max"},
}


class GLMClient(OpenAICompatibleClient):
    def __init__(self, model: str = DEFAULT_GLM_MODEL, api_key: str | None = None,
                 base_url: str | None = None, client: Any | None = None,
                 reasoning_enabled: bool = False, reasoning_effort: str | None = None) -> None:
        resolved_key = api_key if api_key is not None else os.environ.get(GLM_API_KEY_ENV)
        if client is None and not resolved_key:
            raise RuntimeError(f"{GLM_API_KEY_ENV} is required when provider=glm")
        effort = (reasoning_effort or "high") if reasoning_enabled else None
        if model == "glm-5.3" and not reasoning_enabled:
            raise ValueError("GLM-5.3 always reasons and does not support reasoning disabled")
        if reasoning_enabled and model not in GLM_REASONING_EFFORTS:
            raise ValueError(f"reasoning is not supported for GLM model={model}")
        if effort is not None and effort not in GLM_REASONING_EFFORTS[model]:
            allowed = ", ".join(sorted(GLM_REASONING_EFFORTS[model]))
            raise ValueError(f"{model} reasoning_effort must be one of: {allowed}")
        # Runtime 会保留紧邻上一轮的完整工具推理子轮，并把更早可见历史交给语义压缩器。
        # 因此不能声明 preserved thinking；该模式要求全部 reasoning 原样连续回传。
        thinking = {"type": "enabled", "clear_thinking": True} if reasoning_enabled else {"type": "disabled"}
        super().__init__(
            model=model,
            api_key=resolved_key or "injected-client",
            base_url=base_url or os.environ.get(GLM_BASE_URL_ENV, DEFAULT_GLM_BASE_URL),
            provider_name="GLM",
            client=client,
            extra_body={"thinking": thinking},
            reasoning_effort=effort,
            allow_extra_body_fallback=not reasoning_enabled,
        )
