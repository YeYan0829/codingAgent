import json
import subprocess
import sys

import pytest

from codeagent.runtime.approval import FakeApprovalGate
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.command import ApprovalDecision, CommandExecutionStatus, CommandSpec
from codeagent.runtime.command_service import CommandService
from codeagent.runtime.local_executor import LocalCommandExecutor
from codeagent.runtime.local_executor import _terminate_process_tree
from codeagent.runtime.policy import CommandPolicy
from codeagent.session.store import SessionStore
from codeagent.workspace.git_worktree import GitWorktreeManager


def git(cwd, *args):
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def execution_case(tmp_path, files, *, approval=ApprovalDecision.APPROVE_ONCE):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "tests@example.com")
    git(repo, "config", "user.name", "CodeAgent Tests")
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "fixture")
    context = GitWorktreeManager(tmp_path / "worktrees").create(repo, "task")
    store = SessionStore(repo, session_root=tmp_path / "sessions").create(workspace_context=context)
    gate = FakeApprovalGate(approval)
    artifacts = CommandArtifactStore(store)
    service = CommandService(context, CommandPolicy(), gate, LocalCommandExecutor(), store, artifacts)
    return service, store, gate, context, artifacts


def last_receipt(store):
    return [event.payload for event in store.read_events() if event.type == "command_receipt"][-1]


def run(service, target, timeout=30, kind="pytest"):
    return service.run_check({"kind": kind, "targets": [target], "cwd": ".", "timeout_seconds": timeout})


class RecordingLocalCommandExecutor(LocalCommandExecutor):
    def __init__(self):
        self.specs = []

    def execute(self, spec, workspace_context, artifacts):
        self.specs.append(spec)
        return super().execute(spec, workspace_context, artifacts)


def test_real_pytest_success_and_artifacts_outside_worktree(tmp_path):
    service, store, gate, context, artifacts = execution_case(tmp_path, {"test_ok.py": "def test_ok():\n    assert True\n"})
    executor = RecordingLocalCommandExecutor()
    service.executor = executor
    result = run(service, "test_ok.py")
    saved = last_receipt(store)
    command_dir = artifacts.root / saved["command_spec"]["command_id"]
    assert result.ok and saved["status"] == "completed" and saved["result"]["exit_code"] == 0
    assert gate.command_requests[0].argv == tuple(saved["command_spec"]["argv"])
    assert gate.command_requests[0] is executor.specs[0]
    assert command_dir.parent.parent.parent == store.session_dir
    assert context.active_root not in command_dir.parents
    assert {"request.json", "result.json", "stdout.log", "stderr.log"} <= {p.name for p in command_dir.iterdir()}
    runtime_dir = artifacts.runtime_root / saved["command_spec"]["command_id"][:12]
    assert runtime_dir.is_dir()
    assert command_dir not in runtime_dir.parents
    assert {"home", "tmp"} == {path.name for path in runtime_dir.iterdir()}


def test_runtime_temp_keeps_path_budget_for_nested_directories(tmp_path):
    code = (
        "import tempfile\n"
        "from pathlib import Path\n"
        "def test_nested_temp():\n"
        "    target = Path(tempfile.gettempdir()) / ('a' * 40) / ('b' * 40)\n"
        "    target.mkdir(parents=True)\n"
        "    assert target.is_dir()\n"
    )
    service, store, _, _, artifacts = execution_case(tmp_path, {"test_nested_temp.py": code})
    result = run(service, "test_nested_temp.py")
    saved = last_receipt(store)
    runtime_dir = artifacts.runtime_root / saved["command_spec"]["command_id"][:12]
    assert result.ok
    assert (runtime_dir / "tmp" / ("a" * 40) / ("b" * 40)).is_dir()


def test_real_pytest_failure_is_completed(tmp_path):
    service, store, _, _, _ = execution_case(tmp_path, {"test_fail.py": "def test_fail():\n    assert 1 == 2\n"})
    result = run(service, "test_fail.py")
    saved = last_receipt(store)
    assert not result.ok and saved["status"] == "completed" and saved["result"]["exit_code"] != 0


def test_spawn_failure_has_explicit_status(tmp_path, monkeypatch):
    service, store, _, _, artifacts = execution_case(tmp_path, {"test_ok.py": "def test_ok(): assert True\n"})
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("synthetic spawn failure")))
    result = run(service, "test_ok.py")
    saved = last_receipt(store)
    assert result.metadata["status"] == "spawn_failed"
    assert saved["result"]["exit_code"] is None
    assert saved["result"]["spawn_error"] == "synthetic spawn failure"
    assert (artifacts.root / saved["command_spec"]["command_id"] / "result.json").exists()


def test_timeout_has_explicit_status(tmp_path):
    service, store, _, _, _ = execution_case(tmp_path, {"test_slow.py": "import time\ndef test_slow():\n    time.sleep(10)\n"})
    result = run(service, "test_slow.py", timeout=1)
    saved = last_receipt(store)
    assert result.metadata["status"] == "timed_out" and saved["result"]["timed_out"] is True
    assert saved["result"]["started_at"] and saved["result"]["finished_at"]


def test_large_output_is_complete_on_disk_and_summary_keeps_tail(tmp_path):
    body = "A" * 7000 + "TAIL-SENTINEL"
    service, store, _, _, artifacts = execution_case(
        tmp_path, {"test_output.py": f"def test_output():\n    print({body!r})\n    assert False\n"}
    )
    result = run(service, "test_output.py")
    saved = last_receipt(store)
    full = (artifacts.root / saved["command_spec"]["command_id"] / "stdout.log").read_text(encoding="utf-8")
    assert len(full) > len(result.content) and "TAIL-SENTINEL" in full and "TAIL-SENTINEL" in result.content
    assert result.truncated and saved["result"]["stdout_truncated"] is True


def test_executor_rechecks_cwd_boundary(tmp_path):
    _, _, _, context, artifacts = execution_case(tmp_path, {"test_ok.py": "def test_ok(): assert True\n"})
    spec = CommandSpec("outside", "pytest", (sys.executable, "-m", "pytest", "test_ok.py"), "..", 30)
    result = LocalCommandExecutor().execute(spec, context, artifacts.prepare(spec))
    assert result.status == CommandExecutionStatus.EXECUTOR_REJECTED and result.exit_code is None


def test_shell_false_and_stdin_devnull(tmp_path, monkeypatch):
    service, _, gate, _, _ = execution_case(tmp_path, {"test_stdin.py": "def test_stdin():\n    assert True\n"})
    real_popen = subprocess.Popen
    observed = {}
    def spy(*args, **kwargs):
        if args[0] and args[0][0] == sys.executable:
            observed.update(kwargs)
            observed["argv"] = args[0]
        return real_popen(*args, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", spy)
    assert run(service, "test_stdin.py").ok
    assert observed["shell"] is False and observed["stdin"] is subprocess.DEVNULL
    assert tuple(observed["argv"]) == gate.command_requests[0].argv


def test_complete_stdout_and_stderr_are_written(tmp_path):
    files = {
        "test_ok.py": "def test_ok():\n    assert True\n",
        "conftest.py": (
            "import os\n"
            "def pytest_unconfigure(config):\n"
            "    os.write(1, b'STDOUT-SENTINEL\\n')\n"
            "    os.write(2, b'STDERR-SENTINEL\\n')\n"
        ),
    }
    service, store, _, _, artifacts = execution_case(tmp_path, files)
    assert run(service, "test_ok.py").ok
    saved = last_receipt(store)
    command_dir = artifacts.root / saved["command_spec"]["command_id"]
    assert "STDOUT-SENTINEL" in (command_dir / "stdout.log").read_text(encoding="utf-8")
    assert "STDERR-SENTINEL" in (command_dir / "stderr.log").read_text(encoding="utf-8")


def test_sensitive_environment_is_not_forwarded(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-leak")
    code = "import os\ndef test_env():\n    assert 'OPENAI_API_KEY' not in os.environ\n    assert 'GITHUB_TOKEN' not in os.environ\n"
    service, store, _, _, _ = execution_case(tmp_path, {"test_env.py": code})
    assert run(service, "test_env.py").ok
    names = last_receipt(store)["result"]["environment_names"]
    assert "OPENAI_API_KEY" not in names and "GITHUB_TOKEN" not in names and "HOME" in names


@pytest.mark.parametrize(("kind", "approval", "expected"), [
    ("ruff", ApprovalDecision.APPROVE_ONCE, "policy_denied"),
    ("pytest", ApprovalDecision.DENY, "approval_denied"),
])
def test_denials_write_receipt_and_artifact(tmp_path, kind, approval, expected):
    service, store, _, _, artifacts = execution_case(tmp_path, {"test_ok.py": "def test_ok(): assert True\n"}, approval=approval)
    run(service, "test_ok.py", kind=kind)
    saved = last_receipt(store)
    command_dir = artifacts.root / saved["command_spec"]["command_id"]
    assert saved["status"] == expected
    assert json.loads((command_dir / "result.json").read_text(encoding="utf-8"))["status"] == expected


@pytest.mark.skipif(sys.platform != "win32", reason="Windows taskkill path")
def test_windows_taskkill_uses_trusted_absolute_path_and_no_shell(monkeypatch):
    observed = {}
    class Process:
        pid = 123
        def poll(self): return 0
        def kill(self): raise AssertionError("kill should not be needed")
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    def fake_run(argv, **kwargs):
        observed["argv"] = argv
        observed.update(kwargs)
    monkeypatch.setattr(subprocess, "run", fake_run)
    _terminate_process_tree(Process())
    assert observed["argv"][0] == r"C:\Windows\System32\taskkill.exe"
    assert observed["shell"] is False
