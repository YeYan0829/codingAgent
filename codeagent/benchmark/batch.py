from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from codeagent.benchmark.accounting import PriceSnapshot, aggregate_usage, calculate_cost
from codeagent.benchmark.manifest import build_run_manifest, write_json_atomic
from codeagent.benchmark.swebench_harness import SWEbenchHarness, SWEbenchRunResult, SWEbenchTask
from codeagent.benchmark.trajectory import analyze_trajectory, load_events, save_trajectory
from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.model_gateway.base import BaseModelClient


class BatchCompatibilityError(RuntimeError):
    pass


class SWEbenchSelection:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        value = json.loads(self.path.read_text(encoding="utf-8"))
        self.selection_id = str(value["selection_id"])
        self.swebench_commit = (value.get("pinned_sources") or {}).get("swebench_commit")
        tasks = value.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise ValueError("selection tasks 必须是非空列表")
        self.instance_ids = tuple(str(x["instance_id"] if isinstance(x, dict) else x) for x in tasks)
        if len(set(self.instance_ids)) != len(self.instance_ids):
            raise ValueError("selection 包含重复 task")


class SWEbenchBatchRunner:
    """确定性串行 batch；单题 harness 仍负责 Agent、Candidate 与 grader 语义。"""

    def __init__(self, *, selection: SWEbenchSelection, task_loader: Callable[[str], SWEbenchTask],
                 harness: SWEbenchHarness, model_factory: Callable[[], BaseModelClient],
                 model_config: ModelConfig, runtime_config: RuntimeConfig,
                 output_root: str | Path, endpoint_identity: str,
                 price_snapshot: PriceSnapshot | None = None,
                 runtime_root: str | Path = ".") -> None:
        self.selection, self.task_loader, self.harness = selection, task_loader, harness
        self.model_factory, self.model_config, self.runtime_config = model_factory, model_config, runtime_config
        self.output_root, self.endpoint_identity = Path(output_root).resolve(), endpoint_identity
        self.price_snapshot, self.runtime_root = price_snapshot, Path(runtime_root).resolve()
        if price_snapshot and (price_snapshot.provider != model_config.provider or
                               price_snapshot.model != model_config.resolved_model):
            raise ValueError("价格快照与 provider/model 不匹配")

    def run(self, run_id: str | None = None) -> dict:
        run_id = run_id or "batch-" + uuid.uuid4().hex[:16]
        root = self.output_root / run_id
        manifest_path = root / "run-manifest.json"
        expected = build_run_manifest(
            run_id=run_id, selection_path=self.selection.path, selection_id=self.selection.selection_id,
            model=self.model_config, runtime=self.runtime_config, endpoint_identity=self.endpoint_identity,
            swebench_commit=self.selection.swebench_commit, runtime_root=self.runtime_root,
        )
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self._validate_resume(manifest, expected)
        else:
            root.mkdir(parents=True, exist_ok=False)
            manifest = expected
            write_json_atomic(manifest_path, manifest)

        completed: list[dict] = []
        for index, instance_id in enumerate(self.selection.instance_ids):
            task_root = root / "tasks" / f"{index:04d}-{instance_id}"
            state_path = task_root / "task-state.json"
            reused = self._load_complete(state_path, manifest)
            if reused is not None:
                completed.append(reused)
                continue
            task_root.mkdir(parents=True, exist_ok=True)
            state = {"schema_version": 1, "instance_id": instance_id, "status": "running",
                     "config_fingerprint": manifest["model"]["config_fingerprint"]}
            write_json_atomic(state_path, state)
            try:
                result = self.harness.run(self.task_loader(instance_id), self.model_factory(),
                                          self.model_config, task_root / "runs", runtime_config=self.runtime_config)
                entry = self._finish_task(task_root, result)
                if result.source_identity:
                    manifest["benchmark"]["tasks"][instance_id] = result.source_identity
                    write_json_atomic(manifest_path, manifest)
                state.update({"status": "completed", "result": entry})
                write_json_atomic(state_path, state)
                completed.append(entry)
            except Exception as exc:
                state.update({"status": "failed", "error_type": type(exc).__name__, "error": str(exc)})
                write_json_atomic(state_path, state)
                self._write_summary(root, manifest, completed, finished=False)
                raise
            self._write_summary(root, manifest, completed, finished=False)
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json_atomic(manifest_path, manifest)
        return self._write_summary(root, manifest, completed, finished=True)

    def _finish_task(self, task_root: Path, result: SWEbenchRunResult) -> dict:
        raw = asdict(result)
        event_paths = list((task_root / "runs" / result.run_id / "sessions").rglob("events.jsonl"))
        matching = [path for path in event_paths if path.parent.name == result.session_id]
        if len(matching) != 1:
            raise RuntimeError(
                f"无法唯一定位 Session events: session={result.session_id}, matches={len(matching)}"
            )
        trajectory_path = task_root / "trajectory-summary.json"
        save_trajectory(trajectory_path, analyze_trajectory(load_events(matching[0]), raw))
        usage = raw.get("token_usage")
        cost = calculate_cost(usage, self.price_snapshot) if self.price_snapshot and isinstance(usage, dict) else None
        return {**raw, "trajectory_path": str(trajectory_path), "cost": cost}

    def _load_complete(self, path: Path, manifest: dict) -> dict | None:
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if value.get("config_fingerprint") != manifest["model"]["config_fingerprint"]:
            raise BatchCompatibilityError(f"历史 task config 不兼容: {path}")
        result = value.get("result")
        required = {"instance_id", "agent_status", "prediction_path", "oracle_status", "trajectory_path"}
        if value.get("status") != "completed" or not isinstance(result, dict) or not required <= result.keys():
            return None
        if not Path(result["prediction_path"]).is_file() or not Path(result["trajectory_path"]).is_file():
            return None
        event_paths = list(Path(result["prediction_path"]).parent.joinpath("sessions").rglob("events.jsonl"))
        matching = [item for item in event_paths if item.parent.name == result.get("session_id")]
        if len(matching) != 1:
            return None
        save_trajectory(result["trajectory_path"], analyze_trajectory(load_events(matching[0]), result))
        return result

    @staticmethod
    def _validate_resume(old: dict, new: dict) -> None:
        fields = (("selection_id",), ("selection_sha256",), ("model", "config_fingerprint"))
        for field in fields:
            left, right = old, new
            for part in field:
                left = left.get(part) if isinstance(left, dict) else None
                right = right.get(part) if isinstance(right, dict) else None
            if left != right:
                raise BatchCompatibilityError(f"resume identity 不兼容: {'.'.join(field)}")

    def _write_summary(self, root: Path, manifest: dict, tasks: list[dict], *, finished: bool) -> dict:
        usages = [x.get("token_usage") for x in tasks]
        # 逐题汇总已经包含 request 计数；这里按字段保守聚合，旧结果缺失即 unavailable。
        request_items: list[dict | None] = []
        for usage in usages:
            if not isinstance(usage, dict):
                request_items.append(None)
                continue
            count = int(usage.get("requests", 0))
            known = int(usage.get("requests_with_usage", 0))
            request_items.extend([{}] * known + [None] * max(0, count - known))
        coverage = aggregate_usage(request_items)
        totals = {}
        for field in ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
                      "cache_miss_input_tokens", "reasoning_tokens"):
            vals = [u.get(field) for u in usages if isinstance(u, dict)]
            totals[field] = sum(vals) if vals and all(isinstance(x, int) for x in vals) else None
            totals[field + "_complete"] = bool(usages) and all(
                isinstance(u, dict) and u.get(field + "_complete") is True for u in usages
            )
        totals["cache_miss_input_tokens_derived"] = bool(usages) and all(
            isinstance(u, dict) and u.get("cache_miss_input_tokens_derived") is True for u in usages
        )
        usage = {**coverage, **totals}
        cost = calculate_cost(usage, self.price_snapshot) if self.price_snapshot else None
        summary = {
            "schema_version": 1, "run_id": manifest["run_id"], "selection_id": manifest["selection_id"],
            "finished": finished, "completed_tasks": len(tasks), "total_tasks": len(self.selection.instance_ids),
            "resolved": sum(x.get("oracle_passed") is True for x in tasks), "usage": usage,
            "cost": cost, "tasks": tasks,
        }
        write_json_atomic(root / "batch-summary.json", summary)
        return summary
