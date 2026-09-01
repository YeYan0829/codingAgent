from __future__ import annotations

import os
from typing import Any

from codeagent.model_gateway.openai_client import OpenAICompatibleClient

GLM_BASE_URL_ENV = "GLM_BASE_URL"
GLM_API_KEY_ENV = "GLM_API_KEY"
DEFAULT_GLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_GLM_MODEL = "glm-5.2"


class GLMClient(OpenAICompatibleClient):
    def __init__(self, model: str = DEFAULT_GLM_MODEL, api_key: str | None = None,
                 base_url: str | None = None, client: Any | None = None) -> None:
        resolved_key = api_key if api_key is not None else os.environ.get(GLM_API_KEY_ENV)
        if client is None and not resolved_key:
            raise RuntimeError(f"{GLM_API_KEY_ENV} is required when provider=glm")
        super().__init__(
            model=model,
            api_key=resolved_key or "injected-client",
            base_url=base_url or os.environ.get(GLM_BASE_URL_ENV, DEFAULT_GLM_BASE_URL),
            provider_name="GLM",
            client=client,
            extra_body={"thinking": {"type": "disabled"}},
        )
