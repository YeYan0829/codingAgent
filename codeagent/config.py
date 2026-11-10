import os
from dataclasses import dataclass
from pathlib import Path


REASONING_EFFORTS = {
    "glm": ("high", "max"),
    "deepseek": ("low", "high", "max"),
}
# Runtime 默认值优先保证长工具循环的行动效率；max 仍可由调用方显式选择。
REASONING_DEFAULTS = {"glm": "high", "deepseek": "high"}
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
class ModelLimits:
    context_tokens: int
    default_output_tokens: int = 4_000
    max_output_tokens: int | None = None


# 已核对的模型能力与运行默认值；未知型号沿用适配器的保守容量。
# GLM-5.3：官方标称 1M 上下文、128K 输出，调用示例输出 65536。
MODEL_LIMITS = {
    ("glm", "glm-5.3"): ModelLimits(1_000_000, 65_536, 131_072),
}
PROVIDER_LIMITS = {"glm": ModelLimits(128_000)}
DEFAULT_MODEL_LIMITS = ModelLimits(32_000)


@dataclass(frozen=True)
class RuntimeConfig:
    delegation_enabled: bool = False
    # 用户配置的统计单 token 价格，币种由 fee_currency 标识；不自动猜价。
    cost_per_token_microunits: int | None = None
    fee_currency: str = "USD"
    # provider/model -> 统计单 token 价格；每笔请求保存当时价格，不重算历史。
    model_prices: dict[str, int] | None = None
    # provider/model -> 各类别每 token 的百万分之一币种价格；显式配置，不自动猜价。
    model_token_prices: dict[str, dict[str, int]] | None = None
    # 可选主任务进度工具；不改变命令、编辑或最终回复的能力。
    task_tracking: bool = True
    # 条件引导变体：追加到主提示的方法段落（codeagent/prompts/variants/<name>.md）；用于多候选生成的多样性，默认无。
    prompt_variant: str | None = None

    def __post_init__(self) -> None:
        from codeagent.runtime.request_pricing import validate_prices
        validate_prices(self.model_token_prices or {})
        if not isinstance(self.task_tracking, bool):
            raise ValueError("task_tracking must be a boolean")
        if self.prompt_variant is not None and self.prompt_variant not in available_prompt_variants():
            raise ValueError(f"unknown prompt_variant; available: {sorted(available_prompt_variants())}")
        if self.model_prices is not None and any(not isinstance(k, str) or type(v) is not int or v < 0
                                                 for k, v in self.model_prices.items()):
            raise ValueError("model_prices must contain nonnegative integer token prices")
        if self.cost_per_token_microunits is not None and (
            type(self.cost_per_token_microunits) is not int or self.cost_per_token_microunits < 0
        ):
            raise ValueError("cost_per_token_microunits must be a nonnegative integer")

    @classmethod
    def from_options(cls, options: dict) -> "RuntimeConfig":
        from dataclasses import fields
        current = {f.name: options[f.name] for f in fields(cls) if f.name in options}
        if "delegation_enabled" not in current and options.get("orchestration_backend") == "langgraph":
            current["delegation_enabled"] = True
        return cls(**current)


@dataclass(frozen=True)
class ModelConfig:
    provider: str = "fake"
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_enabled: bool = False
    reasoning_effort: str | None = None
    # 兼容已保存的摘要输出覆盖；缺省继承普通模型输出，不设置正文长度门槛。
    summary_max_tokens: int | None = None

    def __post_init__(self) -> None:
        for name, value in (("max_tokens", self.max_tokens), ("summary_max_tokens", self.summary_max_tokens)):
            if value is None:
                continue
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
            limit = self.limits.max_output_tokens or self.context_limit
            if value > limit:
                raise ValueError(f"{name} exceeds model output limit {limit}")
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
    def limits(self) -> ModelLimits:
        return MODEL_LIMITS.get((self.provider, self.resolved_model),
                                PROVIDER_LIMITS.get(self.provider, DEFAULT_MODEL_LIMITS))

    @property
    def context_limit(self) -> int:
        return self.limits.context_tokens

    @property
    def resolved_max_tokens(self) -> int:
        return self.max_tokens if self.max_tokens is not None else self.limits.default_output_tokens

    @property
    def resolved_summary_max_tokens(self) -> int:
        return self.summary_max_tokens if self.summary_max_tokens is not None else self.resolved_max_tokens

    def context_capabilities(self, *, summary: bool = False):
        """所有角色采用同一容量公式，输出值来自实际请求配置。"""
        from codeagent.context.models import ModelCapabilities
        return ModelCapabilities(context_limit=self.context_limit,
                                 max_output_tokens=(self.resolved_summary_max_tokens if summary else self.resolved_max_tokens))

    @property
    def resolved_reasoning_effort(self) -> str | None:
        if not self.reasoning_enabled:
            return None
        return self.reasoning_effort or REASONING_DEFAULTS[self.provider]


def available_prompt_variants() -> set[str]:
    from importlib.resources import files
    return {entry.name[:-3] for entry in files("codeagent.prompts").joinpath("variants").iterdir() if entry.name.endswith(".md")}


def default_session_root() -> Path:
    configured = os.environ.get("CODEAGENT_SESSION_ROOT")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codeagent" / "sessions"
