import json
import subprocess
from pathlib import Path

import pytest

from codeagent.benchmark.batch import BatchCompatibilityError, SWEbenchBatchRunner, SWEbenchSelection
from codeagent.benchmark.manifest import build_run_manifest, config_fingerprint, file_sha256
from codeagent.benchmark.swebench_harness import SWEbenchRunResult, SWEbenchTask
from codeagent.config import ModelConfig, RuntimeConfig


class FakeHarness:
    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def run(self, task, model, model_config, output_root, runtime_config=None):
        self.calls.append(task.instance_id)
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


def make_runner(tmp_path, harness, selection=None, model=None):
    return SWEbenchBatchRunner(selection=selection or make_selection(tmp_path),
        task_loader=lambda item: SWEbenchTask(item, "image:tag", "a" * 40, "fix"),
        harness=harness, model_factory=lambda: object(), model_config=model or ModelConfig(),
        runtime_config=RuntimeConfig(), output_root=tmp_path / "out", endpoint_identity="local-fake",
        runtime_root=tmp_path)


def test_batch_runs_in_order_persists_each_task_and_summarizes(tmp_path):
    harness = FakeHarness(); runner = make_runner(tmp_path, harness)
    summary = runner.run("batch-1")
    assert harness.calls == ["one", "two", "three"]
    assert summary["finished"] and summary["resolved"] == 3
    assert summary["usage"]["coverage"] == "complete" and summary["usage"]["total_tokens"] == 15
    assert summary["usage"]["input_tokens_complete"] is True
    states = sorted((tmp_path / "out/batch-1/tasks").glob("*/task-state.json"))
    assert len(states) == 3 and all(json.loads(x.read_text())["status"] == "completed" for x in states)


def test_failure_keeps_completed_and_resume_skips_it(tmp_path):
    first = FakeHarness(fail_on="two")
    with pytest.raises(RuntimeError):
        make_runner(tmp_path, first).run("batch-2")
    first_state = next((tmp_path / "out/batch-2/tasks").glob("0000-*/task-state.json"))
    assert json.loads(first_state.read_text())["status"] == "completed"
    resumed = FakeHarness(); summary = make_runner(tmp_path, resumed).run("batch-2")
    assert resumed.calls == ["two", "three"] and summary["completed_tasks"] == 3


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


def test_manifest_records_identity_dirty_runtime_and_stable_fingerprint(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "x@y"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=repo, check=True)
    (repo / "a").write_text("a"); subprocess.run(["git", "add", "a"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    selection = tmp_path / "s.json"; selection.write_text('{"selection_id":"s","tasks":["x"]}')
    model, runtime = ModelConfig(provider="glm", max_tokens=123), RuntimeConfig(2, 7)
    one = build_run_manifest(run_id="r", selection_path=selection, selection_id="s", model=model,
        runtime=runtime, endpoint_identity="https://user:secret@example.invalid/v1?api_key=secret", swebench_commit="upstream", runtime_root=repo)
    assert one["selection_sha256"] == file_sha256(selection)
    assert one["runtime"]["git_commit"] and one["runtime"]["dirty"] is False
    assert one["runtime"]["generation_reserve"] == 123 and one["runtime"]["max_model_steps_per_user_turn"] == 7
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
