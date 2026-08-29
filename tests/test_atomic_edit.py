import hashlib
import json

import pytest

from codeagent.model_gateway.base import BaseModelClient, LLMResponse, LLMToolCall, ModelRequest
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.atomic_edit import AtomicEditError, AtomicEditService
from codeagent.runtime.candidate import ApplyStatus, CandidateService
from codeagent.runtime.sandbox_executor import SandboxedCommandExecutor
from codeagent.runtime.runner import AgentRunner
from codeagent.tools.atomic_edit import build_atomic_edit_tool
from codeagent.tools.fs_read import build_fs_tools
from codeagent.tools.registry import ToolRegistry
from codeagent.cli import app

from test_candidate_loop import HostFixtureBackend, command_service, execution_session


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def operation_service(tmp_path):
    repo, store, context = execution_session(tmp_path)
    return repo, store, context, AtomicEditService(context, store)


def test_atomic_edit_schema_has_only_four_closed_operations(tmp_path):
    _, store, context, service = operation_service(tmp_path)
    tool = build_atomic_edit_tool(service)
    assert tool.name == "apply_workspace_edit"
    assert tool.schema["additionalProperties"] is False
    branches = tool.schema["properties"]["operations"]["items"]["oneOf"]
    assert {branch["properties"]["op"]["const"] for branch in branches} == {
        "create_file", "replace_text", "delete_file", "move_file"
    }
    assert all(branch["additionalProperties"] is False for branch in branches)
    assert "make_directory" not in json.dumps(tool.schema)


def test_multi_file_transaction_create_replace_delete_move_and_parent_directories(tmp_path):
    _, store, context, service = operation_service(tmp_path)
    root = context.active_root
    (root / "delete_me.txt").write_text("delete\n", encoding="utf-8")
    (root / "move_me.txt").write_text("move\n", encoding="utf-8")
    result = service.apply_workspace_edit({"operations": [
        {"op": "replace_text", "path": "sort_utils.py", "expected_sha256": sha(root / "sort_utils.py"),
         "old_text": "reverse=True", "new_text": "reverse=False"},
        {"op": "create_file", "path": "generated/deep/new.py", "content": "VALUE = 1\n"},
        {"op": "delete_file", "path": "delete_me.txt", "expected_sha256": sha(root / "delete_me.txt")},
        {"op": "move_file", "source": "move_me.txt", "destination": "moved/deep/name.txt",
         "expected_sha256": sha(root / "move_me.txt")},
    ]})
    assert result.ok, result.error
    assert result.metadata["candidate_revision"] == 1
    assert "reverse=False" in (root / "sort_utils.py").read_text(encoding="utf-8")
    assert (root / "generated/deep/new.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (root / "delete_me.txt").exists()
    assert not (root / "move_me.txt").exists()
    assert (root / "moved/deep/name.txt").read_text(encoding="utf-8") == "move\n"
    event = [item for item in store.read_events() if item.type == "edit_transaction"][-1]
    assert event.payload["transaction_id"] == result.metadata["transaction_id"]
    assert {item["path"] for item in event.payload["file_changes"]} == set(result.metadata["changed_files"])


def test_stale_sha_rejects_without_changes(tmp_path):
    _, _, context, service = operation_service(tmp_path)
    target = context.active_root / "sort_utils.py"
    before = target.read_bytes()
    result = service.apply_workspace_edit({"operations": [{
        "op": "replace_text", "path": "sort_utils.py", "expected_sha256": "0" * 64,
        "old_text": "reverse=True", "new_text": "reverse=False",
    }]})
    assert not result.ok
    assert result.error_code == "edit_conflict"
    assert target.read_bytes() == before


@pytest.mark.parametrize("path", ["../escape.py", ".env", "nested/.env"])
def test_atomic_edit_rejects_escape_and_sensitive_paths(tmp_path, path):
    _, _, _, service = operation_service(tmp_path)
    result = service.apply_workspace_edit({"operations": [{"op": "create_file", "path": path, "content": "secret\n"}]})
    assert not result.ok
    assert result.error_code in {"invalid_arguments", "path_rejected"}


def test_atomic_edit_rejects_symlink_parent(tmp_path):
    _, _, context, service = operation_service(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (context.active_root / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前环境不能创建 symlink")
    result = service.apply_workspace_edit({"operations": [{"op": "create_file", "path": "linked/new.py", "content": "x\n"}]})
    assert not result.ok
    assert result.error_code == "path_rejected"
    assert not (outside / "new.py").exists()


def test_mid_mutation_failure_rolls_back_files_and_created_directories(tmp_path, monkeypatch):
    _, store, context, service = operation_service(tmp_path)
    original = (context.active_root / "sort_utils.py").read_bytes()
    real_write = service._write_path
    calls = 0

    def fail_once(target, content):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected mutation failure")
        real_write(target, content)

    monkeypatch.setattr(service, "_write_path", fail_once)
    result = service.apply_workspace_edit({"operations": [
        {"op": "replace_text", "path": "sort_utils.py", "expected_sha256": hashlib.sha256(original).hexdigest(),
         "old_text": "reverse=True", "new_text": "reverse=False"},
        {"op": "create_file", "path": "new/deep/file.py", "content": "x\n"},
    ]})
    assert not result.ok
    assert result.error_code == "internal_error"
    assert (context.active_root / "sort_utils.py").read_bytes() == original
    assert not (context.active_root / "new").exists()
    assert not [event for event in store.read_events() if event.type == "edit_transaction"]
    assert not list(service.root.glob("*/transaction.json"))


def test_rollback_failure_marks_recovery_required(tmp_path, monkeypatch):
    _, store, context, service = operation_service(tmp_path)
    real_write = service._write_path
    calls = 0

    def fail_commit_and_rollback(target, content):
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise OSError("injected persistent failure")
        real_write(target, content)

    monkeypatch.setattr(service, "_write_path", fail_commit_and_rollback)
    result = service.apply_workspace_edit({"operations": [
        {"op": "replace_text", "path": "sort_utils.py", "expected_sha256": sha(context.active_root / "sort_utils.py"),
         "old_text": "reverse=True", "new_text": "reverse=False"},
        {"op": "create_file", "path": "other.py", "content": "x\n"},
    ]})
    assert not result.ok
    assert result.error_code == "rollback_failed"
    assert store.read_meta()["workspace_state"] == "recovery_required"
    assert any(json.loads(path.read_text(encoding="utf-8"))["state"] == "recovery_required" for path in service.root.glob("*/transaction.json"))


def test_resume_recovers_prepared_transaction_and_directory_side_effects(tmp_path):
    _, _, context, service = operation_service(tmp_path)
    original = (context.active_root / "sort_utils.py").read_bytes()
    plan = service.prepare({"operations": [
        {"op": "replace_text", "path": "sort_utils.py", "expected_sha256": hashlib.sha256(original).hexdigest(),
         "old_text": "reverse=True", "new_text": "reverse=False"},
        {"op": "create_file", "path": "crashed/deep/new.py", "content": "new\n"},
    ]})
    tx_dir = service.root / plan.transaction_id
    (tx_dir / "backup" / "sort_utils.py").parent.mkdir(parents=True)
    (tx_dir / "backup" / "sort_utils.py").write_bytes(original)
    service._write_json(tx_dir / "plan.json", service._plan_record(plan))
    service._write_state(tx_dir, "committing")
    (context.active_root / "crashed/deep").mkdir(parents=True)
    (context.active_root / "crashed/deep/new.py").write_text("new\n", encoding="utf-8")
    (context.active_root / "sort_utils.py").write_text("partially mutated\n", encoding="utf-8")

    service.ensure_recovered()
    assert (context.active_root / "sort_utils.py").read_bytes() == original
    assert not (context.active_root / "crashed").exists()
    assert not tx_dir.exists()


def test_cli_resume_recovers_unfinished_transaction_before_enabling_execution(tmp_path):
    _, store, context, service = operation_service(tmp_path)
    original = (context.active_root / "sort_utils.py").read_bytes()
    plan = service.prepare({"operations": [{
        "op": "replace_text", "path": "sort_utils.py", "expected_sha256": hashlib.sha256(original).hexdigest(),
        "old_text": "reverse=True", "new_text": "reverse=False",
    }]})
    tx_dir = service.root / plan.transaction_id
    (tx_dir / "backup").mkdir(parents=True)
    (tx_dir / "backup" / "sort_utils.py").write_bytes(original)
    service._write_json(tx_dir / "plan.json", service._plan_record(plan))
    service._write_state(tx_dir, "prepared")
    (context.active_root / "sort_utils.py").write_text("interrupted\n", encoding="utf-8")

    from typer.testing import CliRunner
    result = CliRunner().invoke(app, ["resume", store.session_id, "--session-root", str(store.session_root)], input="/exit\n")
    assert result.exit_code == 0
    assert "run_check disabled" not in result.output
    assert (context.active_root / "sort_utils.py").read_bytes() == original
    assert not tx_dir.exists()


def test_cli_resume_recognizes_managed_move_paths(tmp_path):
    _, store, context, service = operation_service(tmp_path)
    result = service.apply_workspace_edit({"operations": [{
        "op": "move_file", "source": "sort_utils.py", "destination": "src/sort_utils.py",
        "expected_sha256": sha(context.active_root / "sort_utils.py"),
    }]})
    assert result.ok
    from typer.testing import CliRunner
    resumed = CliRunner().invoke(app, ["resume", store.session_id, "--session-root", str(store.session_root)], input="/exit\n")
    assert resumed.exit_code == 0
    assert "run_check disabled" not in resumed.output


def test_cli_resume_recovery_required_overrides_clean_worktree(tmp_path):
    _, store, context, service = operation_service(tmp_path)
    tx_dir = service.root / "clean-but-untrusted"
    tx_dir.mkdir(parents=True)
    service._write_state(tx_dir, "recovery_required", error="injected unverified rollback")
    assert not (context.active_root / "sort_utils.py").is_symlink()
    from test_candidate_loop import git
    assert git(context.active_root, "status", "--porcelain", "--untracked-files=all") == ""

    from typer.testing import CliRunner
    resumed = CliRunner().invoke(
        app,
        ["resume", store.session_id, "--session-root", str(store.session_root)],
        input="/tools\n/exit\n",
    )
    assert resumed.exit_code == 0
    assert "Agent workspace state: recovery_required" in resumed.output
    assert "受保护能力已关闭" in resumed.output
    assert "run_check READ" not in resumed.output
    assert "apply_workspace_edit CANDIDATE_WRITE" not in resumed.output
    assert "freeze_candidate CANDIDATE_WRITE" not in resumed.output
    assert store.read_meta()["workspace_state"] == "recovery_required"


def test_candidate_patch_contains_all_four_atomic_edit_kinds_and_applies(tmp_path):
    repo, store, context, service = operation_service(tmp_path)
    root = context.active_root
    (root / "tracked_delete.txt").write_text("delete\n", encoding="utf-8")
    (root / "tracked_move.txt").write_text("move\n", encoding="utf-8")
    # 让新增 fixture 成为 base commit 的一部分，并重建 execution session。
    from test_candidate_loop import git
    git(repo, "worktree", "remove", str(root), "--force")
    (repo / "tracked_delete.txt").write_text("delete\n", encoding="utf-8")
    (repo / "tracked_move.txt").write_text("move\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "add atomic fixtures")
    # 当前 helper 不适合复用已结束 session，创建新的 session/worktree。
    from codeagent.session.store import SessionStore
    from codeagent.workspace.git_worktree import GitWorktreeManager
    store = SessionStore(repo, session_root=tmp_path / "candidate-sessions")
    context = GitWorktreeManager(store.session_root).create(repo, store.session_id)
    store.create()
    store.begin_workspace_upgrade(1)
    store.activate_workspace(context)
    service = AtomicEditService(context, store)
    root = context.active_root
    result = service.apply_workspace_edit({"operations": [
        {"op": "replace_text", "path": "sort_utils.py", "expected_sha256": sha(root / "sort_utils.py"),
         "old_text": "reverse=True", "new_text": "reverse=False"},
        {"op": "create_file", "path": "created/new.txt", "content": "created\n"},
        {"op": "delete_file", "path": "tracked_delete.txt", "expected_sha256": sha(root / "tracked_delete.txt")},
        {"op": "move_file", "source": "tracked_move.txt", "destination": "moved/new_name.txt",
         "expected_sha256": sha(root / "tracked_move.txt")},
    ]})
    assert result.ok, result.error
    assert command_service(store, context).run_check({"kind": "pytest", "targets": ["test_sort_utils.py"], "timeout_seconds": 30}).ok
    manifest = CandidateService(context, store).freeze()
    _, patch = CandidateService(context, store).load(manifest["candidate_id"])
    text = patch.decode("utf-8")
    assert all(name in text for name in ["sort_utils.py", "created/new.txt", "tracked_delete.txt", "tracked_move.txt", "moved/new_name.txt"])
    receipt = CandidateService(context, store).apply(manifest["candidate_id"], lambda *_: True)
    assert receipt["status"] == ApplyStatus.APPLIED.value
    assert (repo / "created/new.txt").is_file()
    assert not (repo / "tracked_delete.txt").exists()
    assert not (repo / "tracked_move.txt").exists()
    assert (repo / "moved/new_name.txt").read_text(encoding="utf-8") == "move\n"


class ReadThenAtomicEditModel(BaseModelClient):
    def __init__(self, expected_sha256):
        self.expected_sha256 = expected_sha256
        self.step = 0

    def complete(self, request: ModelRequest) -> LLMResponse:
        assert "apply_workspace_edit" in {tool.name for tool in request.tools}
        if self.step == 0:
            call = LLMToolCall(call_id="read", name="read_file", arguments={"path": "sort_utils.py"})
        elif self.step == 1:
            call = LLMToolCall(call_id="edit", name="apply_workspace_edit", arguments={"operations": [{
                "op": "replace_text", "path": "sort_utils.py", "expected_sha256": self.expected_sha256,
                "old_text": "reverse=True", "new_text": "reverse=False",
            }]})
        else:
            return LLMResponse(text="done")
        self.step += 1
        return LLMResponse(tool_calls=[call])


def test_fake_model_uses_formal_registry_runner_read_to_atomic_edit(tmp_path):
    _, store, context = execution_session(tmp_path)
    registry = ToolRegistry(workspace_context=context)
    for tool in build_fs_tools(context):
        registry.register(tool)
    runner = AgentRunner(
        store, ReadThenAtomicEditModel(sha(context.active_root / "sort_utils.py")), registry,
        AutoApprovalGate(allow=True), workspace_context=context,
        command_executor=SandboxedCommandExecutor(HostFixtureBackend()), command_artifact_store=CommandArtifactStore(store),
    )
    output = runner.run_turn("read then edit")
    assert output.final_text == "done"
    assert [step["tool"] for step in output.steps] == ["read_file", "apply_workspace_edit"]
    assert all(step["result"]["ok"] for step in output.steps)
    assert output.steps[0]["result"]["metadata"]["sha256"] == runner.model.expected_sha256
