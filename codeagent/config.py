import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimeConfig:
    # 真实修复通常包含读取、基线测试、一次失败编辑、自我修正、复测和冻结。
    # 预留最终模型总结所需的额外一步，避免 Candidate 已冻结却只返回限额提示。
    max_steps_per_turn: int = 12
    mode: str = "readonly"


@dataclass(frozen=True)
class ModelConfig:
    provider: str = "fake"
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None

    @property
    def resolved_model(self) -> str:
        if self.model:
            return self.model
        if self.provider == "deepseek":
            return "deepseek-v4-flash"
        return "fake"


def default_session_root() -> Path:
    configured = os.environ.get("CODEAGENT_SESSION_ROOT")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codeagent" / "sessions"
