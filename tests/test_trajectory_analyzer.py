from codeagent.benchmark.trajectory import analyze_trajectory
from codeagent.session.events import SessionEvent


def ev(kind, payload=None):
    return SessionEvent(type=kind, payload=payload or {})


def call(step_id, name, args):
    return [ev("model_usage", {"usage": {"total_tokens": 10}}),
            ev("assistant_tool_calls", {"tool_calls": [{"call_id": step_id, "name": name, "arguments": args}]}),
            ev("tool_result", {"call_id": step_id, "name": name, "arguments": args,
                               "result": {"ok": True, "metadata": {"sha256": "same"}}})]


def test_normal_trajectory_and_repetition_boundaries():
    events = [ev("user_message")]
    events += call("s", "search_text", {"query": "x"})
    events += call("r1", "read_file", {"path": "a.py", "start_line": 1, "end_line": 5})
    events += call("r2", "read_file", {"path": "a.py", "start_line": 1, "end_line": 5})
    events += call("e", "apply_workspace_edit", {})
    events += [ev("edit_transaction", {"candidate_revision": 1})]
    events += call("r3", "read_file", {"path": "a.py", "start_line": 1, "end_line": 5})
    events[-1].payload["result"]["metadata"]["sha256"] = "changed"
    events += call("v", "run_command", {"command": " pytest   -q ", "purpose": "validation"})
    events += [ev("validation_completed", {"status": "passed"}),
               ev("model_usage", {"usage": {"total_tokens": 7}}), ev("assistant_message")]
    result = analyze_trajectory(events)
    assert result["model_steps"] == 7
    assert result["first_edit_step"] == result["last_edit_step"] == 4
    assert result["exact_same_revision_read_count"] == 1
    assert result["validation_count"] == result["validation_success_count"] == 1
    assert result["post_last_edit_steps"] == 3
    assert result["post_last_edit_token_usage"]["total_tokens"] == 27


def test_failed_or_noop_edit_is_not_effective_and_validation_then_edit_is_visible():
    events = [ev("user_message")]
    events += [ev("model_usage", {"usage": None}), ev("assistant_tool_calls", {"tool_calls": [
        {"call_id": "bad", "name": "apply_workspace_edit", "arguments": {}}]}),
        ev("tool_result", {"call_id": "bad", "name": "apply_workspace_edit", "arguments": {},
                           "result": {"ok": False}})]
    events += call("v", "run_command", {"command": "pytest -q", "purpose": "validation"})
    events += [ev("validation_completed")]
    events += call("e", "apply_workspace_edit", {})
    events += [ev("edit_transaction", {"candidate_revision": 1}), ev("assistant_message")]
    result = analyze_trajectory(events)
    assert result["edit_count"] == 1
    assert result["edit_after_successful_validation"] is True


def test_context_and_step_termination_are_factual():
    context = analyze_trajectory([ev("turn_terminated", {"reason": "context_budget_exceeded",
        "estimated_tokens": 20, "usable_tokens": 10, "reductions": ["minimum_set"]})])
    assert context["context_exceeded"] and context["context_detail"]["minimum_set_composition"] is None
    step = analyze_trajectory([ev("turn_terminated", {"reason": "model_step_budget_exhausted"})])
    assert step["step_exhausted"]


def test_trajectory_distinguishes_completion_empty_patch_and_output_truncation():
    completed = analyze_trajectory([ev("assistant_message", {"message": "done"})], {"patch_present": True})
    empty = analyze_trajectory([ev("assistant_message", {"message": "done"})], {"patch_present": False})
    truncated = analyze_trajectory([
        ev("model_usage", {"finish_reason": "length", "usage": {"total_tokens": 20}}),
        ev("model_output_truncated", {"finish_reason": "length"}),
        ev("turn_terminated", {"reason": "output_truncated"}),
    ], {"patch_present": False})

    assert completed["completed"] and not completed["empty_patch"] and not completed["output_truncated"]
    assert empty["completed"] and empty["empty_patch"] and not empty["output_truncated"]
    assert truncated["termination_reason"] == "output_truncated"
    assert truncated["output_truncated"] and truncated["output_truncation_steps"] == 1
    assert truncated["model_steps"] == 1 and not truncated["completed"]


def test_truncation_recovery_and_context_identity_are_projected():
    events = [
        ev("user_message"),
        ev("model_usage", {"purpose": "main_agent", "finish_reason": "length",
            "reasoning_effort": "high", "usage": {"total_tokens": 20},
            "context": {"anchor_source_event_id": "seq:1", "retained_raw_source_event_ids": ["seq:2"]}}),
        ev("model_output_truncated", {"finish_reason": "length"}),
        ev("model_usage", {"purpose": "main_agent", "finish_reason": "tool_calls",
            "reasoning_effort": "low", "context_reductions": ["output_truncation_partial_replayed"],
            "usage": {"total_tokens": 10}, "context": {"anchor_source_event_id": "seq:1"}}),
        ev("assistant_tool_calls", {"tool_calls": []}),
    ]
    result = analyze_trajectory(events)
    recovery = result["truncation_recovery"]
    assert recovery["high_to_low_count"] == recovery["one_request_recovery_count"] == 1
    assert recovery["records"][0]["partial_replayed"] is True
    assert result["latest_context_snapshot"]["anchor_source_event_id"] == "seq:1"
    assert result["usage_by_purpose"]["main_agent"]["requests"] == 2


def test_context_breakdown_is_projected_and_legacy_remains_unavailable():
    payload = {"reason": "context_budget_exceeded", "estimated_tokens": 20, "usable_tokens": 10,
        "minimum_set_components": [{"category": "recent_tool_observations", "estimated_tokens": 12,
                                     "item_count": 2}],
        "largest_observations": [{"tool": "read_file", "call_id": "c", "path": "a.py",
                                  "estimated_tokens": 8}],
        "component_estimated_tokens": 19, "estimation_residual": 1}
    detail = analyze_trajectory([ev("turn_terminated", payload)])["context_detail"]
    assert detail["minimum_set_composition"][0]["estimated_tokens"] == 12
    assert detail["largest_observations"][0]["path"] == "a.py"
    assert detail["unavailable_reason"] is None


def test_identical_command_normalization():
    events = call("a", "run_command", {"command": "pytest  -q", "purpose": "utility"})
    events += call("b", "run_command", {"command": " pytest -q ", "purpose": "utility"})
    assert analyze_trajectory(events)["normalized_identical_command_repeat_count"] == 1


def test_validation_event_before_tool_result_is_not_double_counted():
    events = [ev("model_usage", {"usage": {"total_tokens": 1}}),
              ev("assistant_tool_calls", {"tool_calls": [{"call_id": "v", "name": "run_command",
                  "arguments": {"command": "pytest", "purpose": "validation"}}]}),
              ev("validation_completed", {"status": "passed"}),
              ev("tool_result", {"call_id": "v", "name": "run_command",
                  "arguments": {"command": "pytest", "purpose": "validation"}, "result": {"ok": True}})]
    result = analyze_trajectory(events)
    assert result["validation_count"] == 1 and result["validation_success_count"] == 1
