import hashlib
import subprocess

import pytest

from codeagent.runtime.atomic_edit import AtomicEditService
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.current_changes import CurrentChangesError, CurrentChangesService
from codeagent.runtime.runner import AgentRunner
from codeagent.model_gateway.base import LLMToolCall
from codeagent.model_gateway.fake import FakeLLM
from codeagent.session.store import SessionStore
from codeagent.tools.fs_read import build_fs_tools
from codeagent.tools.git_read import build_git_tools
from codeagent.tools.registry import ToolRegistry
from codeagent.workspace.workspace import WorkspaceContext
from codeagent.cli import app
from typer.testing import CliRunner
from test_candidate_loop import command_service, execution_session, git


def edit_sort(store, context, old="sorted(values, reverse=True)", new="sorted(values)"):
    target = context.active_root / "sort_utils.py"
    result = AtomicEditService(context, store).apply_workspace_edit({"operations": [{
        "op": "replace_text", "path": "sort_utils.py",
        "expected_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "old_text": old, "new_text": new,
    }]})
    assert result.ok, result.error


def validate(store, context):
    result = command_service(store, context).run_check({
        "kind": "pytest", "targets": ["test_sort_utils.py"], "timeout_seconds": 30,
    })
    assert result.ok, result.error


def test_consecutive_accepts_reuse_worktree_and_checkpoint_pending_diff(tmp_path):
    repo, store, context = execution_session(tmp_path)
    source_head = git(repo, "rev-parse", "HEAD")
    edit_sort(store, context)
    validate(store, context)

    first = CurrentChangesService(store).accept(lambda *_: True)
    assert first["status"] == "applied"
    after_first = store.workspace_context()
    assert after_first.active_root == context.active_root and after_first.active_root.exists()
    assert store.read_meta()["workspace_state"] == "changes_active"
    assert git(repo, "rev-parse", "HEAD") == source_head
    assert git(after_first.active_root, "diff", "HEAD") == ""

    target = after_first.active_root / "sort_utils.py"
    edit = AtomicEditService(after_first, store).apply_workspace_edit({"operations": [{
        "op": "replace_text", "path": "sort_utils.py",
        "expected_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "old_text": "return sorted(values)", "new_text": "return list(sorted(values))",
    }]})
    assert edit.ok
    validate(store, after_first)
    second = CurrentChangesService(store).accept(lambda *_: True)
    assert second["status"] == "applied"
    assert store.workspace_context().active_root == context.active_root
    assert "list(sorted(values))" in (repo / "sort_utils.py").read_text(encoding="utf-8")
    assert not (store.session_dir / "current").exists()
    assert not list(store.diagnostics_dir.glob("transactions/*"))


def test_accept_merges_user_change_in_different_file(tmp_path):
    repo, store, context = execution_session(tmp_path)
    edit_sort(store, context)
    validate(store, context)
    (repo / "user_notes.txt").write_text("user change\n", encoding="utf-8")

    receipt = CurrentChangesService(store).accept(lambda *_: True)

    assert receipt["status"] == "applied"
    assert (repo / "user_notes.txt").read_text(encoding="utf-8") == "user change\n"
    assert "sorted(values)" in (repo / "sort_utils.py").read_text(encoding="utf-8")
    assert (context.active_root / "user_notes.txt").read_text(encoding="utf-8") == "user change\n"


def test_accept_merges_same_file_non_overlapping_user_change(tmp_path):
    repo, store, context = execution_session(tmp_path)
    edit_sort(store, context)
    validate(store, context)
    source = repo / "sort_utils.py"
    source.write_text("# user comment\n" + source.read_text(encoding="utf-8"), encoding="utf-8")

    receipt = CurrentChangesService(store).accept(lambda *_: True)

    assert receipt["status"] == "applied"
    content = source.read_text(encoding="utf-8")
    assert content.startswith("# user comment\n") and "sorted(values)" in content


def test_accept_conflict_preserves_source_and_agent_worktree(tmp_path):
    repo, store, context = execution_session(tmp_path)
    edit_sort(store, context)
    validate(store, context)
    source = repo / "sort_utils.py"
    user_content = source.read_text(encoding="utf-8").replace("sorted(values, reverse=True)", "tuple(values)")
    source.write_text(user_content, encoding="utf-8")

    with pytest.raises(CurrentChangesError, match="三方合并冲突"):
        CurrentChangesService(store).accept(lambda *_: True)

    assert source.read_text(encoding="utf-8") == user_content
    assert "sorted(values)" in (context.active_root / "sort_utils.py").read_text(encoding="utf-8")
    assert store.read_meta()["workspace_state"] == "changes_active"


def test_source_change_during_review_rejects_frozen_patch(tmp_path):
    repo, store, context = execution_session(tmp_path)
    edit_sort(store, context)
    validate(store, context)
    source = repo / "user_notes.txt"

    def change_after_freeze(*_):
        source.write_text("late change\n", encoding="utf-8")
        return True

    receipt = CurrentChangesService(store).accept(change_after_freeze)
    assert receipt["status"] == "source_changed"
    assert "reverse=True" in (repo / "sort_utils.py").read_text(encoding="utf-8")
    assert store.read_meta()["workspace_state"] == "changes_active"


def test_discard_resets_pending_changes_but_keeps_worktree(tmp_path):
    repo, store, context = execution_session(tmp_path)
    edit_sort(store, context)
    receipt = CurrentChangesService(store).discard()
    assert receipt["status"] == "discarded"
    assert context.active_root.exists()
    assert git(context.active_root, "diff", "HEAD") == ""
    assert "reverse=True" in (repo / "sort_utils.py").read_text(encoding="utf-8")
    assert store.read_meta()["workspace_state"] == "changes_active"


def dynamic_runner(store, allowed=True, gate=None):
    context = store.workspace_context()
    registry = ToolRegistry(context)
    for tool in build_fs_tools(context) + build_git_tools(context):
        registry.register(tool)
    return AgentRunner(
        store, FakeLLM(), registry, gate or AutoApprovalGate(allow=allowed),
        workspace_context=context, dynamic_workspace=True,
    )


def test_first_edit_approves_upgrades_and_rebuilds_workspace_tools(tmp_path):
    repo, _, _ = execution_session(tmp_path)
    # 创建一个真正 source_only 的新 Session，避免复用 fixture 的 worktree。
    store = SessionStore(repo, session_root=tmp_path / "dynamic-sessions").create()
    source_hash = hashlib.sha256((repo / "sort_utils.py").read_bytes()).hexdigest()
    runner = dynamic_runner(store)
    old_read = runner.tools.get("read_file")

    step = runner._handle_tool_call(LLMToolCall(call_id="edit", name="apply_workspace_edit", arguments={"operations": [{
        "op": "replace_text", "path": "sort_utils.py", "expected_sha256": source_hash,
        "old_text": "reverse=True", "new_text": "reverse=False",
    }]}))

    assert step["result"]["ok"] is True
    context = store.workspace_context()
    assert context.workspace_kind == "git_worktree" and context.active_root.exists()
    assert runner.tools.workspace_context.active_root == context.active_root
    assert runner.tools.get("read_file") is not old_read
    assert "reverse=False" in runner.tools.call("read_file", {"path": "sort_utils.py"}).content
    assert "reverse=True" in (repo / "sort_utils.py").read_text(encoding="utf-8")


def test_workspace_upgrade_rebinds_approval_gate_to_agent_worktree(tmp_path):
    repo, _, _ = execution_session(tmp_path)
    store = SessionStore(repo, session_root=tmp_path / "approval-context-sessions").create()

    class ContextAwareGate(AutoApprovalGate):
        def __init__(self, context):
            super().__init__(allow=True)
            self.workspace_context = context

    gate = ContextAwareGate(store.workspace_context())
    runner = dynamic_runner(store, gate=gate)
    source_hash = hashlib.sha256((repo / "sort_utils.py").read_bytes()).hexdigest()
    step = runner._handle_tool_call(LLMToolCall(call_id="edit", name="apply_workspace_edit", arguments={"operations": [{
        "op": "replace_text", "path": "sort_utils.py", "expected_sha256": source_hash,
        "old_text": "reverse=True", "new_text": "reverse=False",
    }]}))

    assert step["result"]["ok"] is True
    assert gate.workspace_context.active_root == store.workspace_context().active_root
    assert gate.workspace_context.workspace_kind == "git_worktree"


def test_workspace_upgrade_denied_or_dirty_source_leaves_source_only(tmp_path):
    repo, _, _ = execution_session(tmp_path)
    denied_store = SessionStore(repo, session_root=tmp_path / "denied-sessions").create()
    denied = dynamic_runner(denied_store, allowed=False)._handle_tool_call(
        LLMToolCall(call_id="deny", name="apply_workspace_edit", arguments={"operations": []})
    )
    assert denied["result"]["error_code"] == "approval_denied"
    assert denied_store.workspace_state().value == "source_only"

    (repo / "dirty.txt").write_text("user", encoding="utf-8")
    dirty_store = SessionStore(repo, session_root=tmp_path / "dirty-sessions").create()
    failed = dynamic_runner(dirty_store)._handle_tool_call(
        LLMToolCall(call_id="dirty", name="apply_workspace_edit", arguments={"operations": []})
    )
    assert failed["result"]["error_code"] == "workspace_upgrade_failed"
    assert dirty_store.workspace_state().value == "source_only"
    assert dirty_store.workspace_context().active_root == repo.resolve()


def test_workspace_upgrade_denial_is_only_prompted_once_per_turn(tmp_path):
    repo, _, _ = execution_session(tmp_path)
    store = SessionStore(repo, session_root=tmp_path / "denial-once-sessions").create()

    class CountingGate(AutoApprovalGate):
        def __init__(self):
            super().__init__(allow=False)
            self.upgrade_requests = 0

        def request_workspace_upgrade(self, tool_name, arguments):
            self.upgrade_requests += 1
            return False

    gate = CountingGate()
    runner = dynamic_runner(store, gate=gate)
    first = runner._handle_tool_call(
        LLMToolCall(call_id="command", name="run_command", arguments={"command": "true"})
    )
    second = runner._handle_tool_call(
        LLMToolCall(call_id="edit", name="apply_workspace_edit", arguments={"operations": []})
    )

    assert first["result"]["error_code"] == "approval_denied"
    assert second["result"]["error_code"] == "approval_denied"
    assert "不再重复询问" in second["result"]["error"]
    assert gate.upgrade_requests == 1


def test_resume_after_accept_with_new_pending_edit_uses_latest_baseline(tmp_path):
    _, store, context = execution_session(tmp_path)
    edit_sort(store, context)
    validate(store, context)
    assert CurrentChangesService(store).accept(lambda *_: True)["status"] == "applied"
    active = store.workspace_context()
    target = active.active_root / "sort_utils.py"
    result = AtomicEditService(active, store).apply_workspace_edit({"operations": [{
        "op": "replace_text", "path": "sort_utils.py",
        "expected_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "old_text": "return sorted(values)", "new_text": "return list(sorted(values))",
    }]})
    assert result.ok

    resumed = CliRunner().invoke(app, ["resume", store.session_id, "--session-root", str(store.session_root)], input="/exit\n")
    assert resumed.exit_code == 0, resumed.output
    assert store.read_meta()["workspace_state"] == "changes_active"
    assert "受保护能力已关闭" not in resumed.output
