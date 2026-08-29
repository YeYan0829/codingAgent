from __future__ import annotations

from typing import Any

from codeagent.runtime.git_snapshot import GitSnapshotError, snapshot_tree
from codeagent.session.store import SessionStore
from codeagent.workspace.workspace import WorkspaceContext


def current_validation_evidence(store: SessionStore, context: WorkspaceContext) -> list[dict[str, Any]]:
    """返回对当前Candidate仍有效的“验证命令成功执行”证据，不评价命令质量。"""
    try:
        tree = snapshot_tree(context.active_root, context.base_commit or "HEAD")
    except GitSnapshotError:
        return []
    revision = int(store.read_meta().get("candidate_revision", 0))
    return [
        event.payload for event in store.read_events()
        if event.type == "validation_completed"
        and event.payload.get("status") == "passed"
        and int(event.payload.get("workspace_revision", -1)) == context.workspace_revision
        and event.payload.get("base_commit") == context.base_commit
        and int(event.payload.get("candidate_revision", -1)) == revision
        and event.payload.get("subject_tree") == tree
    ]
