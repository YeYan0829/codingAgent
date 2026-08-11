from __future__ import annotations

import json
import sys
import shutil
import hashlib
import subprocess
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.prompt import Confirm

from codeagent import __version__
from codeagent.config import ModelConfig
from codeagent.interface.commands import handle_manual_call, print_tools
from codeagent.model_gateway.factory import build_model_client
from codeagent.runtime.approval import AutoApprovalGate, ConsoleApprovalGate
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.local_executor import LocalCommandExecutor
from codeagent.runtime.runner import AgentRunner
from codeagent.runtime.candidate import ApplyStatus, CandidateError, CandidateService
from codeagent.session.events import SessionEvent
from codeagent.session.store import SessionStore, SessionStoreError
from codeagent.tools.fs_read import build_fs_tools
from codeagent.tools.git_read import build_git_tools
from codeagent.tools.registry import ToolRegistry
from codeagent.workspace.workspace import Workspace, WorkspaceContext
from codeagent.workspace.git_worktree import GitWorktreeError, GitWorktreeManager, WorkspaceState

app = typer.Typer(help=f"CodeAgent Runtime v{__version__}")
console = Console()

WORKSPACE_ARG = typer.Argument(Path("."), help="要进入的代码工作区目录；默认 '.' 表示当前目录。")
MESSAGE_ARG = typer.Argument(..., help="本轮要发送给 agent 的用户消息。")
PROVIDER_OPT = typer.Option("fake", "--provider", help="模型 provider：fake 或 deepseek。默认 fake。")
MODEL_OPT = typer.Option(None, "--model", help="provider 内的模型编号；deepseek 默认 deepseek-v4-flash。")
SESSION_ROOT_OPT = typer.Option(None, "--session-root", help="session 数据库根目录；默认使用 CODEAGENT_SESSION_ROOT 或 ~/.codeagent/sessions。")
WORKSPACE_FILTER_OPT = typer.Option(None, "--workspace", "-w", help="只显示/选择某个 workspace 的 session；省略时使用全部 session。")


MODE_OPT = typer.Option("readonly", "--mode", help="session 模式：readonly 或 execution。")


def build_registry(workspace: Workspace) -> ToolRegistry:
    return build_registry_for_context(workspace.context)


def build_registry_for_context(context: WorkspaceContext) -> ToolRegistry:
    registry = ToolRegistry(workspace_context=context)
    for tool in build_fs_tools(context) + build_git_tools(context):
        registry.register(tool)
    return registry


def build_runner(store: SessionStore, workspace: Workspace, *, interactive: bool, model_config: ModelConfig, execution_allowed: bool = True) -> AgentRunner:
    context = store.workspace_context()
    execution = store.read_meta().get("mode") == "execution" and execution_allowed
    return AgentRunner(
        session_store=store,
        model=build_model_client(model_config),
        tools=build_registry_for_context(context),
        approval_gate=ConsoleApprovalGate(context) if interactive else AutoApprovalGate(allow=False),
        model_config=model_config,
        workspace_context=context,
        command_executor=LocalCommandExecutor() if execution else None,
        command_artifact_store=CommandArtifactStore(store) if execution else None,
    )


def _create_session(
    workspace: Workspace,
    session_root: Path | None,
    mode: str,
    model_config: ModelConfig,
    title: str | None = None,
) -> SessionStore:
    if mode not in {"readonly", "execution"}:
        raise typer.BadParameter("mode 必须是 readonly 或 execution")
    store = SessionStore(workspace.root, session_root=session_root)
    if mode == "readonly":
        return store.create(mode=mode, provider=model_config.provider, model=model_config.resolved_model, title=title)
    manager = GitWorktreeManager(store.session_root)
    try:
        context = manager.create(workspace.root, store.session_id)
        return store.create(
            mode=mode, provider=model_config.provider, model=model_config.resolved_model,
            title=title, workspace_context=context,
        )
    except Exception as exc:
        cleanup_ok = True
        if "context" in locals():
            try:
                manager.cleanup(context)
            except GitWorktreeError:
                cleanup_ok = False
        if cleanup_ok and store.session_dir.exists():
            shutil.rmtree(store.session_dir)
        elif not cleanup_ok:
            store.session_dir.mkdir(parents=True, exist_ok=True)
            (store.session_dir / "creation-error.json").write_text(
                json.dumps({"error": str(exc), "active_workspace": str(context.active_root)}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        raise


@app.command()
def start(workspace: Path = WORKSPACE_ARG, provider: str = PROVIDER_OPT, model: str | None = MODEL_OPT, session_root: Path | None = SESSION_ROOT_OPT, mode: str = MODE_OPT):
    """创建新 session 并进入交互 CLI。"""
    model_config = ModelConfig(provider=provider, model=model)
    ws = Workspace(workspace)
    store = _create_session(ws, session_root, mode, model_config)
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
    mode: str = MODE_OPT,
):
    """单次非交互运行，用于 smoke test。"""
    model_config = ModelConfig(provider=provider, model=model)
    ws = Workspace(workspace)
    store = _create_session(ws, session_root, mode, model_config, title=SessionStore.title_from_message(message))
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


@app.command("cleanup")
def cleanup_session(
    session_id: str = typer.Argument(..., help="要清理 worktree 的 execution session id。"),
    session_root: Path | None = SESSION_ROOT_OPT,
):
    """仅清理干净且受管的 execution worktree；不会强制删除。"""
    meta = SessionStore.find_session(session_id, session_root=session_root)
    if not meta:
        console.print(f"[red]session not found: {session_id}[/red]")
        raise typer.Exit(1)
    source = meta.get("source_workspace") or meta.get("workspace")
    store = SessionStore(source, session_id=session_id, session_root=session_root).load()
    context = store.workspace_context()
    if context.workspace_kind != "git_worktree":
        console.print("[red]readonly session 没有可清理的 worktree[/red]")
        raise typer.Exit(1)
    manager = GitWorktreeManager(store.session_root)
    report = manager.inspect(context)
    if report.worktree_dirty:
        store.update_workspace_state(WorkspaceState.WORKTREE_DIRTY.value, "dirty worktree 保留，拒绝 cleanup")
        console.print("[red]worktree dirty；已保留现场，拒绝 cleanup[/red]")
        raise typer.Exit(1)
    try:
        manager.cleanup(context)
    except GitWorktreeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    store.update_worktree_lifecycle("discarded")
    store.update_workspace_state(WorkspaceState.DISCARDED.value, "用户显式 cleanup")
    console.print(f"cleaned worktree: {context.active_root}")


@app.command("freeze-candidate")
def freeze_candidate(
    session_id: str = typer.Argument(..., help="要冻结当前 task worktree 的 execution session id。"),
    session_root: Path | None = SESSION_ROOT_OPT,
):
    """把当前已记录编辑与成功 pytest 证据冻结为不可变 Candidate。"""
    store = _load_candidate_store(session_id, session_root)
    try:
        candidate = CandidateService(store.workspace_context(), store).freeze()
    except CandidateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    _print_candidate(candidate, CandidateService(store.workspace_context(), store).load(candidate["candidate_id"])[1])


@app.command("show-candidate")
def show_candidate(
    identifier: str = typer.Argument(..., help="session id 或 candidate id。"),
    session_root: Path | None = SESSION_ROOT_OPT,
):
    """显示 Candidate identity、测试证据和完整固定 diff。"""
    store = _load_candidate_store(identifier, session_root)
    service = CandidateService(store.workspace_context(), store)
    try:
        candidate_id = None if identifier == store.session_id else identifier
        manifest, patch = service.load(candidate_id)
    except CandidateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    _print_candidate(manifest, patch)


@app.command("reject-candidate")
def reject_candidate(
    identifier: str = typer.Argument(..., help="session id 或 candidate id。"),
    session_root: Path | None = SESSION_ROOT_OPT,
):
    """拒绝 Candidate；保留 artifacts 和 dirty task worktree，不修改 source。"""
    store = _load_candidate_store(identifier, session_root)
    service = CandidateService(store.workspace_context(), store)
    try:
        receipt = service.reject(None if identifier == store.session_id else identifier)
    except CandidateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(json.dumps(receipt, ensure_ascii=False, indent=2), markup=False)


@app.command("apply-candidate")
def apply_candidate(
    identifier: str = typer.Argument(..., help="session id 或 candidate id。"),
    session_root: Path | None = SESSION_ROOT_OPT,
):
    """复检 source identity，经明确批准后应用固定 Candidate patch。"""
    store = _load_candidate_store(identifier, session_root)
    service = CandidateService(store.workspace_context(), store)

    def approve(manifest: dict, patch: str) -> bool:
        _print_candidate(manifest, patch.encode("utf-8"))
        if not sys.stdin.isatty():
            return False
        try:
            return Confirm.ask("确认把以上固定 Candidate 应用到 source？", default=False)
        except (EOFError, KeyboardInterrupt):
            return False

    try:
        receipt = service.apply(None if identifier == store.session_id else identifier, approve)
    except CandidateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(json.dumps(receipt, ensure_ascii=False, indent=2), markup=False)
    if receipt["status"] != ApplyStatus.APPLIED.value:
        raise typer.Exit(1)


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
    context = store.workspace_context()
    execution_allowed = True
    if meta.get("mode") == "execution":
        report = GitWorktreeManager(store.session_root).inspect(
            context, candidate_changes=_has_resumable_candidate_changes(store, context)
        )
        store.update_workspace_state(report.state.value, report.reason)
        execution_allowed = report.executable
        if not execution_allowed:
            console.print(f"[yellow]execution workspace state: {report.state.value}: {report.reason}; run_check disabled[/yellow]")
    ws = Workspace(context.active_root)
    model_config = ModelConfig(provider=provider or meta.get("provider", "fake"), model=model or meta.get("model"))
    console.print(Panel(_format_session_overview(store), title="Resumed Session"))
    _interactive_loop(store, ws, model_config, execution_allowed=execution_allowed)


def _interactive_loop(store: SessionStore, ws: Workspace, model_config: ModelConfig, execution_allowed: bool = True) -> None:
    runner = build_runner(store, ws, interactive=True, model_config=model_config, execution_allowed=execution_allowed)
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


def _load_candidate_store(identifier: str, session_root: Path | None) -> SessionStore:
    metas = SessionStore.list_all_sessions(session_root=session_root)
    for meta in metas:
        session_id = meta.get("session_id")
        source = meta.get("source_workspace") or meta.get("workspace")
        if not session_id or not source:
            continue
        store = SessionStore(source, session_id=session_id, session_root=session_root).load()
        if identifier == session_id or (store.session_dir / "artifacts" / "candidates" / identifier / "candidate.json").is_file():
            return store
    console.print(f"[red]session/candidate not found: {identifier}[/red]")
    raise typer.Exit(1)


def _has_resumable_candidate_changes(store: SessionStore, context: WorkspaceContext) -> bool:
    """只把 edit journal 和 command delta 可解释的 dirty 状态视为可恢复候选现场。"""
    edit_hashes: dict[str, str] = {}
    allowed_paths: set[str] = set()
    for event in store.read_events():
        if event.type == "edit_receipt":
            path = str(event.payload.get("path", ""))
            if path:
                allowed_paths.add(path)
                edit_hashes[path] = str(event.payload.get("after_sha256", ""))
        elif event.type == "command_receipt":
            result = event.payload.get("result") or {}
            allowed_paths.update(str(item).replace("\\", "/") for item in result.get("command_introduced_changes", []))
    if not edit_hashes:
        return False
    for relative, expected_hash in edit_hashes.items():
        target = context.active_root / relative
        if not target.is_file() or target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != expected_hash:
            return False
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=context.active_root,
        text=True, capture_output=True, shell=False, check=False, timeout=10,
    )
    if proc.returncode:
        return False
    dirty_paths = {line[3:].replace("\\", "/") for line in proc.stdout.splitlines() if len(line) > 3}
    return bool(dirty_paths) and dirty_paths <= allowed_paths


def _print_candidate(manifest: dict, patch: bytes) -> None:
    summary = {
        "candidate_id": manifest.get("candidate_id"),
        "session_id": manifest.get("session_id"),
        "base_commit": manifest.get("base_commit"),
        "patch_sha256": manifest.get("patch_sha256"),
        "changed_files": manifest.get("changed_files"),
        "workspace_side_effects": manifest.get("workspace_side_effects"),
        "test_receipts": manifest.get("test_receipts"),
    }
    console.print(Panel(json.dumps(summary, ensure_ascii=False, indent=2), title="Frozen Candidate"), markup=False)
    console.print(patch.decode("utf-8"), markup=False)


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
