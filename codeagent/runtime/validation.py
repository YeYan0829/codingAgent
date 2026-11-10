from __future__ import annotations

from typing import Any

from codeagent.runtime.validation_output import reported_validation_issue


def superseded_task_checks(events: list, failures: list, evidence: list, task_id: str) -> set[str]:
    """同一绑定条件的后续失败使该任务的旧成功失效，不影响另一任务/条件。"""
    positions = {e.payload.get('execution_id'): i for i, e in enumerate(events) if e.type == 'validation_completed'}
    failure_positions = {id(e): i for i, e in enumerate(events)}
    invalid = set()
    for success in evidence:
        targets = [t for t in success.get('verification_targets', []) if t.get('task_id') == task_id]
        for failure in failures:
            if (failure_positions[id(failure)] > positions.get(success['execution_id'], -1)
                    and any(t in failure.payload.get('verification_targets', []) for t in targets)):
                invalid.add(success['execution_id'])
                break
    return invalid


def delivery_validation_status(events: list, receipt: dict[str, Any]) -> str:
    """交付后的重新验证可补充证据；原 apply receipt 的状态保持历史身份。"""
    status = receipt.get("delivery_validation_status", "unknown")
    stable = _stable_validation_subjects(events)
    if status == "not_run_on_merged_tree" and any(
        event.type == "validation_completed" and event.payload.get("status") == "passed"
        and event.payload.get("subject_tree") == receipt.get("merged_tree")
        and event.payload.get("workspace_revision") == receipt.get("workspace_revision")
        and stable.get(event.payload.get("execution_id")) == receipt.get("merged_tree")
        for event in events
    ):
        return "passed"
    return status


def _stable_validation_subjects(events: list) -> dict:
    # 旧 v0.5.0 已持久化 before/after tree；只承认能证明被测树稳定的旧记录。
    return {
        event.payload["execution_id"]: event.payload["before_subject_tree"]
        for event in events
        if event.type == "command_completed" and event.payload.get("purpose") == "validation"
        and event.payload.get("status") == "completed" and event.payload.get("exit_code") == 0
        and not reported_validation_issue(event.payload)
        and event.payload.get("audit_complete") is True and event.payload.get("execution_id")
        and (event.payload.get('validation_steps') is None or bool(event.payload['validation_steps'])
             and all(s.get('status') == 'passed' for s in event.payload['validation_steps']))
        and event.payload.get("before_subject_tree")
        and event.payload.get("before_subject_tree") == event.payload.get("after_subject_tree")
    }
