from __future__ import annotations

from codeagent.runtime.atomic_edit import AtomicEditService
from codeagent.tools.base import PermissionLevel, ToolSpec


def build_atomic_edit_tool(service: AtomicEditService) -> ToolSpec:
    hash_schema = {"type": "string", "pattern": "^[0-9a-f]{64}$", "description": "read_file 返回的完整文件 SHA-256"}
    path_schema = {"type": "string", "minLength": 1, "maxLength": 1024, "description": "workspace-relative POSIX path"}
    operations = [
        _operation("create_file", {"path": path_schema, "content": {"type": "string"}}, ["op", "path", "content"]),
        _operation("replace_text", {"path": path_schema, "expected_sha256": hash_schema, "old_text": {"type": "string", "minLength": 1}, "new_text": {"type": "string"}}, ["op", "path", "expected_sha256", "old_text", "new_text"]),
        _operation("delete_file", {"path": path_schema, "expected_sha256": hash_schema}, ["op", "path", "expected_sha256"]),
        _operation("move_file", {"source": path_schema, "destination": path_schema, "expected_sha256": hash_schema}, ["op", "source", "destination", "expected_sha256"]),
    ]
    return ToolSpec(
        name="apply_workspace_edit",
        description="在 Session 的 Agent worktree 内原子执行一组 UTF-8 文本 create/replace/delete/move；修改已有文件必须使用 read_file 的 sha256。",
        permission_level=PermissionLevel.CANDIDATE_WRITE,
        schema={"type": "object", "properties": {"operations": {"type": "array", "minItems": 1, "maxItems": 50, "items": {"oneOf": operations}}}, "required": ["operations"], "additionalProperties": False},
        handler=service.apply_workspace_edit,
    )


def _operation(name: str, properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": {"op": {"const": name}, **properties}, "required": required, "additionalProperties": False}
