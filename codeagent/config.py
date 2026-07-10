import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimeConfig:
    max_steps_per_turn: int = 8
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
