from __future__ import annotations

from typing import Any

from codeagent.runtime.candidate import CandidateError, CandidateService
from codeagent.tools.base import PermissionLevel, ToolResult, ToolSpec


def build_freeze_candidate_tool(service: CandidateService) -> ToolSpec:
    def freeze(_: dict[str, Any]) -> ToolResult:
        try:
            candidate = service.freeze()
        except (CandidateError, OSError, UnicodeError) as exc:
            return ToolResult(ok=False, error=f"冻结 candidate 失败: {exc}")
        return ToolResult(
            ok=True,
            content=(
                f"candidate_id: {candidate['candidate_id']}\n"
                f"patch_sha256: {candidate['patch_sha256']}\n"
                f"changed_files: {', '.join(candidate['changed_files'])}"
            ),
            metadata=candidate,
        )

    return ToolSpec(
        name="freeze_candidate",
        description="在编辑后 pytest 已通过时冻结不可变 candidate patch 和测试证据。",
        permission_level=PermissionLevel.CANDIDATE_WRITE,
        schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=freeze,
    )
