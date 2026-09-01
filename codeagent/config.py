import os
from dataclasses import dataclass
from pathlib import Path


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


def default_session_root() -> Path:
    configured = os.environ.get("CODEAGENT_SESSION_ROOT")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codeagent" / "sessions"
