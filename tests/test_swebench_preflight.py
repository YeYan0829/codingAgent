import json
import subprocess
from pathlib import Path

from codeagent.benchmark import SWEbenchGoldPreflight, SWEbenchTaskRepository


def task_repo(tmp_path: Path) -> Path:
    root = tmp_path / "tasks-repo"
    task = root / "tasks" / "owner__repo-1"
    task.mkdir(parents=True)
    (task / "task.yaml").write_text(
        "base_commit: " + "a" * 40 + "\n"
        "environment_setup_commit: " + "b" * 40 + "\n"
        "eval_type: pass_and_fail\n"
        "image: example/eval:latest\n"
        "instance_id: owner__repo-1\n"
        "log_parser: parse_log_pytest\n"
        "repo: owner/repo\n"
        "split: test\n"
        "version: '1.0'\n",
        encoding="utf-8",
    )
    for name, value in {
        "problem_statement.md": "fix it\n", "gold.patch": "gold\n",
        "test.patch": "test\n", "eval.sh": "pytest\n",
    }.items():
        (task / name).write_text(value, encoding="utf-8")
    (task / "tests.json").write_text(
        json.dumps({"FAIL_TO_PASS": ["test_new"], "PASS_TO_PASS": ["test_old"]}),
        encoding="utf-8",
    )
    return root


def test_task_repo_loads_agent_input_and_exports_official_dataset(tmp_path):
    repository = SWEbenchTaskRepository(task_repo(tmp_path))
    loaded = repository.load("owner__repo-1")
    assert loaded.task.problem_statement == "fix it\n"
    assert loaded.task.image == "example/eval:latest"
    assert loaded.dataset_record["patch"] == "gold\n"
    path = repository.export_dataset(["owner__repo-1"], tmp_path / "dataset.json")
    assert json.loads(path.read_text())[0]["PASS_TO_PASS"] == ["test_old"]


def test_gold_preflight_classifies_each_official_report_state(tmp_path):
    repository = SWEbenchTaskRepository(task_repo(tmp_path))
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        run_id = argv[argv.index("--run_id") + 1]
        report_dir = Path(argv[argv.index("--report_dir") + 1])
        report_dir.mkdir(parents=True)
        (report_dir / f"gold.{run_id}.json").write_text(json.dumps({
            "resolved_ids": ["owner__repo-1"], "unresolved_ids": [],
            "infra_failure_ids": [], "error_ids": [], "incomplete_ids": [],
        }))
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    result = SWEbenchGoldPreflight(repository, ["python", "-m", "grader"], command_runner=run).run(
        ["owner__repo-1"], tmp_path / "runs"
    )
    assert result.tasks[0].status == "gold_passed"
    assert "gold" == calls[0][calls[0].index("--predictions_path") + 1]
    assert Path(result.dataset_path).is_file()


def test_gold_preflight_turns_process_failure_into_environment_state(tmp_path):
    repository = SWEbenchTaskRepository(task_repo(tmp_path))
    failed = lambda argv, **kwargs: subprocess.CompletedProcess(argv, 2, "", "docker unavailable")
    result = SWEbenchGoldPreflight(repository, ["grader"], command_runner=failed).run(
        ["owner__repo-1"], tmp_path / "runs"
    )
    assert result.tasks[0].status == "environment_failed"
    assert result.tasks[0].resolved is None
