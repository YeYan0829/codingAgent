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

    def open_workspace(self, task, root):
        return LocalWorkspace(self.prepare(task), root)


class LocalWorkspace:
    """正式容器工作区协议的离线替身；真实Docker行为由集成测试覆盖。"""
    def __init__(self, prepared, root):
        from codeagent.benchmark.workspace import prepare_swebench_workspace
        from codeagent.workspace.workspace import WorkspaceContext
        self.root, self.closed = root, False
        self.container_id = 'offline-workspace-fixture'
        prepare_swebench_workspace(prepared, root / 'fixture-workspace')
        self.context = WorkspaceContext.source(root / 'fixture-workspace')
        self.identity = {k: getattr(prepared, k) for k in
                         ('image_ref','image_digest','dataset_base_commit','prepared_head','prepared_tree')}
        self.command_executor = LocalCommandExecutor(self)

    def build_read_tools(self, context):
        from codeagent.tools.fs_read import build_fs_tools
        from codeagent.tools.git_read import build_git_tools
        return build_fs_tools(context) + build_git_tools(context)

    def edit_service(self, context, store):
        from codeagent.runtime.atomic_edit import AtomicEditService
        return AtomicEditService(context, store)

    def prediction_patch(self):
        from codeagent.benchmark.workspace import prediction_patch
        return prediction_patch(self.context.active_root)

    def close(self):
        self.closed = True
        self.container_id = None


class LocalCommandExecutor:
    def __init__(self, workspace):
        self.workspace = workspace

    def execute(self, request, context, artifacts):
        from codeagent.runtime.command import CommandResult
        (context.active_root / 'from-container.txt').write_text('fixture command\n')
        return CommandResult(exit_code=0, stdout='1 passed\n', stderr='', payload_started=True,
                             process_tree_stopped=True)

    def close(self):
        self.workspace.close()


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


class FinishCaptureModel(BaseModelClient):
    def complete(self, request):
        import os
        import uuid
        folder = Path(os.environ["PAIR_TEST_CAPTURE"])
        (folder / (uuid.uuid4().hex + ".json")).write_text(request.model_dump_json())
        return LLMResponse(text="无需修改", finish_reason="stop")


def finish_capture_factory(config):
    return FinishCaptureModel()


def test_same_candidate_optional_delegation_preserves_common_prompt(tmp_path, monkeypatch):
    from codeagent.config import RuntimeConfig

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "x@y")
    git(repo, "config", "user.name", "x")
    (repo / "bug.py").write_text("VALUE = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    task = SWEbenchTask("owner__repo-1", "example/image:latest", git(repo, "rev-parse", "HEAD"), "解释现有代码")
    captures = tmp_path / "captures"
    captures.mkdir()
    monkeypatch.setenv("PAIR_TEST_CAPTURE", str(captures))

    for enabled in (False, True):
        result = SWEbenchHarness(SourcePreparer(repo), Grader(), model_factory=finish_capture_factory).run(
            task, FinishCaptureModel(), ModelConfig(), tmp_path / str(enabled),
            runtime_config=RuntimeConfig(delegation_enabled=enabled))
        assert result.agent_status == "completed"
        assert result.agent_final == "无需修改"
    # 开启只增加模型可选能力；没有强制创建子任务，也没有改写原题或共同系统提示。
    captured = [ModelRequest.model_validate_json(p.read_text()) for p in sorted(captures.glob("*.json"), key=lambda p: p.stat().st_mtime_ns)]
    assert len(captured) == 2
    # 两组共享同一核心提示；委派段落只出现在实际提供委派工具的一组。
    from importlib.resources import files
    core = files("codeagent.prompts").joinpath("system.md").read_text(encoding="utf-8")
    delegation = files("codeagent.prompts").joinpath("system_delegation.md").read_text(encoding="utf-8")
    assert captured[0].messages[0]["content"].startswith(core) and captured[1].messages[0]["content"].startswith(core)
    assert delegation not in captured[0].messages[0]["content"] and delegation in captured[1].messages[0]["content"]
    for request in captured:
        from codeagent.benchmark.swebench_network import EVALUATION_RULES
        assert sum(m.get("content") == task.problem_statement + '\n\n' + EVALUATION_RULES for m in request.messages) == 1
    single_tools = {t.name for t in captured[0].tools}
    multi_tools = {t.name for t in captured[1].tools}
    assert not {"task", "spawn_agent", "send_agent", "wait_agents"} & single_tools
    assert "task" in multi_tools
    assert not {"spawn_agent", "send_agent", "wait_agents"} & multi_tools
    assert single_tools < multi_tools


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
    assert result.patch_present
    assert result.oracle_status == "completed" and result.oracle_passed is True
    assert "BROKEN = False" in grader.prediction["model_patch"]
    assert not (repo / "bug.py").read_text().startswith("BROKEN = False")
    saved = json.loads((Path(result.prediction_path).parent / "result.json").read_text())
    assert "validation_passed" not in saved and not hasattr(result, "validation_passed")
    assert saved["patch_sha256"] == result.patch_sha256
    assert saved["token_usage"]["coverage"] == "complete"
    assert saved["token_usage"]["coverage_ratio"] == "1"
    assert {key: saved["token_usage"][key] for key in (
        "requests", "requests_with_usage", "input_tokens", "output_tokens", "total_tokens",
        "cached_input_tokens", "cache_miss_input_tokens", "reasoning_tokens",
    )} == {
        "requests": 2, "requests_with_usage": 2, "input_tokens": 240, "output_tokens": 30,
        "total_tokens": 270, "cached_input_tokens": 150, "cache_miss_input_tokens": 90,
        "reasoning_tokens": None,
    }


def test_cli_grader_uses_fixed_argv_and_explicit_report(tmp_path):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        report_dir = Path(argv[argv.index("--report_dir") + 1])
        report_dir.mkdir(parents=True)
        (report_dir / "model.run-1.json").write_text(json.dumps({
            "resolved_ids": ["owner__repo-1"], "unresolved_ids": [],
            "empty_patch_ids": [], "infra_failure_ids": [],
            "ambiguous_failure_ids": [], "error_ids": [], "incomplete_ids": [],
        }))
        return subprocess.CompletedProcess(argv, 0, "ok", "")
    grader = SWEbenchCLIGrader(
        ["python", "-m", "swebench.harness.run_evaluation"],
        str(tmp_path / "{run_id}.json"), command_runner=run)
    prediction = tmp_path / "prediction.json"; prediction.write_text("{}")
    result = grader.grade(SWEbenchTask("owner__repo-1", "image", "a" * 40, "bug"), prediction, "run-1")
    assert result.status == "completed" and result.resolved is True
    assert calls[0][0][-8:] == [
        "--predictions_path", str(prediction), "--instance_ids", "owner__repo-1",
        "--run_id", "run-1", "--report_dir", str(tmp_path / "grader-report"),
    ]
    assert calls[0][1]["shell"] is False


def test_cli_grader_treats_empty_patch_as_completed_unresolved(tmp_path):
    def run(argv, **kwargs):
        report_dir = Path(argv[argv.index("--report_dir") + 1])
        report_dir.mkdir(parents=True)
        (report_dir / "codeagent__glm.run-empty.json").write_text(json.dumps({
            "resolved_ids": [], "unresolved_ids": [],
            "empty_patch_ids": ["owner__repo-1"], "infra_failure_ids": [],
            "ambiguous_failure_ids": [], "error_ids": [], "incomplete_ids": [],
        }))
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    grader = SWEbenchCLIGrader(
        ["python", "-m", "swebench.harness.run_evaluation"],
        str(tmp_path / "missing/{instance_id}/report.json"), command_runner=run,
    )
    prediction = tmp_path / "prediction.json"
    prediction.write_text("[]")
    result = grader.grade(
        SWEbenchTask("owner__repo-1", "image", "a" * 40, "bug"), prediction, "run-empty"
    )
    assert result.status == "completed"
    assert result.resolved is False
    assert result.detail == "official grader reported empty patch"
    assert result.report_path == str(tmp_path / "grader-report/codeagent__glm.run-empty.json")


def test_cli_grader_preserves_official_infrastructure_failure(tmp_path):
    def run(argv, **kwargs):
        report_dir = Path(argv[argv.index("--report_dir") + 1])
        report_dir.mkdir(parents=True)
        (report_dir / "codeagent__glm.run-infra.json").write_text(json.dumps({
            "resolved_ids": [], "unresolved_ids": [], "empty_patch_ids": [],
            "infra_failure_ids": ["owner__repo-1"], "ambiguous_failure_ids": [],
            "error_ids": [], "incomplete_ids": [],
        }))
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    grader = SWEbenchCLIGrader(
        ["python", "-m", "swebench.harness.run_evaluation"],
        str(tmp_path / "missing/{instance_id}/report.json"), command_runner=run,
    )
    prediction = tmp_path / "prediction.json"
    prediction.write_text("[]")
    result = grader.grade(
        SWEbenchTask("owner__repo-1", "image", "a" * 40, "bug"), prediction, "run-infra"
    )
    assert result.status == "environment_failed"
    assert result.resolved is None


def test_fixed_benchmark_budget_still_terminates_and_exports_patch(tmp_path):
    from codeagent.config import RuntimeConfig
    from codeagent.benchmark.limits import ExperimentLimits
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "x@y")
    git(repo, "config", "user.name", "x")
    path = repo / "bug.py"
    path.write_text("BROKEN = True\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    result = SWEbenchHarness(SourcePreparer(repo), Grader()).run(
        SWEbenchTask("owner__repo-1", "example/image:latest", git(repo, "rev-parse", "HEAD"), "fix"),
        EditModel(hashlib.sha256(path.read_bytes()).hexdigest()), ModelConfig(), tmp_path / "runs",
        runtime_config=RuntimeConfig(), experiment_limits=ExperimentLimits(max_requests=1),
    )
    assert result.agent_status == "experiment_request_limit"
    assert result.patch_present and result.oracle_passed
    events = [json.loads(line) for file in (tmp_path / "runs").rglob("events.jsonl") for line in file.read_text().splitlines()]
    assert any(e["type"] == "turn_terminated" and e["payload"]["reason"] == "experiment_request_limit" for e in events)
    assert not any(e["type"] == "turn_budget_increased" for e in events)


class GraphEditModel(BaseModelClient):
    def complete(self, request):
        names = {tool.name for tool in request.tools}
        # 策略标记来自本测试的原题，通用系统提示在两种策略中相同。
        expected = any(m['role'] == 'user' and '自主委派' in (m.get('content') or '') for m in request.messages)
        assert ('task' in names) is expected
        if not any(m['role'] == 'tool' for m in request.messages):
            return LLMResponse(tool_calls=[LLMToolCall(call_id='edit', name='apply_workspace_edit', arguments={
                'operations': [{'op': 'replace_text', 'path': 'bug.py',
                                'expected_sha256': hashlib.sha256(b'BROKEN = True\n').hexdigest(),
                                'old_text': 'BROKEN = True', 'new_text': 'BROKEN = False'}]
            })], finish_reason='tool_calls', usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15))
        return LLMResponse(text='done', finish_reason='stop',
                           usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15))


def graph_edit_factory(config):
    assert config.provider == 'fake'
    return GraphEditModel()


def test_swe_graph_entry_preserves_edit_export_and_single_tool_mask(tmp_path):
    from codeagent.config import RuntimeConfig
    for delegated in (False, True):
        folder = tmp_path / str(delegated); folder.mkdir()
        repo = folder / 'repo'; repo.mkdir()
        git(repo, 'init'); git(repo, 'config', 'user.email', 'x@y'); git(repo, 'config', 'user.name', 'x')
        (repo / 'bug.py').write_text('BROKEN = True\n')
        git(repo, 'add', '.'); git(repo, 'commit', '-m', 'base')
        result = SWEbenchHarness(SourcePreparer(repo), Grader(), model_factory=graph_edit_factory).run(
            SWEbenchTask('owner__repo-1', 'example/image:latest', git(repo, 'rev-parse', 'HEAD'),
                         '修复 bug；自主委派可选' if delegated else '修复 bug'),
            GraphEditModel(), ModelConfig(), folder / 'runs',
            runtime_config=RuntimeConfig(delegation_enabled=delegated))
        assert result.agent_status == 'completed'
        assert result.patch_present and result.oracle_passed
        assert result.token_usage['requests'] == 2
        assert (repo / 'bug.py').read_text() == 'BROKEN = True\n'


class InvestigateUntilStoppedModel(BaseModelClient):
    def complete(self, request):
        return LLMResponse(tool_calls=[LLMToolCall(call_id='read', name='list_dir', arguments={})],
                           finish_reason='tool_calls', usage=TokenUsage(
                               input_tokens=10, cached_input_tokens=8, output_tokens=2, total_tokens=12))


def investigate_until_stopped_factory(config):
    return InvestigateUntilStoppedModel()


def test_harness_request_limit_preserves_complete_usage_of_actual_dispatches(tmp_path):
    from codeagent.benchmark.limits import ExperimentLimits

    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init')
    git(repo, 'config', 'user.email', 'x@y')
    git(repo, 'config', 'user.name', 'x')
    (repo / 'bug.py').write_text('BROKEN = True\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-m', 'base')
    task = SWEbenchTask('owner__repo-1', 'example/image:latest', git(repo, 'rev-parse', 'HEAD'), '调查代码')
    result = SWEbenchHarness(SourcePreparer(repo), Grader(), model_factory=investigate_until_stopped_factory).run(
        task, InvestigateUntilStoppedModel(), ModelConfig(), tmp_path / 'runs',
        experiment_limits=ExperimentLimits(max_requests=1))
    assert result.agent_status == 'experiment_request_limit'
    assert result.token_usage['requests'] == result.token_usage['requests_with_usage'] == 1
    assert result.token_usage['coverage'] == 'complete'
    assert result.token_usage['cache_miss_input_tokens'] == 2
    saved = json.loads((Path(result.prediction_path).parent / 'result.json').read_text())
    assert saved['token_usage'] == result.token_usage
    controller = json.loads((Path(result.prediction_path).parent / 'experiment-limits.json').read_text())
    assert len(controller['requests']) == 1 and controller['requests'][0]['status'] == 'completed'
