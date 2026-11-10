"""产品重开只对账并收敛，不从历史审批或 checkpoint 启动模型。"""
from codeagent.orchestration.lease import session_lease
from codeagent.runtime.turn_state import store_turn_state
from codeagent.application.session_runtime import recover_lifecycle_transition


def reconcile_product_session(store):
    # 存活 owner 持锁时只允许读取，绝不接管它的子任务或工作区。
    with session_lease(store) as epoch:
        recover_lifecycle_transition(store)
        events = store.read_events()
        finished = {e.payload["run_id"] for e in events if e.type == "agent_run_finished"}
        acked = {e.payload["run_id"] for e in events if e.type == "agent_run_ack"}
        interrupted = False
        tool_results = {e.payload["operation_key"] for e in events if e.type == "tool_execution_result"}
        for event in events:
            if event.type != "tool_execution_intent" or not str(event.payload["operation_key"]).startswith("main:"):
                continue
            p = event.payload
            # run_command 在旧图版本曾以 READ 标记，仍按实际可写工具处理。
            unsafe = not p.get("readonly") or p["call"]["name"] == "run_command"
            known = p["operation_key"] in tool_results or any(
                e.type in {"tool_result", "tool_denied"} and e.turn_id == event.turn_id
                and e.payload.get("call_id") == p["call"]["call_id"] for e in events)
            if unsafe and not known:
                interrupted = True
                store.mark_recovery_required("主工具 intent 无可靠 result；现场保留，禁止自动重放或交付")
        for event in events:
            if event.type != "agent_command_intent":
                continue
            run = event.payload["run"]
            rid = run["run_id"]
            facts = []
            if rid not in finished:
                interrupted = True
                records = [e for e in events if e.type == "agent_history" and e.payload["run_id"] == rid]
                closed_calls = {e.payload["data"].get("call_id") for e in records
                                if e.payload["kind"] in {"tool_result", "tool_denied"}}
                for record in records:
                    if record.payload["kind"] != "assistant_tool_calls":
                        continue
                    for call in record.payload["data"]["tool_calls"]:
                        if call["call_id"] in closed_calls:
                            continue
                        from codeagent.tools.base import ToolResult
                        facts.append(("agent_history", {"agent_id": run["agent_id"], "run_id": rid,
                            "step_id": record.payload["step_id"],
                            "kind": "tool_denied", "key": "recovery:" + call["call_id"],
                            "data": {"call_id": call["call_id"], "name": call["name"],
                                     "result": ToolResult(ok=False, error_code="interrupted", error="Runtime connection lost").model_dump()}}))
                        closed_calls.add(call["call_id"])
                facts.append(("agent_run_finished", {
                    "agent_id": run["agent_id"], "run_id": rid, "status": "interrupted",
                    "findings": "", "evidence_refs": [],
                    "error": "Runtime connection lost; no automatic request replay",
                    "limitations": ["执行中断"], "usage_refs": [], "recovered": True,
                }))
            if rid not in acked:
                facts.append(("agent_run_ack", {"run_id": rid, "execution_epoch": epoch, "recovered": True}))
            if facts:
                store.append_derived_events(facts, turn_id=run["root_turn_id"])
        completed_jobs = {e.payload["job_id"] for e in events if e.type == "execution_job_finished"}
        orphan_jobs = [e for e in events if e.type == "execution_job_started" and e.payload["job_id"] not in completed_jobs]
        # 非活跃的内部切片／预算等待可供用户显式继续；断线的活跃 job 必须结束。
        if (orphan_jobs or interrupted) and not store_turn_state(store).closed:
            store.append_event("turn_terminated", {
                "reason": "connection_lost", "message": "Runtime 已中断；现场已保留，请审查后发送新指令。审批没有恢复。",
            })
        for event in orphan_jobs:
            store.append_event("execution_job_finished", {
                "job_id": event.payload["job_id"], "status": "interrupted", "recovered": True,
            })
        acknowledged = {e.payload["job_id"] for e in events if e.type == "execution_stop_acknowledged"}
        for event in events:
            if event.type == "execution_stop_requested" and event.payload["job_id"] not in acknowledged:
                store.append_event("execution_stop_acknowledged", {
                    "job_id": event.payload["job_id"], "boundary": "recovered_without_execution_owner", "recovered": True,
                })
