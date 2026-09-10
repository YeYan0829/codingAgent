from __future__ import annotations

import json
import sys
import shutil
import hashlib
import subprocess
import os
import shlex
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.prompt import Confirm
from prompt_toolkit import PromptSession

from codeagent import __version__
from codeagent.application.session_runtime import build_agent_runner, build_registry_for_context as build_context_registry
from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.interface.commands import handle_manual_call, print_tools
from codeagent.model_gateway.factory import build_model_client
from codeagent.runtime.approval import AutoApprovalGate, ConsoleApprovalGate
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.sandbox_executor import SandboxedCommandExecutor
from codeagent.runtime.runner import AgentRunner
from codeagent.runtime.turn_budget import increase_turn_budget, store_turn_budget
from codeagent.runtime.candidate import ApplyStatus, CandidateError, CandidateService
from codeagent.runtime.current_changes import CurrentChangesError, CurrentChangesService
from codeagent.runtime.validation import current_validation_evidence
from codeagent.session.events import SessionEvent
from codeagent.session.store import SessionStore, SessionStoreError
from codeagent.tools.fs_read import build_fs_tools
from codeagent.tools.git_read import build_git_tools
from codeagent.tools.registry import ToolRegistry
from codeagent.workspace.workspace import SessionWorkspaceState, Workspace, WorkspaceContext
from codeagent.workspace.git_worktree import GitWorktreeError, GitWorktreeManager, WorkspaceState

app = typer.Typer(help=f"CodeAgent Runtime v{__version__}")
console = Console()

WORKSPACE_ARG = typer.Argument(Path("."), help="要进入的代码工作区目录；默认 '.' 表示当前目录。")
MESSAGE_ARG = typer.Argument(..., help="本轮要发送给 agent 的用户消息。")
PROVIDER_OPT = typer.Option("fake", "--provider", help="模型 provider：fake、deepseek 或 glm。默认 fake。")
MODEL_OPT = typer.Option(None, "--model", help="provider 内的模型编号；deepseek 默认 deepseek-v4-flash，glm 默认 glm-5.2。")
REASONING_OPT = typer.Option(False, "--reasoning/--no-reasoning", help="启用或关闭 Provider reasoning。默认关闭。")
REASONING_EFFORT_OPT = typer.Option(
    None, "--reasoning-effort",
    help="GLM-5.2：high/max；GLM-5.3 与 DeepSeek：low/high/max。",
)
SESSION_ROOT_OPT = typer.Option(None, "--session-root", help="session 数据库根目录；默认使用 CODEAGENT_SESSION_ROOT 或 ~/.codeagent/sessions。")
WORKSPACE_FILTER_OPT = typer.Option(None, "--workspace", "-w", help="只显示/选择某个 workspace 的 session；省略时使用全部 session。")


def build_registry(workspace: Workspace) -> ToolRegistry:
    return build_registry_for_context(workspace.context)


def build_registry_for_context(context: WorkspaceContext) -> ToolRegistry:
    return build_context_registry(context)


def build_runner(
    store: SessionStore,
    workspace: Workspace,
    *,
    interactive: bool,
    model_config: ModelConfig,
    execution_allowed: bool = True,
    progress_callback=None,
) -> AgentRunner:
    options = store.read_meta().get("runtime_options") or {}
    return build_agent_runner(
        store,
        model_config=model_config,
        approval_gate=ConsoleApprovalGate(store.workspace_context()) if interactive else AutoApprovalGate(allow=False),
        execution_allowed=execution_allowed,
        progress_callback=progress_callback,
        model_factory=build_model_client,
        runtime_config=RuntimeConfig(
            max_steps_per_turn=int(options.get("max_steps_per_turn", 12)),
            max_model_steps_per_user_turn=int(options.get("max_model_steps_per_user_turn", 48)),
        ),
    )


def _create_session(
    workspace: Workspace,
    session_root: Path | None,
    model_config: ModelConfig,
    title: str | None = None,
) -> SessionStore:
    store = SessionStore(workspace.root, session_root=session_root)
    return store.create(
        provider=model_config.provider, model=model_config.resolved_model, title=title,
        model_options={
            "temperature": model_config.temperature, "max_tokens": model_config.max_tokens,
            "reasoning_enabled": model_config.reasoning_enabled,
            "reasoning_effort": model_config.resolved_reasoning_effort,
        },
    )


@app.command()
def start(
    workspace: Path = WORKSPACE_ARG, provider: str = PROVIDER_OPT, model: str | None = MODEL_OPT,
    reasoning: bool = REASONING_OPT, reasoning_effort: str | None = REASONING_EFFORT_OPT,
    session_root: Path | None = SESSION_ROOT_OPT,
):
    """创建新 session 并进入交互 CLI。"""
    model_config = ModelConfig(provider=provider, model=model, reasoning_enabled=reasoning,
                               reasoning_effort=reasoning_effort)
    ws = Workspace(workspace)
    store = _create_session(ws, session_root, model_config)
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
    reasoning: bool = REASONING_OPT,
    reasoning_effort: str | None = REASONING_EFFORT_OPT,
    session_root: Path | None = SESSION_ROOT_OPT,
):
    """单次非交互运行，用于 smoke test。"""
    model_config = ModelConfig(provider=provider, model=model, reasoning_enabled=reasoning,
                               reasoning_effort=reasoning_effort)
    ws = Workspace(workspace)
    store = _create_session(ws, session_root, model_config, title=SessionStore.title_from_message(message))
    runner = build_runner(store, ws, interactive=False, model_config=model_config)
    output = runner.run_turn(message)
    while output.status == "slice_exhausted":
        continued = runner.continue_turn()
        output.steps.extend(continued.steps)
        output.final_text = continued.final_text
        output.status = continued.status
        output.steps_used_in_turn = continued.steps_used_in_turn
    for step in output.steps:
        console.print(_format_tool_step(step))
        if step["result"].get("content"):
            console.print(step["result"]["content"], markup=False)
        if step["result"].get("error"):
            console.print(f"[red]{step['result']['error']}[/red]")
    console.print(Panel(Text(output.final_text), title="Assistant"))


@app.command("swebench-batch")
def swebench_batch(
    selection: Path = typer.Argument(..., help="只定义固定题集的 selection JSON。"),
    task_repo: Path = typer.Option(..., "--task-repo", help="固定版本 swe-bench-tasks 仓库。"),
    source_cache: Path = typer.Option(..., "--source-cache", help="官方 image prepared source cache。"),
    output_root: Path = typer.Option(..., "--output-root", help="批次 artifact 根目录。"),
    grader_command: str = typer.Option(..., "--grader-command", help="official grader 命令前缀。"),
    report_template: str = typer.Option(..., "--report-template", help="official 单题 report 路径模板。"),
    provider: str = PROVIDER_OPT,
    model: str | None = MODEL_OPT,
    run_id: str | None = typer.Option(None, "--run-id", help="指定已有 run id 即执行兼容性校验并 resume。"),
    temperature: float | None = typer.Option(None, "--temperature"),
    max_tokens: int | None = typer.Option(None, "--max-tokens"),
    reasoning: bool = REASONING_OPT,
    reasoning_effort: str | None = REASONING_EFFORT_OPT,
    max_steps_per_turn: int = typer.Option(12, "--slice-steps"),
    max_model_steps: int = typer.Option(48, "--max-model-steps"),
    price_snapshot: Path | None = typer.Option(None, "--price-snapshot"),
    cost_budget_cny: str | None = typer.Option(
        None, "--cost-budget-cny", help="批次可用成本预算；使用价格快照时必须设置。"
    ),
    minimum_remaining_cost_cny: str = typer.Option(
        "10", "--minimum-remaining-cost-cny",
        help="下一题启动前要求保留的最低 CNY 预算。",
    ),
):
    """串行运行或恢复固定 SWE-bench selection。"""
    from codeagent.benchmark import (
        PriceSnapshot, SWEbenchBatchRunner, SWEbenchCLIGrader, SWEbenchGoldPreflight,
        SWEbenchSelection, SWEbenchSourcePreparer, SWEbenchTaskRepository,
    )
    from codeagent.benchmark.swebench_harness import SWEbenchHarness
    from codeagent.config import RuntimeConfig
    selection_value = SWEbenchSelection(selection)
    repository = SWEbenchTaskRepository(task_repo)
    model_config = ModelConfig(
        provider=provider, model=model, temperature=temperature, max_tokens=max_tokens,
        reasoning_enabled=reasoning, reasoning_effort=reasoning_effort,
    )
    runtime_config = RuntimeConfig(max_steps_per_turn=max_steps_per_turn,
                                   max_model_steps_per_user_turn=max_model_steps)
    endpoint = (
        os.environ.get("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4") if provider == "glm"
        else "https://api.deepseek.com" if provider == "deepseek" else "local-fake"
    )
    dataset_path = output_root.resolve() / ".datasets" / f"{selection_value.selection_id}.json"
    repository.export_dataset(selection_value.instance_ids, dataset_path)
    grader = SWEbenchCLIGrader(
        [*shlex.split(grader_command), "--dataset_name", str(dataset_path), "--split", "test"],
        report_template,
    )
    runner = SWEbenchBatchRunner(
        selection=selection_value, task_loader=lambda item: repository.load(item).task,
        harness=SWEbenchHarness(SWEbenchSourcePreparer(source_cache), grader),
        model_factory=lambda: build_model_client(model_config), model_config=model_config,
        runtime_config=runtime_config, output_root=output_root, endpoint_identity=endpoint,
        price_snapshot=PriceSnapshot.from_json(price_snapshot) if price_snapshot else None,
        cost_budget_cny=cost_budget_cny,
        minimum_remaining_cost_cny=minimum_remaining_cost_cny,
        runtime_root=Path(__file__).resolve().parents[1],
    )
    try:
        result = runner.run(run_id)
    except Exception as exc:
        console.print(f"[red]SWE-bench batch failed: {exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(json.dumps(result, ensure_ascii=False, indent=2), markup=False)


@app.command("swebench-select")
def swebench_select(
    output: Path = typer.Option(..., "--output", help="最终 Agent-facing selection JSON。"),
    exclude: Path = typer.Option(..., "--exclude", help="需要排除的开发集 selection。"),
    task_repo: Path = typer.Option(..., "--task-repo", help="固定版本 swe-bench-tasks 仓库。"),
    source_cache: Path = typer.Option(..., "--source-cache", help="prepared source cache。"),
    grader_command: str = typer.Option(..., "--grader-command", help="official gold grader 命令前缀。"),
    size: int = typer.Option(50, "--size", min=1),
    seed: int = typer.Option(42, "--seed"),
    selection_id: str = typer.Option("verified-eval-50-v1", "--selection-id"),
    report: Path | None = typer.Option(None, "--report", help="generation report；默认位于 output 同目录。"),
    state_root: Path | None = typer.Option(None, "--state-root", help="可恢复 preflight 状态目录。"),
):
    """从 SWE-bench Verified 生成仅含 gold-preflight PASS 的固定 selection。"""
    from codeagent.benchmark import (
        OfficialCandidatePreflight, SWEbenchGoldPreflight, SWEbenchSourcePreparer,
        SWEbenchTaskRepository, VerifiedSelectionGenerator, candidates_from_repository,
        runtime_identity,
    )
    excluded_value = json.loads(exclude.read_text(encoding="utf-8"))
    excluded_ids = {str(item["instance_id"] if isinstance(item, dict) else item)
                    for item in excluded_value.get("tasks", [])}
    repository = SWEbenchTaskRepository(task_repo)
    report_path = report or output.with_name(output.stem + "-generation.json")
    state_path = state_root or output.parent / "selection-runs" / selection_id
    task_commit = _git_commit(task_repo)
    swebench_root = Path("reference/SWE-bench").resolve()
    swebench_commit = _git_commit(swebench_root) if swebench_root.is_dir() else None
    command = shlex.split(grader_command)
    identity = {
        "selection_id": selection_id,
        "excluded_selection": {"path": str(exclude.resolve()), "sha256": hashlib.sha256(exclude.read_bytes()).hexdigest()},
        "dataset_identity": {"suite": "SWE-bench_Verified", "task_repository": str(task_repo.resolve()),
                             "task_commit": task_commit, "swebench_commit": swebench_commit},
        "runtime": runtime_identity(Path(__file__).resolve().parents[1]),
        "preflight": {"grader_command": command, "source_cache": str(source_cache.resolve())},
    }
    generator = VerifiedSelectionGenerator(
        candidates=candidates_from_repository(repository), excluded_ids=excluded_ids,
        size=size, seed=seed,
        preflight=OfficialCandidatePreflight(
            SWEbenchSourcePreparer(source_cache), SWEbenchGoldPreflight(repository, command), state_path / "artifacts"
        ),
        output_path=output, report_path=report_path, state_root=state_path, identity=identity,
    )
    try:
        result = generator.run()
    except Exception as exc:
        console.print(f"[red]SWE-bench selection generation failed: {exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(json.dumps(result, ensure_ascii=False, indent=2), markup=False)


def _git_commit(root: Path) -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                              text=True, check=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


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
    session_id: str = typer.Argument(..., help="要清理诊断数据的 Session id。"),
    session_root: Path | None = SESSION_ROOT_OPT,
):
    """清理不影响业务状态的诊断数据；当前修改请使用 discard-changes。"""
    meta = SessionStore.find_session(session_id, session_root=session_root)
    if not meta:
        console.print(f"[red]session not found: {session_id}[/red]")
        raise typer.Exit(1)
    source = meta.get("source_workspace") or meta.get("workspace")
    store = SessionStore(source, session_id=session_id, session_root=session_root).load()
    if store.workspace_state() in {SessionWorkspaceState.WORKSPACE_TAINTED, SessionWorkspaceState.RECOVERY_REQUIRED}:
        console.print("[red]workspace tainted/recovery 诊断材料不能清理；请先 discard 或人工检查[/red]")
        raise typer.Exit(1)
    shutil.rmtree(store.diagnostics_dir, ignore_errors=True)
    console.print("diagnostics cleaned")


@app.command("show-changes")
def show_changes(
    session_id: str = typer.Argument(..., help="Session id。"),
    session_root: Path | None = SESSION_ROOT_OPT,
):
    """按需显示 Agent worktree 相对 base 的当前 diff，不创建长期 patch。"""
    store = _load_changes_store(session_id, session_root, allow_tainted=True)
    try:
        patch = CurrentChangesService(store).preview()
    except CandidateError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    console.print(patch.decode("utf-8", errors="replace"), markup=False)


@app.command("accept-changes")
def accept_changes(
    session_id: str = typer.Argument(..., help="Session id。"),
    session_root: Path | None = SESSION_ROOT_OPT,
):
    """冻结、复检并采纳当前修改。"""
    store = _load_changes_store(session_id, session_root)
    service = CurrentChangesService(store)

    def approve(manifest: dict, patch: str) -> bool:
        _print_changes(manifest, patch.encode("utf-8"))
        if not sys.stdin.isatty():
            return False
        try:
            return Confirm.ask("确认把以上当前修改应用到 source？", default=False)
        except (EOFError, KeyboardInterrupt):
            return False

    try:
        receipt = service.accept(approve)
    except CurrentChangesError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(json.dumps(receipt, ensure_ascii=False, indent=2), markup=False)
    if receipt["status"] != ApplyStatus.APPLIED.value:
        raise typer.Exit(1)


@app.command("discard-changes")
def discard_changes(
    session_id: str = typer.Argument(..., help="Session id。"),
    session_root: Path | None = SESSION_ROOT_OPT,
):
    """丢弃当前修改，不改变 source，Session 可以继续。"""
    store = _load_changes_store(session_id, session_root, allow_tainted=True)
    try:
        receipt = CurrentChangesService(store).discard()
    except CurrentChangesError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(json.dumps(receipt, ensure_ascii=False, indent=2), markup=False)


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
    if store.workspace_state() in {
        SessionWorkspaceState.PREPARING_WORKSPACE,
        SessionWorkspaceState.ACCEPTING,
        SessionWorkspaceState.DISCARDING,
    }:
        store.mark_recovery_required("检测到未完成的 workspace lifecycle transition")
        execution_allowed = False
    if store.workspace_state() in {
        SessionWorkspaceState.CHANGES_ACTIVE,
        SessionWorkspaceState.WORKSPACE_TAINTED,
        SessionWorkspaceState.RECOVERY_REQUIRED,
    }:
        recovery_required = _recover_atomic_transactions(store, context)
        report = GitWorktreeManager(store.session_root).inspect(
            context,
            candidate_changes=False if recovery_required else _has_resumable_candidate_changes(store, context),
            recovery_required=recovery_required,
            allow_source_changes=True,
        )
        execution_allowed = report.executable
        if recovery_required or store.workspace_state() == SessionWorkspaceState.RECOVERY_REQUIRED:
            store.mark_recovery_required(report.reason)
            execution_allowed = False
        elif store.workspace_state() == SessionWorkspaceState.WORKSPACE_TAINTED:
            execution_allowed = False
        elif execution_allowed:
            store.restore_changes_active(report.reason)
        else:
            store.mark_recovery_required(report.reason)
        if store.workspace_state() == SessionWorkspaceState.WORKSPACE_TAINTED:
            console.print("[yellow]Agent workspace state: workspace_tainted；仅允许只读检查和 discard-changes[/yellow]")
        elif not execution_allowed:
            console.print(f"[yellow]Agent workspace state: {report.state.value}: {report.reason}; 受保护能力已关闭[/yellow]")
    ws = Workspace(context.active_root)
    model_options = meta.get("model_options") if isinstance(meta.get("model_options"), dict) else {}
    model_config = ModelConfig(
        provider=provider or meta.get("provider", "fake"), model=model or meta.get("model"),
        temperature=model_options.get("temperature"), max_tokens=model_options.get("max_tokens"),
        reasoning_enabled=model_options.get("reasoning_enabled") is True,
        reasoning_effort=model_options.get("reasoning_effort"),
    )
    console.print(Panel(_format_session_overview(store), title="Resumed Session"))
    _interactive_loop(store, ws, model_config, execution_allowed=execution_allowed)


def _interactive_loop(store: SessionStore, ws: Workspace, model_config: ModelConfig, execution_allowed: bool = True) -> None:
    def show_progress(event: dict) -> None:
        if event["type"] == "assistant_progress":
            console.print(Text("Agent ", style="bold blue"), Text(str(event["message"])))
            return
        step = event["step"]
        console.print(_format_tool_step(step))
        result = step.get("result", {})
        if result.get("error"):
            console.print(f"[red]{result['error']}[/red]")

    runner = build_runner(
        store,
        ws,
        interactive=True,
        model_config=model_config,
        execution_allowed=execution_allowed,
        progress_callback=show_progress,
    )
    input_session = _build_input_session() if sys.stdin.isatty() else None
    while True:
        try:
            raw = (input_session.prompt() if input_session is not None else console.input("[bold]> [/bold]")).strip()
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
            print_tools(console, runner.tools)
            continue
        if raw == "/log":
            console.print(str(store.events_path))
            continue
        if raw == "/stop":
            if not store_turn_budget(store).closed:
                store.append_event("turn_terminated", {"reason": "user_stop", "message": "用户已结束本轮执行。"})
            console.print("本轮已停止；可输入新的具体指令。")
            continue
        if raw == "/budget" or raw.startswith("/budget "):
            try:
                budget = store_turn_budget(store)
                requested = raw.split(maxsplit=1)[1]
                increase_turn_budget(
                    store,
                    turn_id=budget.turn_id or "",
                    expected_limit=budget.limit,
                    additional_steps=int(requested[1:]) if requested.startswith("+") else None,
                    new_limit=None if requested.startswith("+") else int(requested),
                )
                output = runner.continue_turn()
            except (ValueError, IndexError, RuntimeError) as exc:
                console.print(Text(f"无法扩额：{exc}。用法：/budget +<追加步数> 或 /budget <新的本轮总上限>"))
                continue
            _finish_or_offer_continuation(runner, output)
            continue
        if raw == "/continue":
            try:
                output = runner.continue_turn()
            except RuntimeError as exc:
                console.print(f"[yellow]{exc}[/yellow]")
                continue
            _finish_or_offer_continuation(runner, output)
            continue
        if raw.startswith("/call"):
            handle_manual_call(console, runner, raw)
            continue
        if not store_turn_budget(store).closed:
            console.print("本轮尚未结束；使用 /budget +<追加步数> 或 /budget <总上限> 扩额，或 /stop 后输入修正指令。")
            continue
        output = runner.run_turn(raw)
        _finish_or_offer_continuation(runner, output)


def _finish_or_offer_continuation(runner: AgentRunner, output) -> None:
    while output.status == "slice_exhausted":
        output = runner.continue_turn()
    if output.status == "model_step_budget_exhausted":
        console.print(Panel(Text(output.final_text + "\n使用 /budget +<追加步数> 或 /budget <新的总上限> 扩额并继续；/stop 结束本轮。"),
                            title="Step Budget Reached"))
        return
    if output.status == "context_budget_exceeded":
        console.print(Panel(
            output.final_text + "\n\n"
            "这不是 Session 总额度耗尽，也不是任务完成。本轮已明确终止；"
            "Session 和 workspace 仍可检查，并可发送新的具体指令开始下一轮。",
            title="Context Capacity Reached",
        ))
        return
    title = "Assistant" if output.status == "completed" else "Execution Stopped"
    console.print(Panel(Text(output.final_text), title=title))


def _build_input_session(**kwargs) -> PromptSession[str]:
    """创建支持 bracketed paste 的单消息编辑缓冲区。"""
    return PromptSession(message="> ", multiline=False, **kwargs)


def _load_session_list(workspace_filter: Path | None, session_root: Path | None) -> list[dict]:
    if workspace_filter is not None:
        ws = Workspace(workspace_filter)
        return SessionStore.list_sessions(ws.root, session_root=session_root, include_legacy=session_root is None)
    return SessionStore.list_all_sessions(session_root=session_root)


def _load_changes_store(session_id: str, session_root: Path | None, *, allow_tainted: bool = False) -> SessionStore:
    meta = SessionStore.find_session(session_id, session_root=session_root)
    if not meta:
        console.print(f"[red]session not found: {session_id}[/red]")
        raise typer.Exit(1)
    store = SessionStore(meta["workspace"], session_id=session_id, session_root=session_root).load()
    allowed = {SessionWorkspaceState.CHANGES_ACTIVE}
    if allow_tainted:
        allowed.add(SessionWorkspaceState.WORKSPACE_TAINTED)
    if store.workspace_state() not in allowed:
        if store.workspace_state() == SessionWorkspaceState.WORKSPACE_TAINTED:
            console.print("[red]workspace_tainted 禁止采纳；只允许只读检查和 discard-changes[/red]")
        else:
            console.print("[red]当前 Session 没有可处理的修改[/red]")
        raise typer.Exit(1)
    return store


def _has_resumable_candidate_changes(store: SessionStore, context: WorkspaceContext) -> bool:
    """Candidate revision 领先 accepted baseline 且 worktree dirty 即可恢复。

    每条命令/编辑的细粒度事件用于诊断，不再承担重建当前修改的职责；异常状态由
    recovery_required/workspace_tainted 的更高优先级 gate 处理。
    """
    meta = store.read_meta()
    if int(meta.get("candidate_revision", 0)) <= int(meta.get("accepted_candidate_revision", 0)):
        return False
    proc = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=context.active_root,
        capture_output=True, shell=False, check=False, timeout=10,
    )
    if proc.returncode:
        return False
    return bool(proc.stdout)


def _recover_atomic_transactions(store: SessionStore, context: WorkspaceContext) -> bool:
    """先恢复未完成事务；任何无法确认的状态都优先关闭 execution capability。"""
    from codeagent.runtime.atomic_edit import AtomicEditError, AtomicEditService

    try:
        AtomicEditService(context, store).ensure_recovered()
    except AtomicEditError:
        return True
    return False


def _print_changes(manifest: dict, patch: bytes) -> None:
    summary = {
        "session_id": manifest.get("session_id"),
        "base_commit": manifest.get("base_commit"),
        "patch_sha256": manifest.get("patch_sha256"),
        "changed_files": manifest.get("changed_files"),
        "workspace_side_effects": manifest.get("workspace_side_effects"),
        "test_receipts": manifest.get("test_receipts"),
    }
    console.print(Panel(json.dumps(summary, ensure_ascii=False, indent=2), title="当前修改"), markup=False)
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
        f"{meta.get('last_active_at')}  {meta.get('workspace_state')}  {provider}/{meta.get('model')}  "
        f"workspace={workspace}"
    )


def _format_tool_step(step: dict) -> Text:
    args_text = json.dumps(step.get("arguments", {}), ensure_ascii=False, sort_keys=True)
    result = step.get("result", {})
    rendered = Text("tool", style="cyan")
    rendered.append(
        f" {step.get('tool')} {args_text} -> ok={result.get('ok')} truncated={result.get('truncated')}"
    )
    return rendered


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
        f"state/provider/model: {meta.get('workspace_state')} / {meta.get('provider', 'fake')} / {meta.get('model')}",
        f"events: {len(events)}",
        f"events: {store.events_path}",
    ]
    context = store.workspace_context()
    if context.workspace_kind == "git_worktree":
        changed = _current_changed_paths(context)
        lines.append(f"current changes: {len(changed)} file(s)" + (f" ({', '.join(changed[:8])})" if changed else ""))
        evidence = current_validation_evidence(store, context)
        lines.append(f"current validation: {'passed evidence exists' if evidence else 'none for current file tree'}")
    else:
        lines.extend(["current changes: none (source-only)", "current validation: none"])
    recent = [event for event in events if event.type != "session_created"][-recent_count:]
    latest_turn_id = next((event.turn_id for event in reversed(events) if event.type == "user_message"), None)
    if latest_turn_id:
        turn_events = [event for event in events if event.turn_id == latest_turn_id]
        paused = next((event for event in reversed(turn_events) if event.type == "execution_slice_exhausted"), None)
        finished = any(event.type in {"assistant_message", "turn_terminated"} for event in turn_events)
        if paused is not None and not finished:
            lines.extend([
                "",
                "active turn: execution paused; original request remains active",
                f"model steps: {paused.payload.get('steps_used_in_turn', '?')}/{paused.payload.get('max_model_steps_per_user_turn', '?')}",
                "next action: type /continue to continue without creating a new user message",
            ])
    if recent:
        lines.append("")
        lines.append("recent history:")
        for event in recent:
            lines.append(f"- {_summarize_event(event)}")
    else:
        lines.append("")
        lines.append("recent history: <empty>")
    return "\n".join(lines)


def _current_changed_paths(context: WorkspaceContext) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(context.active_root), "status", "--porcelain=v1", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line[3:] for line in result.stdout.splitlines() if len(line) > 3]


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
        calls = [f"{call.get('name')} {_summarize_tool_arguments(call.get('name', ''), call.get('arguments', {}))}" for call in payload.get("tool_calls", [])]
        return f"assistant tool calls: {'; '.join(calls)}"
    if event.type == "model_output_truncated":
        return ("model output truncated: "
                f"finish_reason={payload.get('finish_reason')} "
                f"consecutive={payload.get('consecutive_count')}")
    if event.type == "tool_requested":
        return f"tool requested: {payload.get('name')} {_summarize_tool_arguments(payload.get('name', ''), payload.get('arguments', {}))}"
    if event.type == "tool_result":
        result = payload.get("result", {})
        return f"tool result: {payload.get('name')} {_summarize_tool_arguments(payload.get('name', ''), payload.get('arguments', {}))} ok={result.get('ok')}"
    if event.type == "tool_denied":
        return f"tool denied: {payload.get('name')} {payload.get('reason') or payload.get('error', '')}"
    if event.type == "approval_requested":
        return f"approval requested: {payload.get('name')}"
    if event.type == "approval_decision":
        return f"approval decision: {payload.get('name')} allowed={payload.get('allowed')}"
    if event.type == "cli_command":
        return f"cli: {_one_line(payload.get('command', ''))}"
    if event.type == "execution_slice_exhausted":
        return ("execution paused: "
                f"steps={payload.get('steps_used_in_turn', '?')}/"
                f"{payload.get('max_model_steps_per_user_turn', '?')}")
    if event.type == "turn_terminated":
        return f"execution stopped: {payload.get('reason', 'unknown')} {_one_line(payload.get('message', ''))}"
    return f"{event.type}: {_one_line(str(payload))}"


def _summarize_tool_arguments(name: str, arguments: dict) -> str:
    if name == "apply_workspace_edit":
        operations = arguments.get("operations", [])
        targets = []
        for item in operations[:10]:
            target = item.get("path") or f"{item.get('source')} -> {item.get('destination')}"
            targets.append(f"{item.get('op')}:{target}")
        return f"operations={len(operations)} [{', '.join(targets)}]"
    if name == "run_command":
        return f"purpose={arguments.get('purpose', 'utility')} command={_one_line(arguments.get('command', ''), 100)}"
    if "path" in arguments:
        return f"path={arguments['path']}"
    if "query" in arguments:
        return f"query={_one_line(arguments['query'], 80)}"
    return _one_line(str(arguments), 120)


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
