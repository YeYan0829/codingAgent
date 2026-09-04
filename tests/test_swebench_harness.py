import hashlib
import json
import subprocess
from pathlib import Path

from codeagent.benchmark import OracleResult, PreparedSWEbenchSource, SWEbenchCLIGrader, SWEbenchHarness, SWEbenchTask
from codeagent.config import ModelConfig
from codeagent.model_gateway.base import BaseModelClient, LLMResponse, LLMToolCall, ModelRequest, TokenUsage


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True).stdout.strip()


class SourcePreparer:
    def __init__(self, source): self.source = source
    def prepare(self, task):
        head = git(self.source, "rev-parse", "HEAD")
        return PreparedSWEbenchSource(task.instance_id, task.image, "sha256:" + "a" * 64,
            task.dataset_base_commit, head, git(self.source, "rev-parse", "HEAD^{tree}"),
            self.source, self.source.parent / "manifest.json", True)


class EditModel(BaseModelClient):
    step = 0
    def __init__(self, sha): self.sha = sha
    def complete(self, request: ModelRequest):
        if self.step == 0:
            self.step += 1
            return LLMResponse(tool_calls=[LLMToolCall(call_id="edit", name="apply_workspace_edit", arguments={"operations": [{
                "op": "replace_text", "path": "bug.py", "expected_sha256": self.sha,
                "old_text": "BROKEN = True", "new_text": "BROKEN = False",
            }]})], usage=TokenUsage(input_tokens=100, output_tokens=20, total_tokens=120,
                                    cached_input_tokens=60))
        return LLMResponse(text="修复完成", usage=TokenUsage(input_tokens=140, output_tokens=10, total_tokens=150,
                                                             cached_input_tokens=90))


class Grader:
    def __init__(self): self.prediction = None
    def grade(self, task, prediction_path, run_id):
        predictions = json.loads(prediction_path.read_text())
        assert isinstance(predictions, list) and len(predictions) == 1
        self.prediction = predictions[0]
        return OracleResult("completed", True, "resolved", "/tmp/report.json")


def test_single_task_exports_prediction_and_separates_three_states(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    git(repo, "init"); git(repo, "config", "user.email", "x@y"); git(repo, "config", "user.name", "x")
    path = repo / "bug.py"; path.write_text("BROKEN = True\n")
    git(repo, "add", "."); git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    grader = Grader()
    result = SWEbenchHarness(SourcePreparer(repo), grader).run(
        SWEbenchTask("owner__repo-1", "example/image:latest", base, "修复 bug"),
        EditModel(hashlib.sha256(path.read_bytes()).hexdigest()), ModelConfig(), tmp_path / "runs")
    assert result.agent_status == "completed"
    assert result.agent_final == "修复完成"
    assert result.patch_present and result.validation_passed is False
    assert result.oracle_status == "completed" and result.oracle_passed is True
    assert "BROKEN = False" in grader.prediction["model_patch"]
    assert not (repo / "bug.py").read_text().startswith("BROKEN = False")
    saved = json.loads((Path(result.prediction_path).parent / "result.json").read_text())
    assert saved["patch_sha256"] == result.patch_sha256
    assert saved["token_usage"] == {
        "requests": 2, "requests_with_usage": 2, "input_tokens": 240, "output_tokens": 30,
        "total_tokens": 270, "cached_input_tokens": 150, "cache_miss_input_tokens": None,
        "reasoning_tokens": None,
    }


def test_cli_grader_uses_fixed_argv_and_explicit_report(tmp_path):
    report = tmp_path / "run-1.json"
    report.write_text(json.dumps({"resolved_ids": ["owner__repo-1"]}))
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "ok", "")
    grader = SWEbenchCLIGrader(
        ["python", "-m", "swebench.harness.run_evaluation"],
        str(tmp_path / "{run_id}.json"), command_runner=run)
    prediction = tmp_path / "prediction.json"; prediction.write_text("{}")
    result = grader.grade(SWEbenchTask("owner__repo-1", "image", "a" * 40, "bug"), prediction, "run-1")
    assert result.status == "completed" and result.resolved is True
    assert calls[0][0][-6:] == ["--predictions_path", str(prediction), "--instance_ids", "owner__repo-1", "--run_id", "run-1"]
    assert calls[0][1]["shell"] is False
