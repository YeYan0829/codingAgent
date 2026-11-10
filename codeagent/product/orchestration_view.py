"""权威事件的产品投影；无调度、模型调用或授权恢复。"""

from codeagent.orchestration.reports import normalize_child_report


def usage_view(events, turn_id=None, run_id=None):
    selected = [e for e in events if turn_id is None or e.turn_id == turn_id]
    reservations = {e.payload["request_id"]: e.payload for e in selected if e.type == "model_request_reserved"
                    and (run_id is None or str(e.payload.get("operation_key", "")).startswith(run_id + ":"))}
    settled = {}
    for e in selected:
        if e.type == "model_request_settled":
            settled.setdefault(e.payload["request_id"], e.payload)
    tokens = 0
    known_usage = 0
    fees = {}
    not_dispatched = 0
    for rid, reservation in reservations.items():
        result = settled.get(rid, {})
        if result.get("outcome") == "not_dispatched":
            not_dispatched += 1
            continue
        usage = result.get("usage") or {}
        if type(usage.get("total_tokens")) is int and usage["total_tokens"] >= 0:
            tokens += usage["total_tokens"]
            known_usage += 1
        currency = reservation.get("fee_currency", "UNKNOWN")
        fee = fees.setdefault(currency, {"currency": currency, "settledMicrounits": 0,
                                       "reservedMicrounits": 0, "unknownMicrounits": 0, "unknownRequests": 0})
        if result.get("fee_state") == "settled_known":
            fee["settledMicrounits"] += result["fee_settled_microunits"]
        elif result.get("fee_state") == "unpriced":
            fee["unpricedRequests"] = fee.get("unpricedRequests", 0) + 1
        elif result:
            fee["unknownMicrounits"] += reservation.get("fee_reserved_microunits", 0)
            fee["unknownRequests"] += 1
        else:
            fee["reservedMicrounits"] += reservation.get("fee_reserved_microunits", 0)
    # 旧单 Agent usage 没有 ledger；有 request_id 的主／摘要镜像不再相加。
    legacy = [e for e in selected if e.type == "model_usage" and not e.payload.get("request_id")]
    if run_id is not None:
        legacy = []
    for event in legacy:
        usage = event.payload.get("usage") or {}
        if type(usage.get("total_tokens")) is int:
            tokens += max(0, usage["total_tokens"])
            known_usage += 1
    requests = len(reservations) - not_dispatched + len(legacy)
    return {"requests": requests, "knownTokens": tokens, "unknownUsageRequests": requests - known_usage,
            "fees": list(fees.values())}


def orchestration_view(events, meta):
    runs, agents, waits, roots = {}, {}, {}, {}
    current_root = next((e.turn_id for e in reversed(events) if e.type == "user_message"), None)
    for event in events:
        p = event.payload
        if event.type == "user_message":
            roots[event.turn_id] = {"rootTurnId": event.turn_id, "parentRunId": "main:" + str(event.turn_id),
                                    "status": "running", "unhandledRuns": [], "failureDecisions": []}
        elif event.type == "agent_command_intent":
            run, agent = p["run"], p["agent"]
            aid, rid = run["agent_id"], run["run_id"]
            agents.setdefault(aid, {"agentId": aid, "spec": agent["spec"]["name"], "status": "idle", "runIds": []})
            previous = runs.get(agents[aid]["runIds"][-1]) if agents[aid]["runIds"] else None
            agents[aid]["runIds"].append(rid)
            runs[rid] = {"runId": rid, "agentId": aid, "rootTurnId": run["root_turn_id"],
                         "parentRunId": "main:" + run["root_turn_id"], "goal": run["message"][:2000],
                         "action": p["action"], "execution": p.get("execution", "foreground"),
                         "contextEpoch": run.get("context_epoch"), "subjectTree": run.get("identity", {}).get("tree"), "status": "queued", "terminalAck": False,
                         "findings": "", "error": None, "limitations": [], "evidenceRefs": [],
                         "stale": False, "verified": False, "failureDecision": None,
                         "executionEpoch": run["execution_epoch"], "commandId": p["command_id"],
                         "eventSeq": event.seq, "cancelRequested": False,
                         "agentLabel": "调查 " + chr(65 + list(agents).index(aid)),
                         "runOrdinal": len(agents[aid]["runIds"]),
                         "revisionMode": ("refresh" if previous and run.get("context_epoch") != previous["contextEpoch"] else "pinned") if "context_epoch" in run else None,
                         "createdAt": event.ts, "startedAt": None, "completedAt": None}
            runs[rid]["description"] = p.get("description") or run.get("description") or run["message"].split("\n", 1)[0][:140]
            runs[rid]["role"] = agent["spec"]["name"]
        elif event.type == "agent_run_running" and p["run_id"] in runs:
            if runs[p["run_id"]]["status"] == "queued":
                runs[p["run_id"]]["status"] = "running"
                runs[p["run_id"]]["startedAt"] = event.ts
        elif event.type == "agent_run_finished" and p["run_id"] in runs:
            run = runs[p["run_id"]]
            # 终态首条权威；迟到通知／重复终态不能复活或覆写。
            if run["status"] in {"queued", "running"}:
                report = normalize_child_report(p)
                run.update(status=report["status"], findings=str(report.get("findings", ""))[:6000],
                           error=report.get("error"), limitations=report.get("limitations", []),
                           evidenceRefs=p.get("evidence_refs", []), completedAt=event.ts, finishedSeq=event.seq)
        elif event.type == "agent_run_ack" and p["run_id"] in runs:
            runs[p["run_id"]]["terminalAck"] = True
        elif event.type == "agent_cancel_requested":
            for rid in p["run_ids"]:
                if rid in runs:
                    runs[rid]["cancelRequested"] = True
        elif event.type == "agent_report_rejected_stale" and p["run_id"] in runs:
            runs[p["run_id"]]["stale"] = True
        elif event.type == "agent_session_closed" and p["agent_id"] in agents:
            agents[p["agent_id"]]["status"] = "closed"
        elif event.type == "agent_wait_started":
            waits[p["wait_id"]] = {"waitId": p["wait_id"], "rootTurnId": event.turn_id,
                                    "runIds": p["run_ids"], "mode": p["mode"], "status": "waiting"}
        elif event.type == "agent_wait_finished" and p["wait_id"] in waits:
            waits[p["wait_id"]].update(status="satisfied" if p["satisfied"] else "timed_out", pending=p["pending"])
        elif event.type == "agent_failures_resolved":
            for rid in p["run_ids"]:
                if rid in runs:
                    runs[rid]["failureDecision"] = {"decision": p["decision"], "reason": p["reason"]}
        elif event.type == "assistant_message" and event.turn_id in roots:
            roots[event.turn_id]["status"] = "completed"
        elif event.type == "turn_terminated" and event.turn_id in roots:
            status = {"connection_lost": "interrupted", "interrupted": "interrupted", "deadline": "timed_out",
                      "user_stop": "stopped", "cancelled": "stopped"}.get(p.get("reason"), "failed")
            roots[event.turn_id].update(status=status,
                                        reason=p.get("reason"))
        elif event.type == "agent_parent_closing" and event.turn_id in roots:
            roots[event.turn_id]["status"] = "closing"
        elif event.type == "agent_root_stopping" and event.turn_id in roots:
            roots[event.turn_id].update(status="stopping", reason=p.get("reason"))
        elif event.type == "turn_budget_exhausted" and event.turn_id in roots:
            roots[event.turn_id]["status"] = "budget_waiting"
        elif event.type in {"execution_slice_exhausted", "turn_budget_increased"} and event.turn_id in roots:
            # slice 已完成回收，产品 job 自动继续；不能一直显示为停止中。
            roots[event.turn_id]["status"] = "running"
            roots[event.turn_id].pop("reason", None)
        if event.type in {"execution_slice_exhausted", "turn_budget_exhausted"}:
            for wait in waits.values():
                if wait["rootTurnId"] == event.turn_id and wait["status"] == "waiting":
                    wait["status"] = "interrupted"
    for run in runs.values():
        agent = agents[run["agentId"]]
        latest = runs[agent["runIds"][-1]]
        run["evidenceStatus"] = "historical_observation"
        run["resourceState"] = "reclaimed" if run["terminalAck"] else (
            "active" if run["status"] in {"queued", "running"} else "awaiting_ack")
        run["usage"] = usage_view(events, run["rootTurnId"], run["runId"])
        if not latest["terminalAck"] and agent["status"] != "closed":
            agent["status"] = "busy"
    for rid, root in roots.items():
        children = [run for run in runs.values() if run["rootTurnId"] == rid]
        failures = [r for r in children if r["status"] not in {"queued", "running", "completed"}]
        root.update(runs=children, waits=[w for w in waits.values() if w["rootTurnId"] == rid],
                    usage=usage_view(events, rid), unhandledRuns=[r["runId"] for r in failures if not r["failureDecision"]],
                    failureDecisions=[r["failureDecision"] for r in failures if r["failureDecision"]])
        if root["status"] == "completed" and failures:
            root["status"] = "completed_with_warnings"
        if root["status"] in {"completed", "completed_with_warnings", "stopped", "interrupted", "failed", "timed_out"}:
            for wait in root["waits"]:
                if wait["status"] == "waiting":
                    wait["status"] = "interrupted"
        if root["status"] == "running" and any(w["status"] == "waiting" for w in root["waits"]):
            root["status"] = "waiting"
    return {"backend": "runtime",
            "currentRootTurnId": current_root, "agents": list(agents.values()), "tasks": list(roots.values()),
            "totalUsage": usage_view(events)}
