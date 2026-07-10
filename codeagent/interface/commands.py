from __future__ import annotations

import json

from rich.console import Console

from codeagent.model_gateway.base import LLMToolCall
from codeagent.runtime.runner import AgentRunner
from codeagent.tools.registry import ToolRegistry


def print_tools(console: Console, registry: ToolRegistry) -> None:
    for tool in registry.list_tools():
        console.print(f"[bold]{tool.name}[/bold] {tool.permission_level}: {tool.description}")


def handle_manual_call(console: Console, runner: AgentRunner, raw: str) -> None:
    runner.session_store.append_event("cli_command", {"command": raw})
    parts = raw.split(" ", 2)
    if len(parts) < 2:
        console.print("[red]用法：/call <tool_name> <json_args>[/red]")
        return
    name = parts[1]
    try:
        args = json.loads(parts[2]) if len(parts) == 3 else {}
    except json.JSONDecodeError as exc:
        console.print(f"[red]JSON 参数错误：{exc}[/red]")
        return
    step = runner._handle_tool_call(LLMToolCall(call_id="manual-call", name=name, arguments=args))
    result = step["result"]
    if result.get("ok"):
        console.print(result.get("content", ""), markup=False)
    else:
        console.print(f"[red]{result.get('error')}[/red]")
