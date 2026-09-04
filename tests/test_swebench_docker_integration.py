from __future__ import annotations

import os
from pathlib import Path

import pytest

from codeagent.benchmark import (
    SWEbenchDockerCommandExecutor,
    SWEbenchSourcePreparer,
    SWEbenchTaskSource,
)
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.command_service import CommandService
from codeagent.session.store import SessionStore
from codeagent.workspace.git_worktree import GitWorktreeManager


pytestmark = pytest.mark.skipif(
    os.environ.get("CODEAGENT_RUN_SWEBENCH_DOCKER") != "1",
    reason="设置 CODEAGENT_RUN_SWEBENCH_DOCKER=1 后运行真实 SWE-bench Docker 集成测试",
)


def test_official_image_projects_candidate_and_audits_changes(tmp_path: Path) -> None:
    prepared = SWEbenchSourcePreparer(tmp_path / "source-cache").prepare(
        SWEbenchTaskSource(
            instance_id="sympy__sympy-20590",
            image="swebench/sweb.eval.x86_64.sympy_1776_sympy-20590:latest",
            dataset_base_commit="cffd4e0f86fefd4802349a9f9b19ed70934ea354",
        )
    )
    session_root = tmp_path / "sessions"
    store = SessionStore(
        prepared.source_root, session_id="swebench-phase2", session_root=session_root
    ).create()
    manager = GitWorktreeManager(session_root)
    context = None
    try:
        store.begin_workspace_upgrade(1)
        context = manager.create(prepared.source_root, store.session_id, 1)
        store.activate_workspace(context)
        service = CommandService(
            context,
            AutoApprovalGate(True),
            SWEbenchDockerCommandExecutor(prepared),
            store,
            CommandArtifactStore(store),
        )

        result = service.run_command(
            {
                "command": (
                    f"test \"$(git rev-parse --show-toplevel)\" = /testbed && "
                    f"test \"$(git rev-parse HEAD)\" = {prepared.prepared_head} && "
                    "python --version && printf 'container-write\\n' > phase2-container.txt && "
                    "git status --short"
                ),
                "timeout_seconds": 120,
                "purpose": "utility",
            }
        )

        assert result.ok
        assert (context.active_root / "phase2-container.txt").read_text() == "container-write\n"
        assert not (context.source_root / "phase2-container.txt").exists()
        assert result.metadata["command_induced_changes"] == [
            {"path": "phase2-container.txt", "kind": "create"}
        ]
    finally:
        if context is not None:
            manager.discard(context)
