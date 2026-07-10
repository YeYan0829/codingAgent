from codeagent.runtime.policy import DefaultPolicy, PolicyDecision
from codeagent.tools.base import PermissionLevel, ToolResult, ToolSpec


def make_tool(level):
    return ToolSpec(name=str(level), description="", permission_level=level, schema={}, handler=lambda args: ToolResult(ok=True))


def test_policy_defaults():
    policy = DefaultPolicy()

    assert policy.evaluate(make_tool(PermissionLevel.READ)).decision == PolicyDecision.ALLOW
    assert policy.evaluate(make_tool(PermissionLevel.EXEC_READONLY)).decision == PolicyDecision.ASK
    assert policy.evaluate(make_tool(PermissionLevel.WRITE)).decision == PolicyDecision.DENY
    assert policy.evaluate(make_tool(PermissionLevel.NETWORK)).decision == PolicyDecision.DENY
    assert policy.evaluate(make_tool(PermissionLevel.DANGEROUS)).decision == PolicyDecision.DENY
