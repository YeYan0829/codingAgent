from __future__ import annotations

import ast
import json
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Sequence

from codeagent.benchmark.swebench_harness import SWEbenchTask


class SWEbenchTaskRepositoryError(RuntimeError):
    """固定 task repo 的结构或元数据不符合 adapter 预期。"""


@dataclass(frozen=True)
class SWEbenchTaskRecord:
    task: SWEbenchTask
    dataset_record: dict[str, object]


class SWEbenchTaskRepository:
    """只读加载官方 task repo；gold/test patch 只进入 grader artifact。"""

    REQUIRED_METADATA = {
        "base_commit", "environment_setup_commit", "eval_type", "image",
        "instance_id", "log_parser", "repo", "split", "version",
    }

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.tasks_root = self.root / "tasks"
        if not self.tasks_root.is_dir():
            raise SWEbenchTaskRepositoryError(f"task repo 缺少 tasks/: {self.root}")

    def load(self, instance_id: str) -> SWEbenchTaskRecord:
        if not instance_id or "/" in instance_id or "\\" in instance_id or instance_id in {".", ".."}:
            raise SWEbenchTaskRepositoryError("instance_id 格式无效")
        task_dir = (self.tasks_root / instance_id).resolve()
        if task_dir.parent != self.tasks_root or not task_dir.is_dir():
            raise SWEbenchTaskRepositoryError(f"task 不存在: {instance_id}")
        metadata = _parse_task_yaml(task_dir / "task.yaml")
        missing = sorted(self.REQUIRED_METADATA - metadata.keys())
        if missing:
            raise SWEbenchTaskRepositoryError(f"task.yaml 缺少字段: {', '.join(missing)}")
        if metadata["instance_id"] != instance_id:
            raise SWEbenchTaskRepositoryError("目录名与 task.yaml instance_id 不一致")
        problem = _read(task_dir / "problem_statement.md")
        record: dict[str, object] = dict(metadata)
        record.update({
            "problem_statement": problem,
            "patch": _read(task_dir / "gold.patch"),
            "test_patch": _read(task_dir / "test.patch"),
            "eval_script": _read(task_dir / "eval.sh"),
            "hints_text": _read(task_dir / "hints.md", required=False),
        })
        try:
            tests = json.loads(_read(task_dir / "tests.json"))
        except json.JSONDecodeError as exc:
            raise SWEbenchTaskRepositoryError(f"tests.json 无效: {instance_id}: {exc}") from exc
        if not isinstance(tests, dict):
            raise SWEbenchTaskRepositoryError(f"tests.json 顶层必须是 object: {instance_id}")
        record.update(tests)
        task = SWEbenchTask(
            instance_id=instance_id,
            image=str(metadata["image"]),
            base_commit=str(metadata["base_commit"]),
            problem_statement=problem,
        )
        return SWEbenchTaskRecord(task, record)

    def load_metadata(self, instance_id: str) -> dict[str, object]:
        """只读取公开 task metadata；selection 阶段不得加载 gold/test 内容。"""
        if not instance_id or "/" in instance_id or "\\" in instance_id or instance_id in {".", ".."}:
            raise SWEbenchTaskRepositoryError("instance_id 格式无效")
        task_dir = (self.tasks_root / instance_id).resolve()
        if task_dir.parent != self.tasks_root or not task_dir.is_dir():
            raise SWEbenchTaskRepositoryError(f"task 不存在: {instance_id}")
        metadata = _parse_task_yaml(task_dir / "task.yaml")
        missing = sorted(self.REQUIRED_METADATA - metadata.keys())
        if missing:
            raise SWEbenchTaskRepositoryError(f"task.yaml 缺少字段: {', '.join(missing)}")
        if metadata["instance_id"] != instance_id:
            raise SWEbenchTaskRepositoryError("目录名与 task.yaml instance_id 不一致")
        return metadata

    def list_metadata(self) -> list[dict[str, object]]:
        return [self.load_metadata(path.name) for path in sorted(self.tasks_root.iterdir()) if path.is_dir()]

    def export_dataset(self, instance_ids: Sequence[str], path: str | Path) -> Path:
        destination = Path(path).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        records = [self.load(instance_id).dataset_record for instance_id in instance_ids]
        destination.write_text(json.dumps(records, ensure_ascii=False) + "\n", encoding="utf-8")
        return destination


@dataclass(frozen=True)
class GoldTaskResult:
    instance_id: str
    status: str
    resolved: bool | None
    detail: str = ""


@dataclass(frozen=True)
class GoldPreflightResult:
    run_id: str
    dataset_path: str
    report_path: str | None
    tasks: tuple[GoldTaskResult, ...]


class SWEbenchGoldPreflight:
    """运行官方 gold oracle；此流程独立于 Agent、Session 和 Candidate。"""

    def __init__(
        self,
        task_repository: SWEbenchTaskRepository,
        command_prefix: Sequence[str],
        *,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout_seconds: int = 14_400,
    ) -> None:
        self.task_repository = task_repository
        self.command_prefix = tuple(command_prefix)
        self.command_runner = command_runner
        self.timeout_seconds = timeout_seconds

    def run(self, instance_ids: Sequence[str], output_root: str | Path) -> GoldPreflightResult:
        ids = tuple(instance_ids)
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("gold preflight 需要非空且不重复的 instance_ids")
        run_id = "gold-" + uuid.uuid4().hex[:16]
        root = Path(output_root).expanduser().resolve() / run_id
        root.mkdir(parents=True, exist_ok=False)
        dataset = self.task_repository.export_dataset(ids, root / "dataset.json")
        report_dir = root / "report"
        argv = [
            *self.command_prefix,
            "--dataset_name", str(dataset), "--split", "test",
            "--predictions_path", "gold", "--instance_ids", *ids,
            "--run_id", run_id, "--report_dir", str(report_dir),
        ]
        try:
            proc = self.command_runner(
                argv, capture_output=True, text=True, shell=False, check=False,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            result = GoldPreflightResult(
                run_id, str(dataset), None,
                tuple(GoldTaskResult(item, "environment_failed", None, str(exc)) for item in ids),
            )
            self._save(root, result)
            return result
        (root / "grader.stdout.log").write_text(proc.stdout or "", encoding="utf-8")
        (root / "grader.stderr.log").write_text(proc.stderr or "", encoding="utf-8")
        report_path = report_dir / f"gold.{run_id}.json"
        if proc.returncode or not report_path.is_file():
            detail = ((proc.stderr or proc.stdout) or "official report missing")[-4000:]
            result = GoldPreflightResult(
                run_id, str(dataset), str(report_path),
                tuple(GoldTaskResult(item, "environment_failed", None, detail) for item in ids),
            )
            self._save(root, result)
            return result
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            tasks = tuple(_classify_gold(item, report) for item in ids)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            tasks = tuple(GoldTaskResult(item, "environment_failed", None, str(exc)) for item in ids)
        result = GoldPreflightResult(run_id, str(dataset), str(report_path), tasks)
        self._save(root, result)
        return result

    @staticmethod
    def _save(root: Path, result: GoldPreflightResult) -> None:
        value = asdict(result)
        (root / "preflight-result.json").write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def _classify_gold(instance_id: str, report: object) -> GoldTaskResult:
    if not isinstance(report, dict):
        raise ValueError("official gold report 顶层不是 object")
    groups = {
        key: set(value) for key, value in report.items()
        if key.endswith("_ids") and isinstance(value, list) and all(isinstance(x, str) for x in value)
    }
    if instance_id in groups.get("resolved_ids", set()):
        return GoldTaskResult(instance_id, "gold_passed", True, "official gold resolved")
    infrastructure = set().union(
        groups.get("infra_failure_ids", set()), groups.get("error_ids", set()),
        groups.get("incomplete_ids", set()), groups.get("ambiguous_failure_ids", set()),
    )
    if instance_id in infrastructure:
        return GoldTaskResult(instance_id, "environment_failed", None, "official harness reported infrastructure/error state")
    if instance_id in groups.get("unresolved_ids", set()):
        return GoldTaskResult(instance_id, "gold_failed", False, "official gold patch did not resolve all required tests")
    return GoldTaskResult(instance_id, "environment_failed", None, "instance missing from recognizable report groups")


def _parse_task_yaml(path: Path) -> dict[str, object]:
    """解析 task.yaml 的顶层 scalar；列表等非评分 identity 字段明确忽略。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SWEbenchTaskRepositoryError(f"无法读取 {path}: {exc}") from exc
    result: dict[str, object] = {}
    for line in lines:
        if not line or line[0].isspace() or line.startswith("-") or ":" not in line:
            continue
        key, raw = line.split(":", 1)
        raw = raw.strip()
        if not raw:
            continue
        try:
            value = ast.literal_eval(raw) if raw[:1] in {"'", '"'} else raw
        except (SyntaxError, ValueError) as exc:
            raise SWEbenchTaskRepositoryError(f"task.yaml scalar 无效: {key}") from exc
        result[key] = value
    return result


def _read(path: Path, *, required: bool = True) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if not required:
            return ""
        raise SWEbenchTaskRepositoryError(f"task artifact 缺失: {path}") from None
