import hashlib
import json
import stat

import pytest

from codeagent.model_gateway.base import BaseModelClient, LLMResponse, LLMToolCall, ModelRequest
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.atomic_edit import AtomicEditError, AtomicEditService
from codeagent.runtime.sandbox_executor import SandboxedCommandExecutor
from codeagent.runtime.runner import AgentRunner
from codeagent.tools.atomic_edit import build_atomic_edit_tool
from codeagent.tools.fs_read import build_fs_tools
from codeagent.tools.registry import ToolRegistry
from codeagent.cli import app
from codeagent.config import RuntimeConfig

from runtime_fixtures import HostFixtureBackend, command_service, execution_session


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
    branches = [tool.schema["$defs"][item["$ref"].split("/")[-1]]
                for item in tool.schema["properties"]["operations"]["items"]["anyOf"]]
    assert {branch["properties"]["op"]["const"] for branch in branches} == {
        "create_file", "replace_text", "delete_file", "move_file"
    }
    assert all(branch["additionalProperties"] is False for branch in branches)
    required = {branch["properties"]["op"]["const"]: set(branch["required"]) for branch in branches}
    # 常用替换/新建可省略 op 与 hash；删除/移动仍需显式 op，hash 同样可选。
    assert required["replace_text"] == {"path", "old_text", "new_text"}
    assert required["create_file"] == {"path", "content"}
    assert required["delete_file"] == {"op", "path"}
    assert required["move_file"] == {"op", "source", "destination"}
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
    assert result.metadata["transaction_id"]
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


def test_fill_existing_empty_file_with_current_hash(tmp_path):
    _, _, context, service = operation_service(tmp_path)
    target = context.active_root / "empty.py"
    target.write_bytes(b"")
    result = build_atomic_edit_tool(service).handler({"operations": [{
        "op": "replace_text", "path": "empty.py", "expected_sha256": sha(target),
        "old_text": "", "new_text": "标题 = '已填写'\n",
    }]})
    assert result.ok, result.error
    assert target.read_text() == "标题 = '已填写'\n"
    assert result.metadata["transaction_id"]


def test_empty_file_stale_hash_and_nonempty_anchor_reject_atomically(tmp_path):
    _, _, context, service = operation_service(tmp_path)
    target = context.active_root / "empty.py"
    target.write_bytes(b"")
    old_hash = sha(target)
    target.write_text("concurrent change\n")
    stale = service.apply_workspace_edit({"operations": [{
        "op": "replace_text", "path": "empty.py", "expected_sha256": old_hash,
        "old_text": "", "new_text": "overwrite\n",
    }]})
    assert not stale.ok and stale.error_code == "edit_conflict"
    rejected = service.apply_workspace_edit({"operations": [
        {"op": "create_file", "path": "must_not_exist.py", "content": "created\n"},
        {"op": "replace_text", "path": "empty.py", "expected_sha256": sha(target),
         "old_text": "", "new_text": "overwrite\n"},
    ]})
    assert not rejected.ok and rejected.error_code == "edit_conflict"
    assert not (context.active_root / "must_not_exist.py").exists()
    assert target.read_text() == "concurrent change\n"


def test_replace_text_preserves_existing_file_mode(tmp_path):
    _, _, context, service = operation_service(tmp_path)
    target = context.active_root / "sort_utils.py"
    target.chmod(0o755)

    result = service.apply_workspace_edit({"operations": [{
        "op": "replace_text", "path": "sort_utils.py", "expected_sha256": sha(target),
        "old_text": "reverse=True", "new_text": "reverse=False",
    }]})

    assert result.ok, result.error
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_invalid_operation_reports_machine_readable_field_and_no_workspace_change(tmp_path):
    _, _, _, service = operation_service(tmp_path)
    result = service.apply_workspace_edit({"operations": [{"path": "sort_utils.py"}]})
    assert not result.ok
    assert result.error_code == "invalid_arguments"
    assert result.metadata["workspace_changed"] is False
    assert result.metadata["operation_errors"] == [
        {"operation_index": 0, "field": "op", "reason": "required_string"}
    ]


def test_delete_does_not_require_reading_hash_before_normal_work(tmp_path):
    _, _, context, service = operation_service(tmp_path)
    result = service.apply_workspace_edit({"operations": [
        {"op": "create_file", "path": "new.txt", "content": "new"},
        {"op": "delete_file", "path": "sort_utils.py"},
    ]})
    assert result.ok, result
    assert not (context.active_root / "sort_utils.py").exists()
    assert (context.active_root / "new.txt").read_text() == "new"


def test_replace_without_op_or_hash_uses_unique_old_text(tmp_path):
    _, _, context, service = operation_service(tmp_path)
    result = service.apply_workspace_edit({"operations": [
        {"path": "sort_utils.py", "old_text": "reverse=True", "new_text": "reverse=False"},
    ]})
    assert result.ok, result.error
    assert "reverse=False" in (context.active_root / "sort_utils.py").read_text(encoding="utf-8")


def test_wrong_hash_reports_current_hash_and_writes_nothing(tmp_path):
    _, _, context, service = operation_service(tmp_path)
    target = context.active_root / "sort_utils.py"
    original = target.read_bytes()
    result = service.apply_workspace_edit({"operations": [
        {"path": "sort_utils.py", "expected_sha256": "0" * 64, "old_text": "reverse=True", "new_text": "reverse=False"},
    ]})
    assert result.error_code == "edit_conflict"
    assert sha(target) in result.error
    assert target.read_bytes() == original


def test_old_text_mismatch_points_to_candidate_lines(tmp_path):
    _, _, context, service = operation_service(tmp_path)
    target = context.active_root / "sort_utils.py"
    first = next(line for line in target.read_text(encoding="utf-8").splitlines() if line.strip())
    result = service.apply_workspace_edit({"operations": [
        {"path": "sort_utils.py", "old_text": first + "\n    this line does not exist", "new_text": "x"},
    ]})
    assert result.error_code == "edit_conflict"
    assert "实际 0 次" in result.error and "第 [" in result.error


def test_create_file_overwrite_requires_explicit_flag(tmp_path):
    _, _, context, service = operation_service(tmp_path)
    (context.active_root / "scratch.py").write_text("old\n", encoding="utf-8")
    rejected = service.apply_workspace_edit({"operations": [{"path": "scratch.py", "content": "new\n"}]})
    assert rejected.error_code == "already_exists" and "overwrite=true" in rejected.error
    accepted = service.apply_workspace_edit({"operations": [{"path": "scratch.py", "content": "new\n", "overwrite": True}]})
    assert accepted.ok, accepted.error
    assert (context.active_root / "scratch.py").read_text(encoding="utf-8") == "new\n"


@pytest.mark.parametrize("operation, expected", [
    ({"op": "create_file", "path": "new.txt", "content": "x", "unknown": True},
     {"operation_index": 0, "field": "unknown", "reason": "unexpected_field"}),
    ({"op": "replace"}, {"operation_index": 0, "field": "op", "reason": "unsupported_operation"}),
])
def test_unknown_edit_fields_have_actionable_diagnostics(tmp_path, operation, expected):
    _, _, context, service = operation_service(tmp_path)
    result = service.apply_workspace_edit({"operations": [operation]})
    assert result.error_code == "invalid_arguments"
    assert result.metadata["operation_errors"] == [expected]
    assert not (context.active_root / "new.txt").exists()


@pytest.mark.parametrize("path", ["../escape.py", "/tmp/escape.py"])
def test_atomic_edit_rejects_escape_paths(tmp_path, path):
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
    for relative, body in plan.after.items():
        if body is not None:
            saved = tx_dir / "after" / relative
            saved.parent.mkdir(parents=True, exist_ok=True); saved.write_bytes(body)
    service._write_state(tx_dir, "committing")
    (context.active_root / "crashed/deep").mkdir(parents=True)
    (context.active_root / "crashed/deep/new.py").write_text("new\n", encoding="utf-8")
    (context.active_root / "sort_utils.py").write_bytes(plan.after["sort_utils.py"])

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
    (tx_dir / "after").mkdir()
    (tx_dir / "after" / "sort_utils.py").write_bytes(plan.after["sort_utils.py"])
    service._write_state(tx_dir, "prepared")
    (context.active_root / "sort_utils.py").write_bytes(plan.after["sort_utils.py"])

    from typer.testing import CliRunner
    result = CliRunner().invoke(app, ["resume", store.session_id, "--session-root", str(store.session_root)], input="/exit\n")
    assert result.exit_code == 0
    assert "run_check disabled" not in result.output
    assert (context.active_root / "sort_utils.py").read_bytes() == original
    assert not tx_dir.exists()








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
        AutoApprovalGate(allow=True), config=RuntimeConfig(task_tracking=False), workspace_context=context,
        command_executor=SandboxedCommandExecutor(HostFixtureBackend()), command_artifact_store=CommandArtifactStore(store),
    )
    # 关闭任务清单时没有清单工具和交付核对：编辑后正常结束，不追加部分交付警告。
    assert not {"update_task_plan", "read_task_plan"} & {tool.name for tool in runner.tools.as_model_tools()}
    output = runner.run_turn("read then edit")
    assert output.final_text == "done"
    assert [step["tool"] for step in output.steps] == ["read_file", "apply_workspace_edit"]
    assert all(step["result"]["ok"] for step in output.steps)
    assert output.steps[0]["result"]["metadata"]["sha256"] == runner.model.expected_sha256
