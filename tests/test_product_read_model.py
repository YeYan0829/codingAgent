import subprocess
import json

import pytest

from codeagent.product.service import ProductApplicationService
from codeagent.session.store import SessionStore


@pytest.mark.parametrize('reason,terminal', [('user_stop', 'cancelled'), ('connection_lost', 'interrupted')])
def test_live_tool_lifecycle_keeps_identity_and_recovery_terminal(tmp_path, reason, terminal):
    from codeagent.product.read_model import timeline_turns
    store, _, _ = _session(tmp_path)
    store.append_event('user_message', {'message': 'fix'})
    store.append_event('assistant_tool_calls', {'message': '先读取，再修改。', 'tool_calls': [
        {'call_id': 'edit', 'name': 'apply_workspace_edit', 'arguments': {'operations': [{'op': 'replace_text', 'path': 'calculator.py'}]}},
        {'call_id': 'pending', 'name': 'read_file', 'arguments': {'path': 'README.md'}},
    ]})
    def group():
        return timeline_turns(store.read_events(), store.read_meta())[0]['items'][1]
    initial = group()
    assert [t['status'] for t in initial['tools']] == ['pending', 'pending']
    assert initial['tools'][0]['argumentSummary'] == 'calculator.py'
    store.append_event('tool_requested', {'call_id': 'edit', 'name': 'apply_workspace_edit'})
    assert [t['status'] for t in group()['tools']] == ['running', 'pending']
    store.append_event('turn_terminated', {'reason': reason})
    final = group()
    assert final['eventSeq'] == initial['eventSeq']
    assert [t['status'] for t in final['tools']] == [terminal, terminal]


def test_receipt_identity_and_bounded_command_output_use_real_results(tmp_path):
    from codeagent.product.read_model import timeline_turns
    store, _, _ = _session(tmp_path)
    store.append_event('user_message', {'message': 'investigate'})
    store.append_event('assistant_tool_calls', {'tool_calls': [
        {'call_id': 'spawn', 'name': 'spawn_agent', 'arguments': {'goal': 'review'}},
        {'call_id': 'cmd', 'name': 'run_command', 'arguments': {'command': 'python3 verify.py'}},
    ]})
    store.append_event('tool_result', {'call_id': 'spawn', 'result': {'ok': True,
        'content': json.dumps({'run_id': 'real-run', 'agent_id': 'real-agent', 'receipt': 'accepted'})}})
    store.append_event('tool_result', {'call_id': 'cmd', 'result': {'ok': True, 'content': 'x' * 15000}})
    tools = timeline_turns(store.read_events(), store.read_meta())[0]['items'][0]['tools']
    assert tools[0]['status'] == 'submitted' and tools[0]['runId'] == 'real-run' and tools[0]['agentId'] == 'real-agent'
    assert tools[1]['outputTruncated'] and len(tools[1]['output']) <= 12000


def _session(tmp_path, title="Inspect parser"):
    root = tmp_path / "sessions"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = SessionStore(workspace, session_root=root).create(title=title)
    return store, root, workspace


def test_product_service_lists_sessions_and_builds_detail(tmp_path):
    store, session_root, workspace = _session(tmp_path)
    store.append_event("user_message", {"message": "Find the parser bug"})
    store.append_event("tool_requested", {"call_id": "1", "name": "read_file", "arguments": {"path": "parser.py"}})
    store.append_event("tool_result", {"call_id": "1", "name": "read_file", "result": {"ok": True}})
    store.append_event("assistant_message", {"message": "The parser is consistent."})

    service = ProductApplicationService(session_root)
    sessions = service.list_sessions(workspace)
    detail = service.get_session(store.session_id)

    assert sessions == [{
        "sessionId": store.session_id,
        "title": "Inspect parser",
        "provider": "fake",
        "model": "fake",
        "reasoningEnabled": False,
        "reasoningEffort": None,
        "workspace": str(workspace.resolve()),
        "workspaceLabel": "workspace",
        "executionState": "idle",
        "attentionSummary": "Ready",
        "lastActiveAt": store.read_meta()["last_active_at"],
        "changedFileCount": 0,
        "changesState": "none",
    }]
    assert [item["role"] for item in detail["conversation"]] == ["user", "assistant"]
    assert detail["provider"] == "fake"
    assert detail["activitySummary"]["counters"]["inspectedFiles"] == 1
    assert detail["activitySummary"]["currentActivity"] == "Tool completed: read_file"
    assert detail["availableActions"]["canContinue"] is False
    assert detail["lastSeq"] == 5


def test_product_service_projects_continue_without_new_task_semantics(tmp_path):
    store, session_root, _ = _session(tmp_path)
    store.append_event("user_message", {"message": "Keep investigating"})
    store.append_event("assistant_tool_calls", {"tool_calls": []})
    store.append_event("execution_slice_exhausted", {"steps_used_in_turn": 1})

    detail = ProductApplicationService(session_root).get_session(store.session_id)

    assert detail["executionState"] == "idle"
    assert detail["attentionSummary"] == "Continue available"
    assert detail["availableActions"]["canContinue"] is True


def test_reopen_projects_unresolved_approval_as_interrupted_not_active(tmp_path):
    store, session_root, _ = _session(tmp_path)
    store.append_event("user_message", {"message": "edit"})
    store.append_event("approval_requested", {"approval_id": "old", "job_id": "gone"})

    detail = ProductApplicationService(session_root).get_session(store.session_id)

    assert detail["executionState"] == "idle"
    assert detail["pendingApproval"] is None
    assert detail["availableActions"]["canResolveApproval"] is False
    assert "interrupted" in detail["interruptedControl"]


def test_product_events_support_after_seq_and_bounded_pages(tmp_path):
    store, session_root, _ = _session(tmp_path)
    store.append_event("user_message", {"message": "one"})
    store.append_event("assistant_message", {"message": "two"})

    service = ProductApplicationService(session_root)
    first = service.get_events(store.session_id, after_seq=0, limit=2)
    second = service.get_events(store.session_id, after_seq=first["lastSeq"], limit=2)

    assert [event["seq"] for event in first["events"]] == [1, 2]
    assert first["hasMore"] is True
    assert [event["seq"] for event in second["events"]] == [3]
    assert second["hasMore"] is False


def test_product_summary_counts_git_worktree_changes(tmp_path):
    store, session_root, workspace = _session(tmp_path)
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=workspace, check=True)
    (workspace / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=workspace, check=True, capture_output=True)
    from codeagent.runtime.atomic_edit import AtomicEditService
    AtomicEditService(store.workspace_context(), store).apply_workspace_edit({"operations": [
        {"op": "replace_text", "path": "tracked.txt", "old_text": "base", "new_text": "changed"},
        {"op": "create_file", "path": "new.txt", "content": "new\n"},
    ]})
    detail = ProductApplicationService(session_root).get_session(store.session_id)
    assert (workspace / "tracked.txt").read_text() == "changed\n"
    assert not detail["availableActions"]["canAcceptChanges"]
    assert not detail["availableActions"]["canDiscardChanges"]
    assert detail["availableActions"]["canRun"]


def test_validation_failure_is_attention_not_task_failure(tmp_path):
    store, session_root, _ = _session(tmp_path)
    store.append_event("command_completed", {
        "execution_id": "exec-1", "command": "pytest", "purpose": "validation",
        "status": "completed", "exit_code": 1, "duration_ms": 25,
    })

    detail = ProductApplicationService(session_root).get_session(store.session_id)

    assert detail["executionState"] == "idle"
    assert detail["attentionSummary"] == "Validation failed"
    assert detail["validationSummary"]["status"] == "failed"
    assert detail["validationSummary"]["appliesToCurrentChanges"] is False


def test_timeline_interleaves_agent_text_and_deduplicated_tool_activity(tmp_path):
    store, session_root, _ = _session(tmp_path)
    store.append_event("user_message", {"message": "Inspect README"})
    store.append_event("assistant_tool_calls", {
        "message": "I will inspect the repository.",
        "tool_calls": [{"call_id": "call-1", "name": "read_file", "arguments": {"path": "README.md"}}],
    })
    store.append_event("tool_requested", {"call_id": "call-1", "name": "read_file", "arguments": {"path": "README.md"}})
    store.append_event("tool_result", {"call_id": "call-1", "name": "read_file", "result": {"ok": True}})
    store.append_event("assistant_message", {"message": "README describes the runtime."})

    turn = ProductApplicationService(session_root).get_session(store.session_id)["turns"][0]

    assert turn["userMessage"] == "Inspect README"
    assert [item["type"] for item in turn["items"]] == ["markdown", "activityGroup", "markdown"]
    assert turn["items"][1]["summary"] == "读取文件 1 次"
    assert turn["items"][1]["tools"] == [{
        "callId": "call-1", "tool": "read_file", "summary": "Read README.md",
        "argumentSummary": "README.md", "status": "succeeded", "detail": {}, "result": None,
    }]


def test_timeline_attaches_changes_and_validation_to_turn_end(tmp_path):
    store, session_root, _ = _session(tmp_path)
    store.append_event("user_message", {"message": "Fix parser"})
    store.append_event("assistant_tool_calls", {
        "tool_calls": [
            {"call_id": "edit-1", "name": "apply_workspace_edit", "arguments": {"operations": []}},
            {"call_id": "cmd-1", "name": "run_command", "arguments": {"command": "pytest"}},
        ],
    })
    store.append_event("edit_transaction", {"changed_files": ["parser.py", "tests/test_parser.py"]})
    store.append_event("command_completed", {
        "execution_id": "exec-1", "command": "pytest", "purpose": "validation", "status": "completed",
        "exit_code": 0, "duration_ms": 40, "changed_paths": ["parser.py"],
    })
    store.append_event("validation_completed", {
        "execution_id": "exec-1", "command": "pytest", "candidate_revision": 0, "exit_code": 0,
    })
    store.append_event("assistant_message", {"message": "Fixed and validated."})

    turn = ProductApplicationService(session_root).get_session(store.session_id)["turns"][0]

    assert turn["changedFiles"] == ["parser.py", "tests/test_parser.py"]
    assert turn["validation"] == {
        "command": "pytest", "status": "passed", "exitCode": 0, "durationMs": 40,
        "appliesToCurrentChanges": False,
    }


def test_timeline_projects_context_summary_as_system_item_without_duplicate_user_message(tmp_path):
    store, session_root, _ = _session(tmp_path)
    store.append_event("user_message", {"message": "Fix the parser"})
    store.append_event("context_condensation_attempt", {
        "status": "request_failed", "failure_code": "TimeoutError",
    })
    store.append_event("model_usage", {
        "purpose": "context_condenser", "model": "glm-5.3",
        "finish_reason": "stop", "usage": {"total_tokens": 120},
    })
    store.append_event("context_condensation_attempt", {"status": "valid_completion"})
    store.append_event("context_condensed", {
        "newly_covered_event_ids": ["seq:1", "seq:2", "seq:3"],
        "summary": "USER_REQUIREMENTS: Preserve parser behavior.",
        "summary_estimated_tokens": 41, "input_estimated_tokens": 4000,
        "reason": "soft_limit",
    })
    store.append_event("assistant_message", {"message": "Done."})

    detail = ProductApplicationService(session_root).get_session(store.session_id)
    turn = detail["turns"][0]
    item = next(value for value in turn["items"] if value["type"] == "contextSummary")

    assert turn["userMessage"] == "Fix the parser"
    assert sum(value["role"] == "user" for value in detail["conversation"]) == 1
    assert item["title"] == "Historical context summarized"
    assert item["coveredEventCount"] == 3 and item["summaryEstimatedTokens"] == 41
    assert item["condenser"]["model"] == "glm-5.3"
    assert item["retryCount"] == 1
    assert sum(value["type"] == "contextSummary" for value in turn["items"]) == 1
    assert not any("TimeoutError" in str(value) for value in turn["items"])


def test_timeline_groups_recovered_truncations_and_explains_terminal_context_failure(tmp_path):
    store, session_root, _ = _session(tmp_path)
    store.append_event("user_message", {"message": "Continue"})
    store.append_event("model_output_truncated", {"finish_reason": "length"})
    store.append_event("model_output_truncated", {"finish_reason": "length"})
    store.append_event("turn_terminated", {"reason": "context_budget_exceeded"})

    detail = ProductApplicationService(session_root).get_session(store.session_id)
    items = detail["turns"][0]["items"]

    assert items[0]["kind"] == "truncationRecovery" and items[0]["count"] == 2
    assert "2 truncated" in items[0]["message"]
    assert "safe model request" in items[1]["message"]
    assert detail["globalAttention"]["kind"] == "context_limit"


def test_detail_projects_delivery_receipt_and_execution_error(tmp_path):
    store, session_root, _ = _session(tmp_path)
    store.append_event("user_message", {"message": "Apply the fix"})
    store.append_event("changes_accepted", {"files": ["parser.py"]})
    store.append_event("turn_terminated", {
        "reason": "runtime_error", "message": "The Runtime failed before completion",
    })

    detail = ProductApplicationService(session_root).get_session(store.session_id)

    assert detail["deliveryReceipt"]["kind"] == "accepted"
    assert detail["deliveryReceipt"]["message"] == "Changes applied to your repository"
    assert detail["globalAttention"] == {
        "kind": "execution_error", "title": "Agent execution stopped",
        "message": "The Runtime failed before completion",
        "action": "Retry the request or open CodeAgent Runtime output for details.",
    }


def test_timeline_distinguishes_denied_and_cancelled_tool_calls(tmp_path):
    store, session_root, _ = _session(tmp_path)
    store.append_event("user_message", {"message": "Inspect and stop"})
    store.append_event("assistant_tool_calls", {"tool_calls": [
        {"call_id": "denied", "name": "read_file", "arguments": {"path": "secret.txt"}},
        {"call_id": "pending", "name": "search_text", "arguments": {"query": "TODO"}},
    ]})
    store.append_event("tool_denied", {"call_id": "denied", "reason": "permission denied"})
    store.append_event("turn_terminated", {"reason": "user_stop", "message": "Stopped by user"})

    group = ProductApplicationService(session_root).get_session(store.session_id)["turns"][0]["items"][0]

    assert [tool["status"] for tool in group["tools"]] == ["denied", "cancelled"]
    assert group["status"] == "denied"
    assert group["summary"] == "读取文件 1 次 · 搜索 1 次 · 1 项被拒绝 · 1 项取消"


def test_failed_command_projects_exit_code_stderr_summary_and_bounded_output(tmp_path):
    store, session_root, _ = _session(tmp_path)
    store.append_event("user_message", {"message": "Run verification"})
    store.append_event("assistant_tool_calls", {"tool_calls": [{
        "call_id": "command-1", "name": "run_command", "arguments": {"command": "python3 verify.py"},
    }]})
    long_output = "stdout\n" + ("detail\n" * 3000) + "ZeroDivisionError: division by zero\n"
    store.append_event("tool_result", {
        "call_id": "command-1", "name": "run_command", "result": {
            "ok": False,
            "content": long_output,
            "error": "命令未成功完成",
            "error_code": "command_exit_nonzero",
            "metadata": {
                "status": "completed", "exit_code": 1,
                "stderr_tail": "Traceback (most recent call last):\nZeroDivisionError: division by zero",
            },
        },
    })

    group = ProductApplicationService(session_root).get_session(store.session_id)["turns"][0]["items"][0]
    failure = group["tools"][0]["failure"]

    assert group["defaultExpanded"] is True
    assert failure["kind"] == "command_exit_nonzero"
    assert failure["title"] == "Command exited with code 1"
    assert failure["exitCode"] == 1
    assert failure["summary"] == "ZeroDivisionError: division by zero"
    assert failure["outputTruncated"] is True
    assert len(failure["output"]) <= 12_000
    assert "output truncated" in failure["output"]


def test_timeout_and_policy_denial_have_distinct_failure_messages(tmp_path):
    store, session_root, _ = _session(tmp_path)
    store.append_event("user_message", {"message": "Try protected commands"})
    store.append_event("assistant_tool_calls", {"tool_calls": [
        {"call_id": "timeout", "name": "run_command", "arguments": {"command": "sleep 30"}},
        {"call_id": "denied", "name": "run_command", "arguments": {"command": "curl example.com"}},
    ]})
    store.append_event("tool_result", {"call_id": "timeout", "name": "run_command", "result": {
        "ok": False, "error": "命令未成功完成", "error_code": "execution_timed_out",
        "metadata": {"status": "execution_timed_out", "exit_code": -15},
    }})
    store.append_event("tool_denied", {"call_id": "denied", "name": "run_command", "result": {
        "ok": False, "error": "Network access is disabled", "error_code": "policy_denied",
    }})

    tools = ProductApplicationService(session_root).get_session(store.session_id)["turns"][0]["items"][0]["tools"]

    assert tools[0]["failure"]["kind"] == "timeout"
    assert tools[0]["failure"]["title"] == "Command timed out"
    assert tools[1]["failure"]["kind"] == "policy_denied"
    assert tools[1]["failure"]["title"] == "Operation blocked by policy"


@pytest.mark.parametrize("legacy_payload", [
    {"kind": "workspace_upgrade", "approval_id": "old-workspace", "job_id": "gone"},
    {"name": "apply_workspace_edit", "reason": "创建隔离 Agent worktree"},
])
def test_obsolete_workspace_confirmation_is_historical_on_reopen(tmp_path, legacy_payload):
    store, session_root, _ = _session(tmp_path)
    store.append_event("approval_requested", legacy_payload)
    detail = ProductApplicationService(session_root).get_session(store.session_id)
    assert detail["pendingApproval"] is None
    assert detail["interruptedControl"] is None
    assert detail["globalAttention"] is None
    assert detail["availableActions"]["canRun"]


def test_consecutive_model_tool_roundtrips_share_one_activity_entry(tmp_path):
    """工具请求、结果、用量事件不应给用户制造新的段落。"""
    store, session_root, _ = _session(tmp_path)
    store.append_event('user_message', {'message': 'inspect and verify'})
    for i, name in enumerate(['list_dir', 'read_file', 'read_file', 'run_command', 'spawn_agent']):
        store.append_event('assistant_tool_calls', {'tool_calls': [
            {'call_id': str(i), 'name': name, 'arguments': {'path': 'README.md'}},
        ]})
        store.append_event('tool_result', {'call_id': str(i), 'result': {'ok': True}})
        store.append_event('model_usage', {'model': 'fake', 'usage': {'total_tokens': 1}})
    service = ProductApplicationService(session_root)
    first = service.get_session(store.session_id)['turns'][0]['items']
    assert len(first) == 1
    assert len(first[0]['tools']) == 5
    assert first[0]['summary'] == '读取文件 2 次 · 浏览目录 1 次 · 运行 1 条命令 · 子任务操作 1 次'
    assert first[0]['tools'][-1]['status'] == 'submitted'
    assert first[0]['status'] == 'submitted'
    assert first == service.get_session(store.session_id)['turns'][0]['items']
    store.append_event('assistant_message', {'message': 'inspection done'})
    assert service.get_session(store.session_id)['turns'][0]['items'][0]['eventSeq'] == first[0]['eventSeq']


def test_activity_keeps_visible_narrative_approval_and_turn_boundaries(tmp_path):
    store, session_root, _ = _session(tmp_path)
    store.append_event('user_message', {'message': 'inspect'})
    for i in range(4):
        message = {'message': 'Now verify.'} if i == 1 else {}
        store.append_event('assistant_tool_calls', {**message, 'tool_calls': [
            {'call_id': str(i), 'name': 'read_file', 'arguments': {'path': 'README.md'}},
        ]})
        if i == 1:
            store.append_event('approval_requested', {'approval_id': 'a', 'job_id': 'ended'})
        store.append_event('tool_result', {'call_id': str(i), 'result': {'ok': i != 2, 'error': 'read failed' if i == 2 else None}})
    store.append_event('assistant_message', {'message': 'done'})
    store.append_event('user_message', {'message': 'another request'})
    store.append_event('assistant_tool_calls', {'tool_calls': []})
    store.append_event('assistant_tool_calls', {'tool_calls': [
        {'call_id': 'next', 'name': 'list_dir', 'arguments': {}},
    ]})
    turns = ProductApplicationService(session_root).get_session(store.session_id)['turns']
    assert [item['type'] for item in turns[0]['items']] == [
        'activityGroup', 'markdown', 'activityGroup', 'notice', 'activityGroup', 'markdown',
    ]
    assert [len(item['tools']) for item in turns[0]['items'] if item['type'] == 'activityGroup'] == [1, 1, 2]
    assert turns[0]['items'][4]['status'] == 'failed'
    assert turns[0]['items'][4]['tools'][0]['failure']['summary'] == 'read failed'
    assert len(turns[1]['items']) == 1
    assert turns[1]['items'][0]['summary'] == '浏览目录 1 次'


@pytest.mark.parametrize('model,translated', [('c3-fixture', True), ('fake', False), ('real-model', False)])
def test_legacy_fixture_display_preserves_original_response_and_events(tmp_path, model, translated):
    from codeagent.product.read_model import timeline_turns
    store, _, _ = _session(tmp_path)
    legacy = ('Free protocol fixture finished: background delegation, parent work, wait, persistent follow-up and refresh. '
              'Child reports are not validation. Review Diff and the actual validation status before Accept/Discard.')
    store.append_event('user_message', {'message': 'C3 edit'})
    store.append_event('assistant_message', {'message': legacy})
    before = store.read_events()
    turns = timeline_turns(before, {**store.read_meta(), 'provider': 'fake', 'model': model})
    message = turns[0]['items'][0]
    assert message['text'] == legacy
    assert ('displayText' in message) is translated
    if translated:
        assert message['displayText'] == '本次演练已结束。'
    assert [e.model_dump() for e in store.read_events()] == [e.model_dump() for e in before]
