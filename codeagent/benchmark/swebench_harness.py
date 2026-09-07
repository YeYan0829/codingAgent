from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence

from codeagent.benchmark.swebench_executor import SWEbenchDockerCommandExecutor
from codeagent.benchmark.accounting import aggregate_usage
from codeagent.benchmark.swebench_source import SWEbenchSourcePreparer, SWEbenchTaskSource
from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.model_gateway.base import BaseModelClient
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.candidate import CandidateService
from codeagent.runtime.runner import AgentRunner
from codeagent.runtime.validation import current_validation_evidence
from codeagent.session.store import SessionStore
from codeagent.tools.fs_read import build_fs_tools
from codeagent.tools.git_read import build_git_tools
from codeagent.tools.registry import ToolRegistry
from codeagent.workspace.git_worktree import GitWorktreeManager


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
        argv = [*self.command_prefix, "--predictions_path", str(prediction_path),
                "--instance_ids", task.instance_id, "--run_id", run_id]
        try:
            proc = self.command_runner(argv, capture_output=True, text=True, shell=False,
                                       check=False, timeout=self.timeout_seconds)
        except (OSError, subprocess.SubprocessError) as exc:
            return OracleResult("environment_failed", None, str(exc))
        if proc.returncode:
            return OracleResult("grader_failed", None, (proc.stderr or proc.stdout)[-4000:])
        report = Path(self.report_template.format(run_id=run_id, instance_id=task.instance_id)).resolve()
        try:
            value = json.loads(report.read_text(encoding="utf-8"))
            resolved = _resolved_from_report(value, task.instance_id)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return OracleResult("report_invalid", None, str(exc), str(report))
        return OracleResult("completed", resolved, "official grader completed", str(report))


@dataclass(frozen=True)
class SWEbenchRunResult:
    run_id: str
    instance_id: str
    session_id: str
    agent_status: str
    agent_final: str
    validation_passed: bool
    patch_present: bool
    patch_sha256: str | None
    prediction_path: str
    oracle_status: str
    oracle_passed: bool | None
    oracle_detail: str
    token_usage: dict[str, object]
    source_identity: dict[str, str] | None = None


class SWEbenchHarness:
    """单任务 benchmark 编排；不改变正式 AgentRunner 和 Candidate 语义。"""

    def __init__(self, source_preparer: SWEbenchSourcePreparer, grader: SWEbenchGrader) -> None:
        self.source_preparer = source_preparer
        self.grader = grader

    def run(
        self, task: SWEbenchTask, model: BaseModelClient, model_config: ModelConfig,
        output_root: str | Path, *, runtime_config: RuntimeConfig | None = None,
    ) -> SWEbenchRunResult:
        run_id = uuid.uuid4().hex[:16]
        root = Path(output_root).expanduser().resolve() / run_id
        root.mkdir(parents=True, exist_ok=False)
        prepared = self.source_preparer.prepare(SWEbenchTaskSource(task.instance_id, task.image, task.base_commit))
        store = SessionStore(prepared.source_root, session_root=root / "sessions").create(
            provider=model_config.provider, model=model_config.resolved_model,
            title=f"SWE-bench {task.instance_id}",
        )
        manager = GitWorktreeManager(store.session_root)
        context = None
        try:
            store.begin_workspace_upgrade(1)
            context = manager.create(prepared.source_root, store.session_id, 1)
            store.activate_workspace(context)
            registry = ToolRegistry(workspace_context=context)
            for tool in build_fs_tools(context) + build_git_tools(context):
                registry.register(tool)
            runner = AgentRunner(
                store, model, registry, AutoApprovalGate(False),
                config=runtime_config or RuntimeConfig(), model_config=model_config,
                workspace_context=context,
                command_executor=SWEbenchDockerCommandExecutor(prepared),
                command_artifact_store=CommandArtifactStore(store),
            )
            output = runner.run_turn(task.problem_statement)
            while output.status == "slice_exhausted":
                output = runner.continue_turn()
            if output.status == "model_step_budget_exhausted":
                # 固定预算评测不等待人工扩额；保留现有终止与 grader 口径。
                store.append_event("turn_terminated", {
                    "reason": "model_step_budget_exhausted", "message": output.final_text,
                    "steps_used_in_turn": output.steps_used_in_turn,
                })
            patch = CandidateService(context, store).preview_patch()
            patch_hash = hashlib.sha256(patch).hexdigest() if patch else None
            prediction_path = root / "prediction.json"
            # 官方 harness 对 .json 和 .jsonl 采用不同协议；.json 即使只有一题，
            # 顶层也必须是 prediction 列表。
            prediction_path.write_text(json.dumps([{
                "instance_id": task.instance_id,
                "model_name_or_path": f"codeagent/{model_config.resolved_model}",
                "model_patch": patch.decode("utf-8"),
            }], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            oracle = self.grader.grade(task, prediction_path, run_id)
            result = SWEbenchRunResult(
                run_id, task.instance_id, store.session_id, output.status, output.final_text,
                bool(current_validation_evidence(store, context)), bool(patch), patch_hash,
                str(prediction_path), oracle.status, oracle.resolved, oracle.detail,
                _aggregate_token_usage(store),
                {
                    "image_ref": prepared.image_ref, "image_digest": prepared.image_digest,
                    "dataset_base_commit": prepared.dataset_base_commit,
                    "prepared_head": prepared.prepared_head, "prepared_tree": prepared.prepared_tree,
                },
            )
            (root / "result.json").write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return result
        finally:
            if context is not None:
                manager.discard(context)


def _resolved_from_report(value: object, instance_id: str) -> bool:
    if isinstance(value, dict):
        instance = value.get(instance_id)
        if isinstance(instance, dict) and isinstance(instance.get("resolved"), bool):
            return instance["resolved"]
        if isinstance(value.get("resolved"), bool):
            return value["resolved"]
        ids = value.get("resolved_ids")
        if isinstance(ids, list) and all(isinstance(item, str) for item in ids):
            return instance_id in ids
    raise ValueError("official report 不包含可识别的 resolved 结果")


def _aggregate_token_usage(store: SessionStore) -> dict[str, object]:
    events = [event for event in store.read_events() if event.type == "model_usage"]
    return aggregate_usage([
        event.payload.get("usage") if isinstance(event.payload.get("usage"), dict) else None
        for event in events
    ])
