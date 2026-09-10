import hashlib
import json
import subprocess
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from codeagent.benchmark.batch import BatchCompatibilityError, SWEbenchBatchRunner, SWEbenchSelection
from codeagent.benchmark.accounting import PriceSnapshot
from codeagent.benchmark.manifest import build_run_manifest, config_fingerprint, file_sha256
from codeagent.benchmark.swebench_harness import SWEbenchRunResult, SWEbenchTask
from codeagent.config import ModelConfig, RuntimeConfig


class FakeHarness:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def run(self, task, model, model_config, output_root, runtime_config=None, progress_callback=None):
        self.calls.append(task.instance_id)
        if progress_callback:
            progress_callback("agent")
        if task.instance_id == self.fail_on:
            raise RuntimeError("planned")
        run_id = "run-" + task.instance_id
        root = Path(output_root) / run_id
        session = root / "sessions" / "session-1"
        session.mkdir(parents=True, exist_ok=True)
        (root / "sessions" / "worktrees").mkdir(exist_ok=True)
        (session / "events.jsonl").write_text(
            json.dumps({"type": "model_usage", "payload": {"usage": {"input_tokens": 3,
                "output_tokens": 2, "total_tokens": 5, "cached_input_tokens": 0,
                "cache_miss_input_tokens": 3, "reasoning_tokens": 0}}}) + "\n" +
            json.dumps({"type": "assistant_message", "payload": {"message": "done"}}) + "\n")
        prediction = root / "prediction.json"; prediction.write_text("[]")
        if progress_callback:
            progress_callback("grader")
        return SWEbenchRunResult(run_id, task.instance_id, "session-1", "completed", "done",
            True, True, "abc", str(prediction), "completed", True, "ok",
            {"requests": 1, "requests_with_usage": 1, "coverage": "complete",
             "coverage_ratio": "1", "input_tokens": 3, "output_tokens": 2,
             "total_tokens": 5, "cached_input_tokens": 0, "cache_miss_input_tokens": 3,
             "reasoning_tokens": 0, "input_tokens_complete": True,
             "output_tokens_complete": True, "total_tokens_complete": True,
             "cached_input_tokens_complete": True, "cache_miss_input_tokens_complete": True,
             "reasoning_tokens_complete": True, "cache_miss_input_tokens_derived": False},
            {"image_ref": "image:tag", "image_digest": "sha256:" + "a" * 64,
             "dataset_base_commit": "b", "prepared_head": "h", "prepared_tree": "t"})


def make_selection(tmp_path):
    path = tmp_path / "selection.json"
    path.write_text(json.dumps({"selection_id": "three", "tasks": [
        {"instance_id": "one"}, {"instance_id": "two"}, {"instance_id": "three"}]}, indent=2))
    return SWEbenchSelection(path)


def make_runner(tmp_path, harness, selection=None, model=None, **kwargs):
    return SWEbenchBatchRunner(selection=selection or make_selection(tmp_path),
        task_loader=lambda item: SWEbenchTask(item, "image:tag", "a" * 40, "fix"),
        harness=harness, model_factory=lambda: object(), model_config=model or ModelConfig(),
        runtime_config=RuntimeConfig(), output_root=tmp_path / "out", endpoint_identity="local-fake",
        runtime_root=tmp_path, **kwargs)


def cny_snapshot():
    return PriceSnapshot("test", "fake", "fake", "2026-09-08", "CNY", 1,
        {"input": "1", "output": "1", "cached_input": "1", "reasoning": "1"}, "test")


def test_batch_runs_in_order_persists_each_task_and_summarizes(tmp_path):
    harness = FakeHarness(); runner = make_runner(tmp_path, harness)
    summary = runner.run("batch-1")
    assert harness.calls == ["one", "two", "three"]
    assert summary["finished"] and summary["resolved"] == 3
    assert summary["usage"]["coverage"] == "complete" and summary["usage"]["total_tokens"] == 15
    assert summary["usage"]["input_tokens_complete"] is True
    states = sorted((tmp_path / "out/batch-1/tasks").glob("*/task-state.json"))
    assert len(states) == 3 and all(json.loads(x.read_text())["status"] == "completed" for x in states)
    for path in states:
        state = json.loads(path.read_text())
        assert state["phase"] == "completed"
        assert state["started_at"] <= state["updated_at"] == state["completed_at"]
    assert summary["current_task"] is None


def test_batch_exposes_current_task_and_grader_phase_while_running(tmp_path):
    snapshots = []

    class InspectingHarness(FakeHarness):
        def run(self, task, model, model_config, output_root, runtime_config=None,
                progress_callback=None):
            progress_callback("grader")
            batch_root = Path(output_root).parents[2]
            snapshots.append(json.loads((batch_root / "batch-summary.json").read_text()))
            return super().run(task, model, model_config, output_root, runtime_config,
                               progress_callback=None)

    make_runner(tmp_path, InspectingHarness()).run("batch-progress")

    assert snapshots[0]["current_task"]["instance_id"] == "one"
    assert snapshots[0]["current_task"]["phase"] == "grader"


def test_formal_selection_is_fixed_easy_medium_hard_order(tmp_path):
    formal = (Path(__file__).parents[1] /
              "benchmarks/swebench/selections/hal-verified-mini-50.json")
    value = json.loads(formal.read_text())
    difficulties = [item["difficulty"] for item in value["tasks"]]
    assert difficulties == ["easy"] * 20 + ["medium"] * 24 + ["hard"] * 6
    assert len(value["official_instance_ids"]) == len(set(value["official_instance_ids"])) == 50
    assert {item["instance_id"] for item in value["tasks"]} == set(value["official_instance_ids"])
    official_bytes = ("\n".join(value["official_instance_ids"]) + "\n").encode()
    assert hashlib.sha256(official_bytes).hexdigest() == value["source"]["task_ids_sha256"]
    for difficulty in ("easy", "medium", "hard"):
        official_indexes = [item["hal_official_index"] for item in value["tasks"]
                            if item["difficulty"] == difficulty]
        assert official_indexes == sorted(official_indexes)
    assert len(SWEbenchSelection(formal).instance_ids) == 50

    invalid = tmp_path / "invalid-order.json"
    invalid.write_text(json.dumps({"selection_id": "bad", "tasks": [
        {"instance_id": "hard-first", "difficulty": "hard"},
        {"instance_id": "easy-last", "difficulty": "easy"},
    ]}))
    with pytest.raises(ValueError, match="Easy → Medium → Hard"):
        SWEbenchSelection(invalid)


def test_evaluation_candidate_machine_config_matches_runtime_contract():
    root = Path(__file__).parents[1]
    path = root / "benchmarks/swebench/configs/v0.5.0-evaluation-candidate-hal-mini-50.json"
    value = json.loads(path.read_text())
    selection = root / value["selection"]["path"]
    pricing = root / value["pricing"]["path"]

    assert file_sha256(selection) == value["selection"]["sha256"]
    assert file_sha256(pricing) == value["pricing"]["sha256"]
    assert value["model"] == {
        "provider": "glm", "model": "glm-5.3",
        "endpoint_identity": "https://open.bigmodel.cn/api/paas/v4",
        "temperature": 1.0, "reasoning_enabled": True, "reasoning_effort": "high",
        "clear_thinking": True, "max_output_tokens": 8192,
        "output_truncation_recovery_effort": "low", "restore_effort_after_recovery": "high",
    }
    assert value["runtime"]["max_steps_per_slice"] == 12
    assert value["runtime"]["max_model_steps_per_task"] == 72
    assert value["runtime"]["context"] == {
        "max_events": 80, "target_events": 40, "soft_token_ratio": 0.8,
        "target_token_ratio": 0.5, "minimum_progress": 0.1,
        "tool_result_max_chars": 16000,
    }
    assert value["runtime"]["condenser"]["summary_max_tokens"] == 2048
    assert value["runtime"]["condenser"]["safety_margin"] == 2000
    assert value["orchestration"]["cost_guard"]["evaluation_budget"] == "250"
    assert value["orchestration"]["cost_guard"]["stop_before_task_when_remaining_below"] == "10"


def test_failure_keeps_completed_and_resume_skips_it(tmp_path):
    first = FakeHarness(fail_on="two")
    with pytest.raises(RuntimeError):
        make_runner(tmp_path, first).run("batch-2")
    first_state = next((tmp_path / "out/batch-2/tasks").glob("0000-*/task-state.json"))
    assert json.loads(first_state.read_text())["status"] == "completed"
    resumed = FakeHarness(); summary = make_runner(tmp_path, resumed).run("batch-2")
    assert resumed.calls == ["two", "three"] and summary["completed_tasks"] == 3
    retried_state = next((tmp_path / "out/batch-2/tasks").glob("0001-*/task-state.json"))
    assert json.loads(retried_state.read_text())["attempt"] == 2


def test_normally_completed_unresolved_is_persisted_and_skipped_on_resume(tmp_path):
    class UnresolvedHarness(FakeHarness):
        def run(self, *args, **kwargs):
            return replace(super().run(*args, **kwargs), oracle_passed=False,
                           oracle_detail="not resolved", oracle_report_path="reports/report.json")

    first = UnresolvedHarness()
    summary = make_runner(tmp_path, first).run("batch-unresolved")
    assert summary["outcomes"] == {
        "resolved": 0, "unresolved": 3, "budget_exhausted": 0,
        "output_truncated": 0, "provider_error": 0, "infra_error": 0, "not_started": 0,
    }
    state = next((tmp_path / "out/batch-unresolved/tasks").glob("0000-*/task-state.json"))
    result = json.loads(state.read_text())["result"]
    assert result["attempt"] == 1 and result["outcome"] == "unresolved"
    assert result["oracle_report_path"] == "reports/report.json"

    resumed = FakeHarness()
    resumed_summary = make_runner(tmp_path, resumed).run("batch-unresolved")
    assert resumed.calls == []
    assert resumed_summary["outcomes"]["unresolved"] == 3


def test_failed_task_records_infra_outcome_and_not_started_count(tmp_path):
    harness = FakeHarness(fail_on="two")
    with pytest.raises(RuntimeError):
        make_runner(tmp_path, harness).run("batch-infra")
    summary = json.loads((tmp_path / "out/batch-infra/batch-summary.json").read_text())
    assert summary["outcomes"] == {
        "resolved": 1, "unresolved": 0, "budget_exhausted": 0,
        "output_truncated": 0, "provider_error": 0, "infra_error": 1, "not_started": 1,
    }


def test_grader_infra_result_stops_and_is_not_treated_as_complete(tmp_path):
    class GraderInfraHarness(FakeHarness):
        def run(self, *args, **kwargs):
            return replace(
                super().run(*args, **kwargs), oracle_status="report_invalid",
                oracle_passed=None, oracle_detail="missing report",
            )

    with pytest.raises(RuntimeError, match="grader infrastructure failed"):
        make_runner(tmp_path, GraderInfraHarness()).run("batch-grader-infra")
    state = next((tmp_path / "out/batch-grader-infra/tasks").glob("0000-*/task-state.json"))
    value = json.loads(state.read_text())
    assert value["status"] == "failed"
    assert value["outcome"] == "infra_error"
    assert value["result"]["oracle_status"] == "report_invalid"

    resumed = FakeHarness()
    summary = make_runner(tmp_path, resumed).run("batch-grader-infra")
    assert resumed.calls == ["one", "two", "three"]
    assert summary["finished"] is True


def test_incomplete_task_reexecutes_and_config_or_selection_mismatch_fails(tmp_path):
    runner = make_runner(tmp_path, FakeHarness()); runner.run("batch-3")
    state = next((tmp_path / "out/batch-3/tasks").glob("0001-*/task-state.json"))
    value = json.loads(state.read_text()); value["status"] = "running"; state.write_text(json.dumps(value))
    resumed = FakeHarness(); make_runner(tmp_path, resumed).run("batch-3")
    assert resumed.calls == ["two"]
    with pytest.raises(BatchCompatibilityError):
        make_runner(tmp_path, FakeHarness(), model=ModelConfig(max_tokens=99)).run("batch-3")
    path = tmp_path / "selection.json"; raw = json.loads(path.read_text()); raw["tasks"].reverse(); path.write_text(json.dumps(raw))
    with pytest.raises(BatchCompatibilityError):
        make_runner(tmp_path, FakeHarness(), selection=SWEbenchSelection(path)).run("batch-3")


def test_cost_guard_stops_before_next_task_and_never_interrupts_started_task(tmp_path):
    harness = FakeHarness()
    runner = make_runner(tmp_path, harness, price_snapshot=cny_snapshot(),
                         cost_budget_cny=Decimal("14"), minimum_remaining_cost_cny="10")
    summary = runner.run("batch-cost")
    assert harness.calls == ["one"]
    assert summary["finished"] is False
    assert summary["completed_tasks"] == 1
    assert summary["stop_reason"] == "remaining_cost_below_threshold"
    assert summary["cost_guard"] == {
        "enabled": True, "can_start_next_task": False,
        "reason": "remaining_cost_below_threshold", "currency": "CNY",
        "budget": "14", "spent": "5", "remaining": "9",
        "stop_before_task_below": "10",
    }


def test_cost_guard_allows_exact_threshold_and_resume_skips_completed(tmp_path):
    first = FakeHarness(fail_on="two")
    with pytest.raises(RuntimeError):
        make_runner(tmp_path, first, price_snapshot=cny_snapshot(),
                    cost_budget_cny="20", minimum_remaining_cost_cny="10").run("batch-cost-resume")
    resumed = FakeHarness()
    summary = make_runner(tmp_path, resumed, price_snapshot=cny_snapshot(),
                          cost_budget_cny="20", minimum_remaining_cost_cny="10").run("batch-cost-resume")
    assert first.calls == ["one", "two"]
    assert resumed.calls == ["two", "three"]
    assert summary["finished"] is True


def test_price_snapshot_requires_cost_guard_budget(tmp_path):
    with pytest.raises(ValueError, match="必须设置 cost_budget_cny"):
        make_runner(tmp_path, FakeHarness(), price_snapshot=cny_snapshot())


def test_cost_guard_stops_when_completed_cost_is_incomplete(tmp_path):
    class IncompleteUsageHarness(FakeHarness):
        def run(self, *args, **kwargs):
            result = super().run(*args, **kwargs)
            usage = {**result.token_usage, "coverage": "partial", "output_tokens": None,
                     "output_tokens_complete": False}
            return replace(result, token_usage=usage)

    runner = make_runner(tmp_path, IncompleteUsageHarness(), price_snapshot=cny_snapshot(),
                         cost_budget_cny="20")
    summary = runner.run("batch-incomplete")
    assert summary["finished"] is False
    assert summary["stop_reason"] == "cost_accounting_incomplete"
    assert runner.harness.calls == ["one"]


def test_manifest_records_identity_dirty_runtime_and_stable_fingerprint(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "x@y"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=repo, check=True)
    (repo / "a").write_text("a"); subprocess.run(["git", "add", "a"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    selection = tmp_path / "s.json"; selection.write_text('{"selection_id":"s","tasks":["x"]}')
    model = ModelConfig(provider="glm", max_tokens=123, reasoning_enabled=True, reasoning_effort="max")
    runtime = RuntimeConfig(2, 7)
    one = build_run_manifest(run_id="r", selection_path=selection, selection_id="s", model=model,
        runtime=runtime, endpoint_identity="https://user:secret@example.invalid/v1?api_key=secret", swebench_commit="upstream", runtime_root=repo)
    assert one["selection_sha256"] == file_sha256(selection)
    assert one["runtime"]["git_commit"] and one["runtime"]["dirty"] is False
    assert one["runtime"]["generation_reserve"] == 123 and one["runtime"]["max_model_steps_per_user_turn"] == 7
    assert one["model"]["reasoning"] == {
        "enabled": True, "effort": "max",
        "immediate_output_truncation_recovery_effort": "max",
        "restore_after_recovery": "max", "clear_thinking": True,
    }
    assert one["runtime"]["context_reduction"] == "semantic-condensation-v1"
    assert one["runtime"]["context"]["max_events"] == 80
    assert one["runtime"]["condenser"]["max_output_tokens"] == 2048
    serialized = json.dumps(one).lower()
    assert "secret" not in serialized and "api_key" not in serialized
    two = build_run_manifest(run_id="other", selection_path=selection, selection_id="s", model=model,
        runtime=runtime, endpoint_identity="https://example.invalid/v1", swebench_commit="upstream", runtime_root=repo)
    assert one["model"]["config_fingerprint"] == two["model"]["config_fingerprint"]
    changed = {"x": one["model"]["config_fingerprint"]}
    assert config_fingerprint(changed) != one["model"]["config_fingerprint"]
    (repo / "a").write_text("dirty")
    dirty = build_run_manifest(run_id="r", selection_path=selection, selection_id="s", model=model,
        runtime=runtime, endpoint_identity="x", swebench_commit=None, runtime_root=repo)
    assert dirty["runtime"]["dirty"] is True


def test_resume_rejects_different_evaluation_commit(tmp_path):
    runner = make_runner(tmp_path, FakeHarness())
    runner.run("batch-commit")
    manifest_path = tmp_path / "out/batch-commit/run-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["runtime"]["git_commit"] = "different"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(BatchCompatibilityError, match="runtime.git_commit"):
        make_runner(tmp_path, FakeHarness()).run("batch-commit")
