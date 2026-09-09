import os
from dataclasses import dataclass
from pathlib import Path


REASONING_EFFORTS = {
    "glm": ("high", "max"),
    "deepseek": ("low", "high", "max"),
}
REASONING_DEFAULTS = {"glm": "max", "deepseek": "high"}
REASONING_MODELS = {
    "glm": {"glm-5.2", "glm-5.3"},
    "deepseek": {"deepseek-v4-flash", "deepseek-v4-pro"},
}
REASONING_MODEL_EFFORTS = {
    ("glm", "glm-5.2"): ("high", "max"),
    ("glm", "glm-5.3"): ("low", "high", "max"),
}
ALWAYS_REASONING_MODELS = {("glm", "glm-5.3")}


@dataclass(frozen=True)
class RuntimeConfig:
    # 单个执行切片的控制点；耗尽不结束 UserTurn。
    max_steps_per_turn: int = 12
    # 同一 UserTurn 的总体模型调用预算；CLI 可逐 slice 继续，benchmark 可自动继续。
    max_model_steps_per_user_turn: int = 48


@dataclass(frozen=True)
class ModelConfig:
    provider: str = "fake"
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_enabled: bool = False
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reasoning_enabled, bool):
            raise ValueError("reasoning_enabled must be a boolean")
        if (self.provider, self.resolved_model) in ALWAYS_REASONING_MODELS and not self.reasoning_enabled:
            raise ValueError(f"reasoning cannot be disabled for {self.provider} model={self.resolved_model}")
        allowed_efforts = REASONING_MODEL_EFFORTS.get(
            (self.provider, self.resolved_model), REASONING_EFFORTS.get(self.provider, ())
        )
        if self.reasoning_effort is not None and self.reasoning_effort not in allowed_efforts:
            allowed = ", ".join(allowed_efforts) or "none"
            raise ValueError(f"reasoning_effort for {self.provider} must be one of: {allowed}")
        if self.reasoning_enabled:
            if self.provider not in REASONING_MODELS:
                raise ValueError(f"reasoning is not supported for provider={self.provider}")
            if self.resolved_model not in REASONING_MODELS[self.provider]:
                raise ValueError(f"reasoning is not supported for {self.provider} model={self.resolved_model}")

    @property
    def resolved_model(self) -> str:
        if self.model:
            return self.model
        if self.provider == "deepseek":
            return "deepseek-v4-flash"
        if self.provider == "glm":
            return "glm-5.2"
        return "fake"

    @property
    def context_limit(self) -> int:
        """Runtime 实验输入上限；低于 provider 宣称容量也可用于成本保护。"""
        if self.provider == "glm":
            return 128_000
        return 32_000

    @property
    def resolved_reasoning_effort(self) -> str | None:
        if not self.reasoning_enabled:
            return None
        return self.reasoning_effort or REASONING_DEFAULTS[self.provider]


def default_session_root() -> Path:
    configured = os.environ.get("CODEAGENT_SESSION_ROOT")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codeagent" / "sessions"
