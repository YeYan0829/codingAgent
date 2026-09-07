import subprocess

from codeagent.product.service import ProductApplicationService
from codeagent.session.store import SessionStore


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
        "workspace": str(workspace.resolve()),
        "workspaceLabel": "workspace",
        "executionState": "idle",
        "attentionSummary": "Ready",
        "lastActiveAt": store.read_meta()["last_active_at"],
        "changedFileCount": 0,
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
    from codeagent.workspace.git_worktree import GitWorktreeManager

    store.begin_workspace_upgrade(1)
    context = GitWorktreeManager(session_root).create(workspace, store.session_id, 1)
    store.activate_workspace(context)
    (context.active_root / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (context.active_root / "new.txt").write_text("new\n", encoding="utf-8")

    detail = ProductApplicationService(session_root).get_session(store.session_id)

    assert detail["changedFileCount"] == 2
    assert detail["changesSummary"]["files"] == ["new.txt", "tracked.txt"]
    assert detail["attentionSummary"] == "Changes ready for review"


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
    assert turn["items"][1]["summary"] == "1 tool call"
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
        "appliesToCurrentChanges": True,
    }


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
    assert group["summary"] == "2 tool calls · 1 denied · 1 cancelled"
