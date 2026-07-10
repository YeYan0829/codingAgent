from __future__ import annotations

import json
import sys
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
MESSAGE_ARG = typer.Argument(..., help="本轮要发送给 agent 的用户消息。")
PROVIDER_OPT = typer.Option("fake", "--provider", help="模型 provider：fake 或 deepseek。默认 fake。")
MODEL_OPT = typer.Option(None, "--model", help="provider 内的模型编号；deepseek 默认 deepseek-v4-flash。")
SESSION_ROOT_OPT = typer.Option(None, "--session-root", help="session 数据库根目录；默认使用 CODEAGENT_SESSION_ROOT 或 ~/.codeagent/sessions。")
WORKSPACE_FILTER_OPT = typer.Option(None, "--workspace", "-w", help="只显示/选择某个 workspace 的 session；省略时使用全部 session。")


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
def start(workspace: Path = WORKSPACE_ARG, provider: str = PROVIDER_OPT, model: str | None = MODEL_OPT, session_root: Path | None = SESSION_ROOT_OPT):
    """创建新 session 并进入交互 CLI。"""
    model_config = ModelConfig(provider=provider, model=model)
    ws = Workspace(workspace)
    store = SessionStore(ws.root, session_root=session_root).create(provider=model_config.provider, model=model_config.resolved_model)
    console.print(
        Panel(
            "\n".join(
                [
                    f"title: {store.read_meta().get('title')}",
                    f"session: {store.session_id}",
                    f"workspace: {ws.root}",
                    f"session_root: {store.session_root}",
                    f"provider/model: {model_config.provider} / {model_config.resolved_model}",
                ]
            ),
            title="CodeAgent",
        )
    )
    _interactive_loop(store, ws, model_config)


@app.command()
def ask(
    workspace: Path = WORKSPACE_ARG,
    message: str = MESSAGE_ARG,
    provider: str = PROVIDER_OPT,
    model: str | None = MODEL_OPT,
    session_root: Path | None = SESSION_ROOT_OPT,
):
    """单次非交互运行，用于 smoke test。"""
    model_config = ModelConfig(provider=provider, model=model)
    ws = Workspace(workspace)
    store = SessionStore(ws.root, session_root=session_root).create(
        provider=model_config.provider,
        model=model_config.resolved_model,
        title=SessionStore.title_from_message(message),
    )
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
def list_sessions(
    workspace_arg: Path | None = typer.Argument(None, help="可选 workspace 过滤；省略时列出 session-root 下全部 session。"),
    workspace: Path | None = WORKSPACE_FILTER_OPT,
    session_root: Path | None = SESSION_ROOT_OPT,
):
    """列出 session。默认列出统一 session-root 下的全部 session。"""
    workspace_filter = workspace or workspace_arg
    sessions = _load_session_list(workspace_filter, session_root)
    if not sessions:
        console.print("没有 session。")
        return
    for meta in sessions:
        console.print(_format_session_list_row(meta), markup=False)


@app.command()
def resume(
    session_id: str | None = typer.Argument(None, help="要恢复的 session id；省略时打开全局选择器。"),
    workspace_arg: Path | None = typer.Argument(None, help="可选 workspace；兼容旧用法 `resume <id> .`。"),
    workspace: Path | None = WORKSPACE_FILTER_OPT,
    provider: str | None = typer.Option(None, "--provider", help="覆盖 session meta 中的 provider。"),
    model: str | None = MODEL_OPT,
    session_root: Path | None = SESSION_ROOT_OPT,
):
    """恢复已有 session 并进入交互 CLI。"""
    workspace_filter = workspace or workspace_arg
    if session_id is None:
        selected = _select_session(workspace_filter, session_root)
        if selected is None:
            raise typer.Exit(1)
        session_id, selected_workspace = selected
        workspace_path = selected_workspace
    else:
        workspace_path = _resolve_resume_workspace(session_id, workspace_filter, session_root)
        if workspace_path is None:
            raise typer.Exit(1)

    ws = Workspace(workspace_path)
    try:
        store = SessionStore(ws.root, session_id=session_id, session_root=session_root).load()
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


def _load_session_list(workspace_filter: Path | None, session_root: Path | None) -> list[dict]:
    if workspace_filter is not None:
        ws = Workspace(workspace_filter)
        return SessionStore.list_sessions(ws.root, session_root=session_root, include_legacy=session_root is None)
    return SessionStore.list_all_sessions(session_root=session_root)


def _resolve_resume_workspace(session_id: str, workspace_filter: Path | None, session_root: Path | None) -> Path | None:
    if workspace_filter is not None:
        return Workspace(workspace_filter).root
    meta = SessionStore.find_session(session_id, session_root=session_root)
    if not meta:
        console.print(f"[red]session not found: {session_id}[/red]")
        return None
    workspace = meta.get("workspace")
    if not workspace:
        console.print(f"[red]session missing workspace: {session_id}[/red]")
        return None
    return Path(workspace)


def _format_session_list_row(meta: dict) -> str:
    provider = meta.get("provider", "fake")
    workspace = _safe_text(meta.get("workspace", "<unknown workspace>"))
    return (
        f"{_safe_text(meta.get('title', SessionStore.UNTITLED))}  {meta.get('session_id')}  "
        f"{meta.get('last_active_at')}  {meta.get('mode')}  {provider}/{meta.get('model')}  "
        f"workspace={workspace}"
    )


def _format_tool_step(step: dict) -> str:
    args_text = json.dumps(step.get("arguments", {}), ensure_ascii=False, sort_keys=True)
    result = step.get("result", {})
    return f"[cyan]tool[/cyan] {step.get('tool')} {args_text} -> ok={result.get('ok')} truncated={result.get('truncated')}"


def _format_session_overview(store: SessionStore, recent_count: int = 6) -> str:
    meta = store.read_meta()
    events = store.read_events()
    lines = [
        f"title: {meta.get('title', SessionStore.UNTITLED)}",
        f"session: {store.session_id}",
        f"workspace: {meta.get('workspace')}",
        f"session_root: {meta.get('session_root') or store.session_root}",
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


def _select_session(workspace_filter: Path | None, session_root: Path | None) -> tuple[str, Path] | None:
    sessions = _load_session_list(workspace_filter, session_root)
    if not sessions:
        console.print("没有可恢复的 session。")
        return None
    choices = []
    for meta in sessions:
        session_id = meta.get("session_id")
        workspace = meta.get("workspace")
        if session_id and workspace:
            choices.append((session_id, Path(workspace), _format_session_choice(meta)))
    if sys.stdin.isatty() and _supports_unicode_tui():
        try:
            from prompt_toolkit.shortcuts import radiolist_dialog

            values = [((session_id, workspace), label) for session_id, workspace, label in choices]
            selected = radiolist_dialog(title="Resume Session", text="Select a session", values=values).run()
            return selected
        except Exception:
            pass
    for index, (_, _, label) in enumerate(choices, 1):
        console.print(f"{index}. {label}", markup=False)
    raw = console.input("选择 session 编号: ").strip()
    try:
        selected_index = int(raw)
    except ValueError:
        console.print("[red]无效编号。[/red]")
        return None
    if selected_index < 1 or selected_index > len(choices):
        console.print("[red]编号超出范围。[/red]")
        return None
    session_id, workspace, _ = choices[selected_index - 1]
    return session_id, workspace


def _format_session_choice(meta: dict) -> str:
    return f"{_safe_text(meta.get('title', SessionStore.UNTITLED))}  [{meta.get('session_id')}]  {meta.get('last_active_at')}  {_safe_text(meta.get('workspace'))}"


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
    text = " ".join(_safe_text(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _safe_text(value: object) -> str:
    text = str(value or "")
    text = "".join(char if char.isprintable() and not ("\ue000" <= char <= "\uf8ff") else "?" for char in text)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def _supports_unicode_tui() -> bool:
    encoding = (getattr(sys.stdout, "encoding", None) or "").lower()
    return "utf" in encoding


if __name__ == "__main__":
    app()
