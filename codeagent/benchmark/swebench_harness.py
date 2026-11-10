from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence

from codeagent.benchmark.limits import ExperimentLimits, ExperimentController, ExperimentLimitReached, LimitedFactory
from codeagent.benchmark.c4 import disable_delegation, ledger_summary
from codeagent.benchmark.swebench_source import SWEbenchSourcePreparer, SWEbenchTaskSource
from codeagent.benchmark.swebench_network import EVALUATION_RULES, SWEbenchNetworkConfig, authorize_network
from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.model_gateway.base import BaseModelClient
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.runner import AgentRunner
from codeagent.session.store import SessionStore
from codeagent.tools.registry import ToolRegistry


@dataclass(frozen=True)
class SWEbenchTask:
    instance_id: str
    image: str
    base_commit: str
    problem_statement: str

    @classmethod
    def from_json(cls, path: str | Path) -> "SWEbenchTask":
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(value["instance_id"], value["image"], value["base_commit"], value["problem_statement"])


@dataclass(frozen=True)
class OracleResult:
    status: str
    resolved: bool | None
    detail: str = ""
    report_path: str | None = None


class SWEbenchGrader(Protocol):
    def grade(self, task: SWEbenchTask, prediction_path: Path, run_id: str) -> OracleResult: ...


class SWEbenchCLIGrader:
    """调用固定版本官方 harness；明确注入命令和 report 路径，不猜测版本布局。"""

    def __init__(
        self, command_prefix: Sequence[str], report_template: str,
        *, command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: int = 3600,
    ) -> None:
        self.command_prefix = tuple(command_prefix)
        self.report_template = report_template
        self.command_runner = command_runner
        self.timeout_seconds = timeout_seconds

    def grade(self, task: SWEbenchTask, prediction_path: Path, run_id: str) -> OracleResult:
        report_dir = prediction_path.parent / "grader-report"
        argv = [*self.command_prefix, "--predictions_path", str(prediction_path),
                "--instance_ids", task.instance_id, "--run_id", run_id,
                "--report_dir", str(report_dir)]
        try:
            proc = self.command_runner(argv, capture_output=True, text=True, shell=False,
                                       check=False, timeout=self.timeout_seconds)
        except (OSError, subprocess.SubprocessError) as exc:
            return OracleResult("environment_failed", None, str(exc))
        if proc.returncode:
            return OracleResult("grader_failed", None, (proc.stderr or proc.stdout)[-4000:])
        summaries = list(report_dir.glob(f"*.{run_id}.json"))
        if len(summaries) != 1:
            return OracleResult(
                "report_invalid", None,
                f"official batch report count must be 1, got {len(summaries)}",
            )
        summary = summaries[0]
        try:
            value = json.loads(summary.read_text(encoding="utf-8"))
            status, resolved, detail = _outcome_from_batch_report(value, task.instance_id)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return OracleResult("report_invalid", None, str(exc), str(summary))
        instance_report = Path(
            self.report_template.format(run_id=run_id, instance_id=task.instance_id)
        ).resolve()
        evidence = instance_report if instance_report.is_file() else summary
        return OracleResult(status, resolved, detail, str(evidence))


@dataclass(frozen=True)
class SWEbenchRunResult:
    run_id: str
    instance_id: str
    session_id: str
    agent_status: str
    agent_final: str
    patch_present: bool
    patch_sha256: str | None
    prediction_path: str
    oracle_status: str
    oracle_passed: bool | None
    oracle_detail: str
    token_usage: dict[str, object]
    source_identity: dict[str, object] | None = None
    oracle_report_path: str | None = None


class SWEbenchHarness:
    """正式AgentRunner的项目工具全部操作单题容器，最终补丁交给独立grader。"""

    def __init__(self, source_preparer: SWEbenchSourcePreparer, grader: SWEbenchGrader,
                 *, model_factory=None) -> None:
        self.source_preparer = source_preparer
        self.grader = grader
        self.model_factory = model_factory

    def run(
        self, task: SWEbenchTask, model: BaseModelClient, model_config: ModelConfig,
        output_root: str | Path, *, runtime_config: RuntimeConfig | None = None,
        progress_callback: Callable[[str], None] | None = None,
        experiment_limits: ExperimentLimits | None = None,
        expected_source_identity: dict | None = None,
    ) -> SWEbenchRunResult:
        runtime_config = runtime_config or RuntimeConfig()
        if experiment_limits and runtime_config.delegation_enabled and self.model_factory is None:
            raise ValueError('带委派的限额实验需要可spawn的model_factory，以覆盖主/子/摘要请求')
        run_id = uuid.uuid4().hex[:16]
        root = Path(output_root).expanduser().resolve() / run_id
        root.mkdir(parents=True, exist_ok=False)
        if progress_callback:
            progress_callback("agent")
        workspace = self.source_preparer.open_workspace(SWEbenchTaskSource(task.instance_id, task.image, task.base_commit), root)
        try:
            if expected_source_identity is not None and any(
                    workspace.identity.get(key) != value for key, value in expected_source_identity.items()):
                raise ValueError('实际SWE容器镜像/源码起点与本题预检不一致，未调用模型')
            return self._run_workspace(task, model, model_config, root, run_id, workspace,
                                       runtime_config, progress_callback, experiment_limits)
        finally:
            workspace.close()

    def _run_workspace(self, task, model, model_config, root, run_id, workspace,
                       runtime_config, progress_callback, experiment_limits):
        context = workspace.context
        store = SessionStore(context.active_root, session_root=root / "sessions").create(
            provider=model_config.provider, model=model_config.resolved_model,
            title=f"SWE-bench {task.instance_id}",
            runtime_options=asdict(runtime_config),
            model_options={
                "temperature": model_config.temperature, "max_tokens": model_config.resolved_max_tokens,
                **({"summary_max_tokens": model_config.summary_max_tokens} if model_config.summary_max_tokens is not None else {}),
                "reasoning_enabled": model_config.reasoning_enabled,
                "reasoning_effort": model_config.resolved_reasoning_effort,
            },
        )
        command_executor = None
        try:
            store.append_event("swebench_container_workspace_prepared", workspace.identity)
            network = getattr(workspace, 'network_config', SWEbenchNetworkConfig())
            authorize_network(store, network)
            store.append_event('swebench_network_policy', network.identity())
            registry = ToolRegistry(workspace_context=context, workspace_backend=workspace)
            for tool in workspace.build_read_tools(context):
                registry.register(tool)
            controller = ExperimentController(root / "experiment-limits.json", experiment_limits) if experiment_limits else None
            limited_model = controller.wrap(model, model_config, runtime_config) if controller else model
            factory = LimitedFactory(self.model_factory, controller, runtime_config) if controller and self.model_factory else self.model_factory
            command_executor = workspace.command_executor
            runner = AgentRunner(
                store, limited_model, registry, AutoApprovalGate(False),
                config=runtime_config, model_config=model_config,
                model_factory=factory,
                cancellation_callback=controller.cancelled if controller else None,
                workspace_context=context,
                command_executor=command_executor,
                command_artifact_store=CommandArtifactStore(store),
            )
            command_executor.cancelled = runner.cancellation_callback
            if not runtime_config.delegation_enabled:
                disable_delegation(runner)
            try:
                output = runner.run_turn(task.problem_statement + '\n\n' + EVALUATION_RULES)
            except ExperimentLimitReached as exc:
                from codeagent.runtime.runner import RunnerOutput
                output = RunnerOutput(str(exc), steps=getattr(runner, "_turn_steps", []), status=str(exc))
            finally:
                # 每次命令已回收进程；持锁导出补丁后立即删除容器，再进入独立评分。
                patch = workspace.prediction_patch()
                workspace.close()
            if controller and controller.reason:
                output.status = controller.reason
                output.final_text = controller.reason
                store.append_event("experiment_stopped", {"reason": controller.reason})
                store.append_event("turn_terminated", {"reason": controller.reason, "external_control": True})
            patch_hash = hashlib.sha256(patch).hexdigest() if patch else None
            prediction_path = root / "prediction.json"
            # 官方 harness 对 .json 和 .jsonl 采用不同协议；.json 即使只有一题，
            # 顶层也必须是 prediction 列表。
            prediction_path.write_text(json.dumps([{
                "instance_id": task.instance_id,
                "model_name_or_path": f"codeagent/{model_config.resolved_model}",
                "model_patch": patch.decode("utf-8"),
            }], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if progress_callback:
                progress_callback("grader")
            oracle = self.grader.grade(task, prediction_path, run_id)
            result = SWEbenchRunResult(
                run_id=run_id, instance_id=task.instance_id, session_id=store.session_id,
                agent_status=output.status, agent_final=output.final_text,
                patch_present=bool(patch), patch_sha256=patch_hash,
                prediction_path=str(prediction_path), oracle_status=oracle.status,
                oracle_passed=oracle.resolved, oracle_detail=oracle.detail,
                token_usage=_aggregate_token_usage(store),
                source_identity=dict(workspace.identity),
                oracle_report_path=oracle.report_path,
            )
            (root / "result.json").write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return result
        finally:
            if command_executor is not None:
                command_executor.close()
            # 只回收容器；实验目录、命令日志和费用原样保留。


def _outcome_from_batch_report(value: object, instance_id: str) -> tuple[str, bool | None, str]:
    if not isinstance(value, dict):
        raise ValueError("official batch report 顶层不是 object")
    groups = {
        key: set(items) for key, items in value.items()
        if key.endswith("_ids") and isinstance(items, list)
        and all(isinstance(item, str) for item in items)
    }
    if instance_id in groups.get("resolved_ids", set()):
        return "completed", True, "official grader resolved"
    if instance_id in groups.get("unresolved_ids", set()):
        return "completed", False, "official grader unresolved"
    if instance_id in groups.get("empty_patch_ids", set()):
        return "completed", False, "official grader reported empty patch"
    infrastructure = set().union(
        groups.get("infra_failure_ids", set()),
        groups.get("ambiguous_failure_ids", set()),
        groups.get("error_ids", set()),
        groups.get("incomplete_ids", set()),
    )
    if instance_id in infrastructure:
        return "environment_failed", None, "official grader reported infrastructure/error state"
    raise ValueError("instance missing from recognizable official batch report groups")


def _aggregate_token_usage(store: SessionStore) -> dict[str, object]:
    return ledger_summary(store.read_events())["usage"]
