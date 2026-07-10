from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from codeagent.config import ModelConfig
from codeagent.interface.commands import handle_manual_call, print_tools
from codeagent.model_gateway.factory import build_model_client
from codeagent.runtime.approval import AutoApprovalGate, ConsoleApprovalGate
from codeagent.runtime.runner import AgentRunner
from codeagent.session.events import SessionEvent
from codeagent.session.store import SessionStore, SessionStoreError
from codeagent.tools.fs_read import build_fs_tools
from codeagent.tools.git_read import build_git_tools
from codeagent.tools.registry import ToolRegistry
from codeagent.workspace.workspace import Workspace

app = typer.Typer(help="CodeAgent Runtime v0.2")
console = Console()

WORKSPACE_ARG = typer.Argument(Path("."), help="要进入的代码工作区目录；默认 '.' 表示当前目录。")
SESSION_ID_ARG = typer.Argument(..., help="要恢复的 session id，可通过 list-sessions 查看。")
MESSAGE_ARG = typer.Argument(..., help="本轮要发送给 agent 的用户消息。")
PROVIDER_OPT = typer.Option("fake", "--provider", help="模型 provider：fake 或 deepseek。默认 fake。")
MODEL_OPT = typer.Option(None, "--model", help="provider 内的模型编号；deepseek 默认 deepseek-v4-flash。")


def build_registry(workspace: Workspace) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in build_fs_tools(workspace.guard) + build_git_tools(workspace.root):
        registry.register(tool)
    return registry


def build_runner(store: SessionStore, workspace: Workspace, *, interactive: bool, model_config: ModelConfig) -> AgentRunner:
    return AgentRunner(
        session_store=store,
        model=build_model_client(model_config),
        tools=build_registry(workspace),
        approval_gate=ConsoleApprovalGate() if interactive else AutoApprovalGate(allow=False),
        model_config=model_config,
    )


@app.command()
def start(workspace: Path = WORKSPACE_ARG, provider: str = PROVIDER_OPT, model: str | None = MODEL_OPT):
    """创建新 session 并进入交互 CLI。"""
    model_config = ModelConfig(provider=provider, model=model)
    ws = Workspace(workspace)
    store = SessionStore(ws.root).create(provider=model_config.provider, model=model_config.resolved_model)
    console.print(Panel(f"session: {store.session_id}\nworkspace: {ws.root}\nprovider/model: {model_config.provider} / {model_config.resolved_model}", title="CodeAgent"))
    _interactive_loop(store, ws, model_config)


@app.command()
def ask(workspace: Path = WORKSPACE_ARG, message: str = MESSAGE_ARG, provider: str = PROVIDER_OPT, model: str | None = MODEL_OPT):
    """单次非交互运行，用于 smoke test。"""
    model_config = ModelConfig(provider=provider, model=model)
    ws = Workspace(workspace)
    store = SessionStore(ws.root).create(provider=model_config.provider, model=model_config.resolved_model)
    runner = build_runner(store, ws, interactive=False, model_config=model_config)
    output = runner.run_turn(message)
    for step in output.steps:
        console.print(_format_tool_step(step))
        if step["result"].get("content"):
            console.print(step["result"]["content"], markup=False)
        if step["result"].get("error"):
            console.print(f"[red]{step['result']['error']}[/red]")
    console.print(Panel(Text(output.final_text), title="Assistant"))


@app.command("list-sessions")
def list_sessions(workspace: Path = WORKSPACE_ARG):
    """列出 workspace 下的 session。"""
    ws = Workspace(workspace)
    sessions = SessionStore.list_sessions(ws.root)
    if not sessions:
        console.print("没有 session。")
        return
    for meta in sessions:
        provider = meta.get("provider", "fake")
        console.print(f"{meta.get('session_id')}  {meta.get('last_active_at')}  {meta.get('mode')}  {provider}/{meta.get('model')}")


@app.command()
def resume(
    session_id: str = SESSION_ID_ARG,
    workspace: Path = WORKSPACE_ARG,
    provider: str | None = typer.Option(None, "--provider", help="覆盖 session meta 中的 provider。"),
    model: str | None = MODEL_OPT,
):
    """恢复已有 session 并进入交互 CLI。"""
    ws = Workspace(workspace)
    try:
        store = SessionStore(ws.root, session_id=session_id).load()
    except SessionStoreError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    meta = store.read_meta()
    model_config = ModelConfig(provider=provider or meta.get("provider", "fake"), model=model or meta.get("model"))
    console.print(Panel(_format_session_overview(store), title="Resumed Session"))
    _interactive_loop(store, ws, model_config)


def _interactive_loop(store: SessionStore, ws: Workspace, model_config: ModelConfig) -> None:
    runner = build_runner(store, ws, interactive=True, model_config=model_config)
    registry = runner.tools
    while True:
        try:
            raw = console.input("[bold]> [/bold]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n退出。")
            return
        if not raw:
            continue
        if raw == "/exit":
            return
        if raw == "/status":
            console.print(Panel(_format_session_overview(store), title="Session Status"))
            continue
        if raw == "/tools":
            print_tools(console, registry)
            continue
        if raw == "/log":
            console.print(str(store.transcript_path))
            continue
        if raw.startswith("/call"):
            handle_manual_call(console, runner, raw)
            continue
        output = runner.run_turn(raw)
        for step in output.steps:
            console.print(_format_tool_step(step))
        console.print(Panel(Text(output.final_text), title="Assistant"))


def _format_tool_step(step: dict) -> str:
    args_text = json.dumps(step.get("arguments", {}), ensure_ascii=False, sort_keys=True)
    result = step.get("result", {})
    return f"[cyan]tool[/cyan] {step.get('tool')} {args_text} -> ok={result.get('ok')} truncated={result.get('truncated')}"


def _format_session_overview(store: SessionStore, recent_count: int = 6) -> str:
    meta = store.read_meta()
    events = store.read_events()
    lines = [
        f"session: {store.session_id}",
        f"workspace: {meta.get('workspace')}",
        f"created_at: {meta.get('created_at')}",
        f"last_active_at: {meta.get('last_active_at')}",
        f"mode/provider/model: {meta.get('mode')} / {meta.get('provider', 'fake')} / {meta.get('model')}",
        f"events: {len(events)}",
        f"transcript: {store.transcript_path}",
    ]
    recent = [event for event in events if event.type != "session_created"][-recent_count:]
    if recent:
        lines.append("")
        lines.append("recent history:")
        for event in recent:
            lines.append(f"- {_summarize_event(event)}")
    else:
        lines.append("")
        lines.append("recent history: <empty>")
    return "\n".join(lines)


def _summarize_event(event: SessionEvent) -> str:
    payload = event.payload
    if event.type == "user_message":
        return f"user: {_one_line(payload.get('message', ''))}"
    if event.type == "assistant_message":
        return f"assistant: {_one_line(payload.get('message', ''))}"
    if event.type == "assistant_tool_calls":
        calls = [f"{call.get('name')} {call.get('arguments', {})}" for call in payload.get("tool_calls", [])]
        return f"assistant tool calls: {'; '.join(calls)}"
    if event.type == "tool_requested":
        return f"tool requested: {payload.get('name')} {payload.get('arguments', {})}"
    if event.type == "tool_result":
        result = payload.get("result", {})
        return f"tool result: {payload.get('name')} {payload.get('arguments', {})} ok={result.get('ok')}"
    if event.type == "tool_denied":
        return f"tool denied: {payload.get('name')} {payload.get('reason') or payload.get('error', '')}"
    if event.type == "approval_requested":
        return f"approval requested: {payload.get('name')}"
    if event.type == "approval_decision":
        return f"approval decision: {payload.get('name')} allowed={payload.get('allowed')}"
    if event.type == "cli_command":
        return f"cli: {_one_line(payload.get('command', ''))}"
    return f"{event.type}: {_one_line(str(payload))}"


def _one_line(value: str, limit: int = 120) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


if __name__ == "__main__":
    app()
