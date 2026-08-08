from codeagent.tools.fs_read import build_fs_tools
from codeagent.tools.registry import ToolRegistry
from codeagent.workspace.workspace import WorkspaceContext


def test_registry_exports_internal_model_tools(tmp_path):
    registry = ToolRegistry()
    for tool in build_fs_tools(WorkspaceContext.source(tmp_path)):
        registry.register(tool)

    tools = registry.as_model_tools()
    read_file = next(tool for tool in tools if tool.name == "read_file")

    assert read_file.description
    assert read_file.parameters["type"] == "object"
    assert read_file.parameters["properties"]["path"]["type"] == "string"
    assert "path" in read_file.parameters["required"]
