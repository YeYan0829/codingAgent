from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeConfig:
    max_steps_per_turn: int = 8
    model: str = "fake"
    mode: str = "readonly"
