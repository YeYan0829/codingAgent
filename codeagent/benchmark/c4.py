"""正式测评共用的单 Agent 工具适配与请求账本统计。"""
from __future__ import annotations

from collections import Counter

from codeagent.benchmark.accounting import aggregate_usage, zero_usage
from codeagent.tools.registry import ToolRegistry

ORCHESTRATION_TOOLS = frozenset({
    "task", "delegate_readonly", "spawn_agent", "send_agent", "wait_agents",
    "cancel_agents", "close_agent", "resolve_agent_failures",
})


class SingleAgentRegistry(ToolRegistry):
    """保持原工具执行与权限，只屏蔽委派注册；伪造工具调用也无法获取 handler。"""
    def register(self, tool):
        if tool.name not in ORCHESTRATION_TOOLS:
            super().register(tool)


def disable_delegation(runner):
    def filtered(registry):
        result = SingleAgentRegistry(registry.workspace_context, workspace_backend=registry.workspace_backend)
        for tool in registry.list_tools():
            result.register(tool)
        return result

    runner.tools = filtered(runner.tools)
    runner.context_manager.tool_registry = runner.tools
    return runner


def ledger_summary(events):
    """计 reservation 而非镜像 model_usage；UNKNOWN、不完整 usage 与冲突均显式保留。"""
    reserved, settled = {}, {}
    for event in events:
        if event.type not in {"model_request_reserved", "model_request_settled"}:
            continue
        target = reserved if event.type == "model_request_reserved" else settled
        rid = event.payload["request_id"]
        if rid in target and target[rid] != event.payload:
            raise ValueError("conflicting request identity: " + rid)
        target[rid] = event.payload
    orphan = sorted(set(settled) - set(reserved))
    if orphan:
        raise ValueError("orphan settlement: " + str(orphan))
    groups, rows = {}, []
    for rid, reservation in reserved.items():
        result = settled.get(rid, {})
        purpose = reservation["purpose"]
        usage = result.get("usage")
        if result.get("outcome") != "not_dispatched":
            groups.setdefault(purpose, []).append(usage)
        rows.append({"request_id": rid, "purpose": purpose, "agent_id": reservation["agent_id"],
                     "outcome": result.get("outcome", "pending"), "usage": usage,
                     "currency": reservation.get("fee_currency"),
                     "reservation_microunits": reservation.get("fee_reserved_microunits"),
                     "fee_state": result.get("fee_state", "pending"),
                     "conservative_settled_microunits": result.get("fee_settled_microunits")})
    legacy = [e.payload.get("usage") for e in events
              if e.type == "model_usage" and not e.payload.get("request_id")]
    if reserved and legacy:
        raise ValueError("mixed unlinked legacy usage requires manual reconciliation")
    usages = [row["usage"] for row in rows if row["outcome"] != "not_dispatched"] if reserved else legacy
    return {"requests": len(usages), "reservation_count": len(reserved),
            "physical_dispatch_count": None,  # reservation 是物理请求上界，故障时不等于 dispatch。
            "usage": aggregate_usage(usages) if usages else zero_usage(),
            "by_purpose": {k: aggregate_usage(v) for k, v in groups.items()},
            "unknown_or_pending_request_ids": [r["request_id"] for r in rows if r["fee_state"] not in {"settled_known", "unpriced", "not_dispatched"}],
            "unpriced_request_ids": [r["request_id"] for r in rows if r["fee_state"] == "unpriced"],
            "execution_unknown_or_pending_request_ids": [r["request_id"] for r in rows if r["outcome"] not in {"completed", "protocol_error", "not_dispatched"}],
            "outcomes": dict(Counter(r["outcome"] for r in rows)), "requests_detail": rows,
            "actual_provider_cost": None, "cost_note": "需独立冻结价格快照；账本保守结算不是 Provider 发票"}
