from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from codeagent.model_gateway.base import ModelTool


class PermissionLevel(StrEnum):
    READ = "READ"
    EXEC_READONLY = "EXEC_READONLY"
    WRITE = "WRITE"
    WORKSPACE_WRITE = "WORKSPACE_WRITE"
    WORKSPACE_EXECUTE = "WORKSPACE_EXECUTE"
    SESSION_WRITE = "SESSION_WRITE"
    NETWORK = "NETWORK"
    DANGEROUS = "DANGEROUS"


class ToolResult(BaseModel):
    ok: bool
    content: str = ""
    error: str | None = None
    error_code: str | None = None
    truncated: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


ToolHandler = Callable[[dict[str, Any]], ToolResult]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    permission_level: PermissionLevel
    schema: dict[str, Any]
    handler: ToolHandler
    input_model: type[BaseModel] | None = None

    @classmethod
    def from_model(cls, name, description, permission_level, input_model, handler):
        """同一输入类型生成模型schema并校验调用；文件/权限条件仍由服务处理。"""
        def validated(arguments):
            try:
                # 保留调用方是否显式传参；例如字节续读不能自动补入start_line。
                command = input_model.model_validate(arguments)
            except ValidationError as exc:
                return ToolResult(ok=False, error_code="invalid_arguments", error=str(exc), metadata={
                    "argument_errors": exc.errors(include_input=False, include_context=False),
                    **({"workspace_changed": False} if permission_level == PermissionLevel.WORKSPACE_WRITE else {}),
                })
            result = handler(command.model_dump(exclude_unset=True, exclude_none=True))
            # 文件实现也部署到无第三方依赖的容器工具进程；公开返回类型保持一致。
            return result if isinstance(result, ToolResult) else ToolResult.model_validate(result.model_dump())
        return cls(name, description, permission_level, input_model.model_json_schema(), validated, input_model)

    def to_model_tool(self) -> ModelTool:
        return ModelTool(name=self.name, description=self.description, parameters=self.schema)
