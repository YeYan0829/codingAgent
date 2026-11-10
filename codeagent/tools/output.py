"""工具实现层的无依赖返回值；对外schema仍由tools.base.ToolResult校验。"""
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ToolOutput:
    ok: bool
    content: str = ""
    error: str | None = None
    error_code: str | None = None
    truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def model_dump(self):
        return asdict(self)
