from pathlib import Path


ROOT = Path(__file__).parents[1]
TASKS = ROOT / "examples" / "eval-tasks" / "tasks"


def test_eval_task_templates_use_generated_absolute_python_path():
    for path in sorted(TASKS.glob("*.md")):
        content = path.read_text(encoding="utf-8")
        assert "{{EVAL_PYTHON}}" in content
        assert "~/.cache/codeagent-evals" not in content


def test_prepare_generates_workspace_task_from_template():
    content = (ROOT / "examples" / "eval-tasks" / "prepare.sh").read_text(encoding="utf-8")
    assert 'task_output="$workspace/CODEAGENT_TASK.md"' in content
    assert "{{EVAL_PYTHON}}" in content
    assert 'task:      $task_output' in content
