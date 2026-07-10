from codeagent.safety.path_guard import PathGuard
from codeagent.tools.fs_read import build_fs_tools
from codeagent.tools.registry import ToolRegistry


def test_registry_exports_openai_compatible_tools(tmp_path):
    registry = ToolRegistry()
    for tool in build_fs_tools(PathGuard(tmp_path)):
        registry.register(tool)

    tools = registry.as_openai_tools()
    read_file = next(tool for tool in tools if tool["function"]["name"] == "read_file")

    assert read_file["type"] == "function"
    assert read_file["function"]["description"]
    assert read_file["function"]["parameters"]["type"] == "object"
    assert read_file["function"]["parameters"]["properties"]["path"]["type"] == "string"
    assert "path" in read_file["function"]["parameters"]["required"]
