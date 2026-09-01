import json
import subprocess
from pathlib import Path

import pytest

from codeagent.context.builder import ConservativeTokenEstimator, ContextManager
from codeagent.context.models import ContextBudgetExceeded, ModelCapabilities
from codeagent.context.projector import ContextNotReady, ProjectionError, project_events
from codeagent.session.store import SessionStore


def _git_workspace(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "a.py").write_text("old\nline2\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
    return root


def test_new_events_have_explicit_identity_and_legacy_events_project(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "inspect"})
    store.append_event("assistant_tool_calls", {"tool_calls": [{"call_id": "c1", "name": "read_file", "arguments": {"path": "a.py"}}]})
    store.append_event("tool_denied", {"call_id": "c1", "reason": "denied", "result": {"ok": False, "error": "denied"}})
    events = store.read_events()
    assert events[-3].seq and events[-3].turn_id
    assert events[-2].model_step_id == events[-1].model_step_id

    legacy = [event.model_copy(update={"seq": None, "turn_id": None, "model_step_id": None}) for event in events]
    turns = project_events(legacy)
    assert turns[0].model_steps[0].exchanges[0].terminal_kind == "denied"


def test_protocol_error_is_not_fabricated_as_tool_exchange(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "edit"})
    store.append_event("model_protocol_error", {"tool": "apply_workspace_edit", "message": "bad json"})
    turns = project_events(store.read_events())
    assert turns[0].model_steps[0].protocol_error["message"] == "bad json"
    assert turns[0].model_steps[0].exchanges == []


def test_incomplete_tool_exchange_fails_closed_before_provider_render(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "inspect"})
    store.append_event("assistant_tool_calls", {"tool_calls": [{"call_id": "c1", "name": "read_file", "arguments": {"path": "a.py"}}]})
    with pytest.raises(ContextNotReady, match="open ModelStep"):
        ContextManager(store).build()


def test_batch_model_step_remains_one_closed_manipulation_atom(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "inspect two things"})
    store.append_event("assistant_tool_calls", {"message": "batch", "tool_calls": [
        {"call_id": "c1", "name": "read_file", "arguments": {"path": "a.py"}},
        {"call_id": "c2", "name": "git_status", "arguments": {}},
    ]})
    store.append_event("tool_result", {"call_id": "c1", "result": {"ok": True, "content": "source"}})
    turns = project_events(store.read_events())
    assert len(turns[0].model_steps) == 1
    assert turns[0].model_steps[0].closed is False

    store.append_event("tool_denied", {"call_id": "c2", "reason": "denied"})
    turns = project_events(store.read_events())
    assert len(turns[0].model_steps) == 1
    assert turns[0].model_steps[0].closed is True
    messages = ContextManager(store).build()
    assistant = next(message for message in messages if message.get("tool_calls"))
    assert [call["id"] for call in assistant["tool_calls"]] == ["c1", "c2"]


def test_orphan_and_duplicate_terminal_outcomes_fail_projection(tmp_path):
    root = _git_workspace(tmp_path)
    orphan = SessionStore(root).create()
    orphan.append_event("user_message", {"message": "inspect"})
    orphan.append_event("tool_result", {"call_id": "missing", "result": {"ok": True}})
    with pytest.raises(ProjectionError, match="unknown call_id"):
        project_events(orphan.read_events())

    duplicate = SessionStore(root).create()
    duplicate.append_event("user_message", {"message": "inspect"})
    duplicate.append_event("assistant_tool_calls", {"tool_calls": [
        {"call_id": "c1", "name": "read_file", "arguments": {"path": "a.py"}}
    ]})
    duplicate.append_event("tool_result", {"call_id": "c1", "result": {"ok": True}})
    duplicate.append_event("tool_denied", {"call_id": "c1", "reason": "late denial"})
    with pytest.raises(ProjectionError, match="duplicate terminal outcome"):
        project_events(duplicate.read_events())


def test_old_execution_becomes_residue_without_body(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "first"})
    store.append_event("assistant_tool_calls", {"tool_calls": [{"call_id": "c1", "name": "read_file", "arguments": {"path": "a.py"}}]})
    store.append_event("tool_result", {"call_id": "c1", "result": {"ok": True, "content": "SECRET BODY", "metadata": {"path": "a.py", "start_line": 1, "end_line": 2, "sha256": "old"}}})
    store.append_event("assistant_message", {"message": "done"})
    store.append_event("user_message", {"message": "continue"})
    (root / "a.py").write_text("changed now\n", encoding="utf-8")
    rendered = ContextManager(store).build()
    residue = next(item["content"] for item in rendered if item["role"] == "system" and "历史执行记录" in item["content"])
    assert "SECRET BODY" not in residue
    assert "a.py" in residue and "sha256" in residue
    assert '"changed_since_read": true' in residue


def test_context_does_not_reread_source_into_runtime_snapshot(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "inspect"})
    store.append_event("assistant_tool_calls", {"tool_calls": [{"call_id": "c1", "name": "read_file", "arguments": {"path": "a.py", "start_line": 1, "end_line": 2}}]})
    store.append_event("tool_result", {"call_id": "c1", "result": {"ok": True, "content": "old", "metadata": {"path": "a.py", "start_line": 1, "end_line": 2}}})
    (root / "a.py").write_text("new current\nline2\n", encoding="utf-8")
    messages = ContextManager(store).build()
    snapshot = messages[2]["content"]
    rendered = json.dumps(messages, ensure_ascii=False)
    assert "new current" not in rendered
    assert '"active_code"' not in snapshot
    assert "old" in rendered


def test_budget_evicts_whole_completed_turns_and_keeps_current(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    for index in range(5):
        store.append_event("user_message", {"message": f"old-{index}-" + "x" * 4000})
        store.append_event("assistant_message", {"message": f"answer-{index}"})
    store.append_event("user_message", {"message": "current request"})
    manager = ContextManager(store)
    messages = manager.build(model_capabilities=ModelCapabilities(context_limit=7_000, generation_reserve=1000,
        continuation_reserve=1000, safety_margin=1000))
    assert any(item.get("content") == "current request" for item in messages)
    assert manager.last_budget_report and manager.last_budget_report.dropped_turn_ids
    assert manager.last_budget_report.estimated_tokens <= manager.last_budget_report.usable_tokens


def test_minimum_context_over_budget_fails_with_breakdown(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "x" * 5000})
    with pytest.raises(ContextBudgetExceeded) as caught:
        ContextManager(store).build(model_capabilities=ModelCapabilities(context_limit=1000, generation_reserve=100,
            continuation_reserve=100, safety_margin=100))
    assert caught.value.report.estimated_tokens > caught.value.report.usable_tokens


def test_conservative_estimator_counts_utf8_and_no_context_artifacts(tmp_path):
    assert ConservativeTokenEstimator().estimate("中文") >= 2
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    ContextManager(store).build()
    forbidden = {"context_views", "runtime_snapshots", "active_code", "budget_reports", "tool_residues", "conversation_windows"}
    assert forbidden.isdisjoint({path.name for path in store.session_dir.iterdir()})


def test_system_prompt_explains_temporary_observations_and_action_convergence(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "fix it"})

    system_prompt = ContextManager(store).build()[0]["content"]

    assert "工具读取结果只会在近期 Context 中暂时保留" in system_prompt
    assert "足以支持一项可验证的 bugfix 时，立即" in system_prompt
    assert "不要为了获得不影响实现选择的额外确定性继续读取" in system_prompt


def test_desensitized_real_session_fixture_preserves_success_failure_and_resume_context(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    fixture = Path(__file__).parent / "fixtures" / "context_itsdangerous_events.jsonl"
    store.events_path.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    (root / "a.py").write_text("fixed current source\n", encoding="utf-8")
    messages = ContextManager(store).build()
    rendered = json.dumps(messages, ensure_ascii=False)
    assert "继续完成测试并验证" in rendered
    assert "fixed current source" not in rendered
    assert "operation_errors" in rendered and "workspace_changed" in rendered
    assert "old source body" not in rendered


def test_active_turn_keeps_four_recent_steps_raw_and_older_steps_as_residue(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "inspect"})
    for index in range(6):
        call_id = f"read-{index}"
        store.append_event("assistant_tool_calls", {"message": f"step {index}", "tool_calls": [
            {"call_id": call_id, "name": "read_file", "arguments": {"path": "a.py"}}
        ]})
        store.append_event("tool_result", {"call_id": call_id, "result": {
            "ok": True, "content": f"RAW-{index}", "metadata": {"path": "a.py", "sha256": "old"}
        }})

    rendered = json.dumps(ContextManager(store).build(), ensure_ascii=False)
    assert "RAW-0" not in rendered and "RAW-1" not in rendered
    assert all(f"RAW-{index}" in rendered for index in range(2, 6))
    tool_results = [json.loads(message["content"]) for message in ContextManager(store).build()
                    if message["role"] == "tool"]
    assert sum(result.get("content_retained") is False for result in tool_results) == 2


def test_recent_raw_target_can_degrade_under_hard_budget(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "inspect"})
    for index in range(4):
        call_id = f"read-{index}"
        store.append_event("assistant_tool_calls", {"tool_calls": [
            {"call_id": call_id, "name": "read_file", "arguments": {"path": "a.py"}}
        ]})
        store.append_event("tool_result", {"call_id": call_id, "result": {
            "ok": True, "content": str(index) * 12_000, "metadata": {"path": "a.py", "sha256": "old"}
        }})

    manager = ContextManager(store)
    messages = manager.build(model_capabilities=ModelCapabilities(
        context_limit=8_000, generation_reserve=1_000, continuation_reserve=1_000, safety_margin=1_000,
    ))
    assert manager.last_budget_report
    assert "recent_raw_to_residue" in manager.last_budget_report.reductions
    assert manager.last_budget_report.estimated_tokens <= 5_000
    tool_results = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
    assert any(result.get("content_retained") is False for result in tool_results)


def test_observation_bounding_is_raw_and_preserves_metadata(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "inspect"})
    store.append_event("assistant_tool_calls", {"tool_calls": [
        {"call_id": "large", "name": "read_file", "arguments": {"path": "a.py"}}
    ]})
    store.append_event("tool_result", {"call_id": "large", "result": {
        "ok": True, "content": "x" * 20_000,
        "metadata": {"path": "a.py", "returned_range": [1, 200], "sha256": "digest"},
    }})

    tool_message = next(message for message in ContextManager(store).build() if message["role"] == "tool")
    result = json.loads(tool_message["content"])
    assert result["context_truncated"] is True
    assert result["metadata"]["context_limit_bytes"] == 16_000
    assert result["metadata"]["returned_range"] == [1, 200]
    assert result["metadata"]["sha256"] == "digest"
    assert "content_retained" not in result
    assert len(result["content"].encode("utf-8")) <= 16_000


def test_legacy_command_residue_recovers_bounded_stderr_tail(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "run"})
    store.append_event("assistant_tool_calls", {"tool_calls": [{"call_id": "cmd", "name": "run_command", "arguments": {"command": "missing", "purpose": "validation"}}]})
    store.append_event("tool_result", {"call_id": "cmd", "result": {"ok": False, "error_code": "completed",
        "error": "命令未成功完成", "content": "status: completed\nexit_code: 127\nstderr:\n/bin/bash: missing: No such file\neffective_policy: {}",
        "metadata": {"status": "completed", "exit_code": 127}}})
    store.append_event("assistant_message", {"message": "stopped"})
    store.append_event("user_message", {"message": "what happened?"})
    residue_message = next(item["content"] for item in ContextManager(store).build()
                           if item["role"] == "system" and "历史执行记录" in item["content"])
    residue = json.loads(residue_message.split("：\n", 1)[1])[0]
    assert residue["metadata"]["exit_code"] == 127
    assert "No such file" in residue["metadata"]["stderr_tail"]
