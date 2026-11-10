import json
import subprocess
from pathlib import Path

import pytest

from codeagent.context.builder import ConservativeTokenEstimator, ContextManager
from codeagent.context.models import (
    CondensationRequired, ContextBudgetExceeded, ModelCapabilities, ReadyContext,
)
from codeagent.context.projector import ContextNotReady, ProjectionError, project_events
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.command_service import CommandService
from codeagent.runtime.sandbox_executor import SandboxedCommandExecutor
from codeagent.session.store import SessionStore
from codeagent.workspace.workspace import WorkspaceContext


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


def test_old_execution_remains_continuous_raw_until_semantic_condensation(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "first"})
    store.append_event("assistant_tool_calls", {"tool_calls": [{"call_id": "c1", "name": "read_file", "arguments": {"path": "a.py"}}]})
    store.append_event("tool_result", {"call_id": "c1", "result": {"ok": True, "content": "SECRET BODY", "metadata": {"path": "a.py", "start_line": 1, "end_line": 2, "sha256": "old"}}})
    store.append_event("assistant_message", {"message": "done"})
    store.append_event("user_message", {"message": "continue"})
    (root / "a.py").write_text("changed now\n", encoding="utf-8")
    result = ContextManager(store).build()
    assert isinstance(result, ReadyContext)
    rendered = json.dumps(result.messages, ensure_ascii=False)
    assert "SECRET BODY" in rendered
    assert "historical_reduction" not in rendered


def test_context_does_not_reread_source_into_runtime_context(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "inspect"})
    store.append_event("assistant_tool_calls", {"tool_calls": [{"call_id": "c1", "name": "read_file", "arguments": {"path": "a.py", "start_line": 1, "end_line": 2}}]})
    store.append_event("tool_result", {"call_id": "c1", "result": {"ok": True, "content": "old", "metadata": {"path": "a.py", "start_line": 1, "end_line": 2}}})
    (root / "a.py").write_text("new current\nline2\n", encoding="utf-8")
    messages = ContextManager(store).build().messages
    snapshot = messages[2]["content"]
    rendered = json.dumps(messages, ensure_ascii=False)
    assert "new current" not in rendered
    assert '"active_code"' not in snapshot
    assert "old" in rendered


def test_runtime_context_exposes_current_project_without_baseline_or_version(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    snapshot = ContextManager(store).build()[2]['content']
    assert f'"active_workspace": "{root}"' in snapshot
    assert f'"default_cwd_resolves_to": "{root}"' in snapshot
    assert 'baseline_workspace' not in snapshot and 'subject_tree' not in snapshot
    assert '历史观察' in snapshot


def test_runtime_context_uses_command_service_environment_source(tmp_path, monkeypatch):
    class DiscoverableBackend:
        def discover(self, _workspace_root):
            return Path("/fixture/bwrap")

    monkeypatch.setenv("PATH", "/project-tools:/usr/bin")
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    active = tmp_path / "candidate"
    subprocess.run(["git", "clone", "-q", str(root), str(active)], check=True)
    (active / "a.py").write_text("candidate\n", encoding="utf-8")
    context = WorkspaceContext(root, active)
    context = store.workspace_context()
    commands = CommandService(
        context, AutoApprovalGate(True), SandboxedCommandExecutor(DiscoverableBackend()),
        store, CommandArtifactStore(store),
    )

    snapshot = ContextManager(store, environment_provider=commands.environment_snapshot).build()[2]["content"]

    assert '"contract_revision": "command-environment-v1"' in snapshot
    assert '"path": ["/project-tools", "/usr/bin"]' in snapshot
    assert '"backend_availability": "executable_available"' in snapshot
    assert not (CommandArtifactStore(store).runtime_root / "next-command").exists()


def test_source_only_snapshot_does_not_claim_worktree_transition(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()

    snapshot = ContextManager(store).build()[2]["content"]

    assert '"active_workspace":' in snapshot and "workspace_kind" not in snapshot
    assert "Runtime 已动态激活隔离 Candidate worktree" not in snapshot


def test_budget_evicts_whole_completed_turns_and_keeps_current(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    for index in range(5):
        store.append_event("user_message", {"message": f"old-{index}-" + "x" * 4000})
        store.append_event("assistant_message", {"message": f"answer-{index}"})
    store.append_event("user_message", {"message": "current request"})
    manager = ContextManager(store)
    measured = manager.build(model_capabilities=ModelCapabilities(context_limit=200_000))
    # 给历史保留1000 token；工具/提示增长不能让“压缩完整旧轮次”测试提前走fixed_set。
    result = manager.build(model_capabilities=ModelCapabilities(context_limit=measured.budget_report.fixed_request_tokens + 4_000, max_output_tokens=1000))
    assert isinstance(result, CondensationRequired)
    assert result.plan.source_event_ids[0] == "seq:2"
    assert "seq:12" not in result.plan.source_event_ids
    assert manager.last_budget_report and not manager.last_budget_report.dropped_turn_ids


def test_minimum_context_over_budget_fails_with_breakdown(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "x" * 5000})
    result = ContextManager(store).build(model_capabilities=ModelCapabilities(context_limit=1000, max_output_tokens=100))
    assert isinstance(result, ContextBudgetExceeded)
    assert result.report.estimated_tokens > result.report.usable_tokens
    report = result.report
    categories = {item.category for item in report.minimum_set_components}
    assert {"system_prompt", "runtime_context", "current_user_message", "tool_definitions"} <= categories
    assert report.component_estimated_tokens == sum(
        item.estimated_tokens for item in report.minimum_set_components
    )
    assert report.estimation_residual == report.estimated_tokens - report.component_estimated_tokens
    assert "xxxxx" not in json.dumps([item.__dict__ for item in report.minimum_set_components])


def test_minimum_breakdown_identifies_large_observation_without_copying_content(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "x" * 5000})
    store.append_event("assistant_tool_calls", {"tool_calls": [{
        "call_id": "large-read", "name": "read_file", "arguments": {"path": "a.py"},
    }]})
    store.append_event("tool_result", {"call_id": "large-read", "result": {
        "ok": True, "content": "SECRET_SOURCE_BODY" * 1000,
        "metadata": {"path": "a.py", "sha256": "digest"},
    }})
    result = ContextManager(store).build(model_capabilities=ModelCapabilities(context_limit=1000, max_output_tokens=100))
    assert isinstance(result, ContextBudgetExceeded)
    report = result.report
    assert report.largest_observations[0].tool == "read_file"
    assert report.largest_observations[0].call_id == "large-read"
    assert report.largest_observations[0].path == "a.py"
    assert "SECRET_SOURCE_BODY" not in repr(report)


def test_conservative_estimator_counts_utf8_and_no_context_artifacts(tmp_path):
    assert ConservativeTokenEstimator().estimate("中文") >= 2
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    ContextManager(store).build()
    forbidden = {"context_views", "runtime_contexts", "active_code", "budget_reports", "tool_residues", "conversation_windows"}
    assert forbidden.isdisjoint({path.name for path in store.session_dir.iterdir()})


def test_context_loads_packaged_instructions_without_replacing_user_anchor(tmp_path):
    from importlib.resources import files
    from codeagent.skills.builtin import render_behavior_contract_skill
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "fix it"})

    messages = ContextManager(store).build().messages
    # 未注册工具时只装配核心提示，不出现清单/委派工作流说明。
    assert messages[0] == {"role": "system", "content": files("codeagent.prompts").joinpath("system.md").read_text(encoding="utf-8") + render_behavior_contract_skill("main", delegation=False)}
    assert messages[1] == {"role": "system", "content": files("codeagent.prompts").joinpath("tool_policy.md").read_text(encoding="utf-8")}
    # 用户原文保持为锚点；易变 Runtime 状态位于其后，不改动历史前缀。
    assert messages[-2] == {"role": "user", "content": "fix it"}
    assert messages[-1]["role"] == "system" and messages[-1]["content"].startswith("当前 Runtime 状态")


def test_desensitized_real_session_fixture_preserves_success_failure_and_resume_context(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    fixture = Path(__file__).parent / "fixtures" / "context_itsdangerous_events.jsonl"
    store.events_path.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    (root / "a.py").write_text("fixed current source\n", encoding="utf-8")
    messages = ContextManager(store).build().messages
    rendered = json.dumps(messages, ensure_ascii=False)
    assert "继续完成测试并验证" in rendered
    assert "fixed current source" not in rendered
    assert "operation_errors" in rendered and "workspace_changed" in rendered
    assert "old source body" in rendered
    assert "historical_reduction" not in rendered


def test_active_turn_keeps_continuous_raw_tail_without_residue(tmp_path):
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

    messages = ContextManager(store).build().messages
    rendered = json.dumps(messages, ensure_ascii=False)
    assert all(f"RAW-{index}" in rendered for index in range(6))
    tool_results = [json.loads(message["content"]) for message in messages
                    if message["role"] == "tool"]
    assert all("content_retained" not in result for result in tool_results)


def test_hard_budget_requires_semantic_condensation_not_raw_degradation(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "inspect"})
    for index in range(4):
        call_id = f"read-{index}"
        store.append_event("assistant_tool_calls", {
            "reasoning_content": f"reason-{index}",
            "tool_calls": [
                {"call_id": call_id, "name": "read_file", "arguments": {"path": "a.py"}}
            ],
        })
        store.append_event("tool_result", {"call_id": call_id, "result": {
            "ok": True, "content": str(index) * 12_000, "metadata": {"path": "a.py", "sha256": "old"}
        }})

    manager = ContextManager(store)
    result = manager.build(model_capabilities=ModelCapabilities(context_limit=12_000, max_output_tokens=1_000))
    assert isinstance(result, CondensationRequired)
    assert manager.last_budget_report
    assert "recent_raw_to_residue" not in manager.last_budget_report.reductions
    assert result.plan.hard is True


def test_large_context_budget_keeps_tool_observations_intact_and_reasoning_recent(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "inspect"})
    for index in range(12):
        call_id = f"read-{index}"
        store.append_event("assistant_tool_calls", {
            "reasoning_content": f"reason-{index}",
            "tool_calls": [
                {"call_id": call_id, "name": "read_file", "arguments": {"path": "a.py"}}
            ],
        })
        store.append_event("tool_result", {"call_id": call_id, "result": {
            "ok": True, "content": str(index % 10) * 50_000,
            "metadata": {"path": "a.py", "sha256": "old"},
        }})

    manager = ContextManager(store)
    result = manager.build(model_capabilities=ModelCapabilities(context_limit=1_000_000, max_output_tokens=131_072))
    assert isinstance(result, ReadyContext)
    messages = result.messages

    assert manager.last_budget_report
    assert manager.last_budget_report.reductions == ("reasoning_history_omitted",)
    assert manager.last_budget_report.estimated_tokens <= manager.last_budget_report.usable_tokens
    tool_results = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
    assert len(tool_results) == 12
    assert [result["content"] for result in tool_results] == [str(index % 10) * 50_000 for index in range(12)]
    assert all("context_truncated" not in result for result in tool_results)
    reasoning = [message.get("reasoning_content") for message in messages
                 if message.get("tool_calls") and message.get("reasoning_content") is not None]
    assert reasoning == ["reason-11"]
    assert not any("runtime_reasoning_compaction" in str(message.get("content")) for message in messages)
    rendered = json.dumps(messages, ensure_ascii=False)
    assert all(f'"reasoning_content": "reason-{index}"' not in rendered for index in range(10))


def test_completed_turn_keeps_visible_answer_but_not_reasoning_in_next_turn(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "first"})
    store.append_event("assistant_message", {
        "message": "visible answer", "reasoning_content": "private completed reasoning",
    })
    store.append_event("user_message", {"message": "second"})

    manager = ContextManager(store)
    messages = manager.build().messages

    assert any(message.get("content") == "visible answer" for message in messages)
    assert "private completed reasoning" not in json.dumps(messages, ensure_ascii=False)
    assert manager.last_budget_report
    assert "reasoning_history_omitted" in manager.last_budget_report.reductions


def test_hard_budget_never_drops_latest_reasoning_tool_protocol(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "inspect"})
    for index in range(2):
        call_id = f"read-{index}"
        store.append_event("assistant_tool_calls", {
            "reasoning_content": str(index) * 20_000,
            "tool_calls": [{
                "call_id": call_id, "name": "read_file", "arguments": {"path": "a.py"},
            }],
        })
        store.append_event("tool_result", {"call_id": call_id, "result": {
            "ok": True, "content": f"evidence-{index}", "metadata": {"path": "a.py"},
        }})

    manager = ContextManager(store)
    result = manager.build(model_capabilities=ModelCapabilities(context_limit=9_000, max_output_tokens=1_000))
    assert isinstance(result, CondensationRequired)
    assert "reasoning_history_omitted" in result.budget_report.reductions
    assert all(atom.atom_id != "model-step:step-5" for atom in result.plan.source_atoms)


def test_observation_projection_preserves_body_and_metadata(tmp_path):
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

    tool_message = next(message for message in ContextManager(store).build().messages if message["role"] == "tool")
    result = json.loads(tool_message["content"])
    assert "context_truncated" not in result
    assert "context_limit_bytes" not in result["metadata"]
    assert result["metadata"]["returned_range"] == [1, 200]
    assert result["metadata"]["sha256"] == "digest"
    assert "content_retained" not in result
    assert result["content"] == "x" * 20_000


@pytest.mark.parametrize("scope", ["main", "child"])
@pytest.mark.parametrize("batch_size", [1, 2])
def test_tool_body_reaches_agent_and_condenser_independently_of_batch_size(tmp_path, scope, batch_size):
    """同一观察不能因多了一项很短的同批结果而丢失尾部证据。"""
    from codeagent.context.models import ContextState
    from codeagent.context.projector import project_context_events
    from codeagent.orchestration.context import ScopedHistoryStore

    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    body = "x" * 11_760 + "\nREQUIRED_END\n"

    def append(kind, data):
        if scope == "main":
            return store.append_event(kind, data)
        return store.append_event("agent_history", {"agent_id": "child-a", "run_id": "run-a",
            "step_id": "step-a", "kind": kind, "data": data})

    if scope == "child":
        store.append_event("user_message", {"message": "PARENT_PRIVATE"})
    append("user_message", {"message": "inspect"})
    calls = [{"call_id": "read", "name": "read_file", "arguments": {"path": "a.py"}}]
    if batch_size == 2:
        calls.append({"call_id": "search", "name": "search_text", "arguments": {"query": "symbol"}})
    append("assistant_tool_calls", {"tool_calls": calls})
    append("tool_result", {"call_id": "read", "name": "read_file", "result": {
        "ok": True, "content": body, "truncated": False}})
    if batch_size == 2:
        append("tool_result", {"call_id": "search", "name": "search_text", "result": {
            "ok": True, "content": "y" * 82}})
    history = store if scope == "main" else ScopedHistoryStore(
        store, {"agent_id": "child-a", "run_id": "run-a"}, lambda *args, **kwargs: None)
    manager = ContextManager(history, state_provider=lambda: ContextState(str(root), {"readonly": scope == "child"}))
    result = manager.build(model_capabilities=ModelCapabilities(context_limit=128_000))
    assert isinstance(result, ReadyContext)
    observed = next(json.loads(m["content"]) for m in result.messages if m.get("tool_call_id") == "read")
    assert observed["content"] == body and not observed["truncated"]
    assert "context_truncated" not in observed

    atoms = project_context_events(history.read_events()).atoms
    condenser = manager._condenser_messages(list(atoms), None, ModelCapabilities().max_output_tokens)
    sources = [json.loads(line) for line in condenser[1]["content"].splitlines()[1:]]
    source = next(s for s in sources if s["content"].get("call_id") == "read")
    assert source["content"]["result"]["content"] == body
    assert "PARENT_PRIVATE" not in json.dumps(result.messages + condenser)


def test_legacy_command_result_remains_raw_without_residue(tmp_path):
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "run"})
    store.append_event("assistant_tool_calls", {"tool_calls": [{"call_id": "cmd", "name": "run_command", "arguments": {"command": "missing", "purpose": "validation"}}]})
    store.append_event("tool_result", {"call_id": "cmd", "result": {"ok": False, "error_code": "completed",
        "error": "命令未成功完成", "content": "status: completed\nexit_code: 127\nstderr:\n/bin/bash: missing: No such file\neffective_policy: {}",
        "metadata": {"status": "completed", "exit_code": 127}}})
    store.append_event("assistant_message", {"message": "stopped"})
    store.append_event("user_message", {"message": "what happened?"})
    messages = ContextManager(store).build().messages
    tool = next(item for item in messages if item["role"] == "tool")
    result = json.loads(tool["content"])
    assert result["metadata"]["exit_code"] == 127
    assert "No such file" in result["content"]
