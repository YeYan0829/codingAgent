"""最小真实 GLM-5.3 semantic condensation smoke；不会启动 SWE-bench。"""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from codeagent.benchmark.accounting import PriceSnapshot, aggregate_usage, calculate_cost
from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.model_gateway.base import BaseModelClient, ModelRequest
from codeagent.model_gateway.factory import build_model_client
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.runner import AgentRunner
from codeagent.session.store import SessionStore
from codeagent.tools.registry import ToolRegistry


ROOT = Path(__file__).resolve().parents[3]
OUTPUT = ROOT / "benchmarks/swebench/smoke/glm-5.3-semantic-condensation-2026-09-10.json"
ANCHOR = "SEMANTIC_CONDENSATION_SMOKE_TASK_20260910：只回复 SMOKE_OK。"


class RecordingClient(BaseModelClient):
    def __init__(self, delegate: BaseModelClient) -> None:
        self.delegate = delegate
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest):
        self.requests.append(request)
        return self.delegate.complete(request)


def main() -> None:
    temp_root = Path(tempfile.mkdtemp(prefix="codeagent-semantic-smoke-"))
    workspace = temp_root / "workspace"
    workspace.mkdir()
    session_root = temp_root / "sessions"
    config = ModelConfig(
        provider="glm", model="glm-5.3", temperature=1.0, max_tokens=8192,
        reasoning_enabled=True, reasoning_effort="high",
    )
    store = SessionStore(workspace, session_root=session_root).create(
        provider="glm", model="glm-5.3",
        model_options={"reasoning_enabled": True, "reasoning_effort": "high", "max_tokens": 8192},
    )
    anchor_event = store.append_event("user_message", {
        "message": ANCHOR, "max_model_steps_per_user_turn": 100,
    })
    for index in range(45):
        call_id = f"synthetic-read-{index}"
        store.append_event("assistant_tool_calls", {
            "message": f"历史可见步骤 {index}",
            "tool_calls": [{
                "call_id": call_id, "name": "read_file",
                "arguments": {"path": f"historical/file-{index}.py"},
            }],
        })
        store.append_event("tool_result", {
            "call_id": call_id, "name": "read_file",
            "result": {
                "ok": True,
                "content": f"historical evidence {index}: target_symbol_{index}",
                "metadata": {"path": f"historical/file-{index}.py", "sha256": f"digest-{index}"},
            },
        })

    model = RecordingClient(build_model_client(config))
    runner = AgentRunner(
        store, model, ToolRegistry(), AutoApprovalGate(False),
        config=RuntimeConfig(max_steps_per_turn=4, max_model_steps_per_user_turn=100),
        model_config=config,
    )
    output = runner.continue_turn()
    events = store.read_events()
    attempts = [event for event in events if event.type == "context_condensation_attempt"]
    summaries = [event for event in events if event.type == "context_condensed"]
    usage_events = [event for event in events if event.type == "model_usage"]
    by_purpose = {}
    for purpose in ("context_condenser", "main_agent"):
        selected = [event.payload.get("usage") for event in usage_events if event.payload.get("purpose") == purpose]
        by_purpose[purpose] = aggregate_usage(selected)
    total_usage = aggregate_usage([event.payload.get("usage") for event in usage_events])
    price = PriceSnapshot.from_json(ROOT / "benchmarks/swebench/prices/glm-5.3-standard-api-2026-09-09.json")
    cost = calculate_cost(total_usage, price)
    main_requests = [request for request in model.requests if request.purpose == "main_agent"]
    condenser_requests = [request for request in model.requests if request.purpose == "context_condenser"]
    final_main = main_requests[-1]
    memory_messages = [
        message for message in final_main.messages
        if message.get("role") == "user" and "<historical_memory>" in str(message.get("content", ""))
    ]
    anchor_messages = [
        message for message in final_main.messages
        if message.get("role") == "user" and message.get("content") == ANCHOR
    ]
    summary_payload = summaries[-1].payload if summaries else {}
    checks = {
        "condenser_called": bool(condenser_requests),
        "condenser_has_no_tools": all(not request.tools for request in condenser_requests),
        "condenser_effort_low": all(request.reasoning_effort == "low" for request in condenser_requests),
        "valid_attempt_and_success": bool(attempts and summaries and attempts[-1].payload.get("status") == "valid_completion"),
        "summary_wrapper_in_main": len(memory_messages) == 1,
        "current_task_anchor_exact_once": len(anchor_messages) == 1,
        "anchor_source_covered": anchor_event.seq is not None and f"seq:{anchor_event.seq}" in summary_payload.get("newly_covered_event_ids", []),
        "recent_raw_tail_present": any(message.get("role") == "tool" for message in final_main.messages),
        "main_request_fit": bool(runner.context_manager.last_budget_report and
                                 runner.context_manager.last_budget_report.estimated_tokens <=
                                 runner.context_manager.last_budget_report.usable_tokens),
        "main_completed": output.status == "completed" and "SMOKE_OK" in output.final_text,
        "usage_complete": total_usage.get("coverage") == "complete",
    }
    artifact = {
        "schema_version": 1,
        "status": "passed" if all(checks.values()) else "failed",
        "purpose": "GLM-5.3 real semantic condensation Runtime smoke; not a SWE-bench score",
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "provider": "glm",
        "model": "glm-5.3",
        "reasoning": {"enabled": True, "main_effort": "high", "condenser_effort": "low"},
        "context_calibration": {
            "max_events": 80, "target_events": 40, "soft_token_ratio": 0.80,
            "target_token_ratio": 0.50, "summary_max_tokens": 2048,
            "condenser_safety_margin": 2000, "minimum_progress": 0.10,
        },
        "checks": checks,
        "requests": {
            "total": len(model.requests),
            "context_condenser": len(condenser_requests),
            "main_agent": len(main_requests),
        },
        "usage": {"by_purpose": by_purpose, "total": total_usage},
        "cost": cost,
        "events": {
            "attempt_event_ids": [f"seq:{event.seq}" for event in attempts],
            "summary_event_ids": [f"seq:{event.seq}" for event in summaries],
            "logical_covered_event_count": summary_payload.get("logical_covered_event_count"),
            "logical_frontier_source_event_id": summary_payload.get("logical_frontier_source_event_id"),
            "newly_covered_event_ids_sha256": summary_payload.get("newly_covered_event_ids_sha256"),
        },
        "result": {
            "agent_status": output.status,
            "final_text_sha256": hashlib.sha256(output.final_text.encode()).hexdigest(),
            "steps_used_in_turn": output.steps_used_in_turn,
        },
        "notes": [
            "Artifact excludes prompts, summary text, hidden reasoning, API key, and provider request IDs.",
            f"Ephemeral Session root: {session_root}",
        ],
    }
    OUTPUT.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"artifact": str(OUTPUT), "status": artifact["status"], "checks": checks}, ensure_ascii=False))
    if artifact["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
