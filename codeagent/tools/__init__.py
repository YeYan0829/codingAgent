"""工具公开接口惰性加载；容器里的文件实现无需安装模型SDK或Pydantic。"""
from importlib import import_module

__all__ = ["PermissionLevel", "ToolResult", "ToolSpec", "ToolRegistry"]


def __getattr__(name):
    if name not in __all__:
        raise AttributeError(name)
    module = "registry" if name == "ToolRegistry" else "base"
    value = getattr(import_module("codeagent.tools." + module), name)
    globals()[name] = value
    return value
