"""免费Runtime测试共用临时仓库；fixture backend仅用于单元测试，生产无宿主回退。"""
import hashlib
import json
import stat
import subprocess
import sys
from pathlib import Path
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.atomic_edit import AtomicEditService
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.command_service import CommandService
from codeagent.runtime.sandbox_executor import SandboxedCommandExecutor
from codeagent.runtime.bubblewrap import BubblewrapFeatures
from codeagent.session.store import SessionStore
from codeagent.workspace.workspace import WorkspaceContext


def git(cwd, *args, check=True):
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=False)
    if check:
        assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def make_bug_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "tests@example.com")
    git(repo, "config", "user.name", "CodeAgent Tests")
    (repo / "sort_utils.py").write_text(
        "def sort_values(values):\n    return sorted(values, reverse=True)\n", encoding="utf-8"
    )
    (repo / "test_sort_utils.py").write_text(
        "from sort_utils import sort_values\n\ndef test_sort_values():\n    assert sort_values([3, 1, 2]) == [1, 2, 3]\n",
        encoding="utf-8",
    )
    (repo / "test_side_effect.py").write_text(
        "from pathlib import Path\n\ndef test_mutates_existing_dirty_file():\n"
        "    path = Path('sort_utils.py')\n"
        "    path.write_text(path.read_text(encoding='utf-8') + '# test side effect\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial bug")
    return repo


def command_service(store, context):
    service = CommandService(context, AutoApprovalGate(allow=True), SandboxedCommandExecutor(HostFixtureBackend()), store, CommandArtifactStore(store))

    class Commands:
        def run_check(self, arguments):
            targets = " ".join(arguments.get("targets", []))
            return service.run_command({"command": f"{sys.executable} -m pytest -p no:cacheprovider {targets}",
                                        "purpose": "validation", "timeout_seconds": arguments.get("timeout_seconds", 120)})

    return Commands()


class HostFixtureBackend:
    def probe(self, workspace_root):
        return BubblewrapFeatures(Path("/fixture/bwrap"), "fixture", False)

    def invocation(self, features, plan, policy, cwd, environment, payload):
        return ["/usr/bin/env", "-i", *(f"{name}={value}" for name, value in sorted(environment.items())), *payload]


def execution_session(tmp_path):
    repo = make_bug_repo(tmp_path)
    store = SessionStore(repo, session_root=tmp_path / "sessions").create()
    return repo, store, store.workspace_context()


def state(repo):
    return (git(repo, "rev-parse", "HEAD"), git(repo, "symbolic-ref", "HEAD"), (repo / ".git/index").read_bytes(),
            {str(p.relative_to(repo)): p.read_bytes() for p in repo.rglob("*")
             if p.is_file() and ".git" not in p.relative_to(repo).parts})


def dirty_session(tmp_path):
    repo = make_bug_repo(tmp_path)
    (repo / ".gitignore").write_text("ignored*\n__pycache__/\n.pytest_cache/\n")
    target = repo / "sort_utils.py"
    target.write_text("# heading\n" + target.read_text() + "\n" * 6 + "# end\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "spaced fixture")
    # 同一文件部分暂存；working tree 与 index 必须能独立还原。
    target.write_text(target.read_text().replace("# heading", "# staged user"))
    git(repo, "add", "sort_utils.py")
    target.write_text(target.read_text().replace("# end", "# unstaged user"))
    (repo / "notes.txt").write_text("initial untracked user\n")
    (repo / "ignored-local.txt").write_text("user ignored content\n")
    # staged-only blob 不在 S0 工作树中，也必须被引用保留。
    (repo / "staged-only.txt").write_text("index-only content\n")
    git(repo, "add", "staged-only.txt")
    (repo / "staged-only.txt").unlink()
    original = state(repo)
    store = SessionStore(repo, session_root=tmp_path / "sessions").create()
    context = store.workspace_context()
    assert state(repo) == original
    return repo, store, context


def edit_sort(store, context, old="sorted(values, reverse=True)", new="sorted(values)"):
    target = context.active_root / "sort_utils.py"
    result = AtomicEditService(context, store).apply_workspace_edit({"operations": [{
        "op": "replace_text", "path": "sort_utils.py",
        "expected_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "old_text": old, "new_text": new,
    }]})
    assert result.ok, result.error


def validate(store, context):
    result = command_service(store, context).run_check({
        "kind": "pytest", "targets": ["test_sort_utils.py"], "timeout_seconds": 30,
    })
    assert result.ok, result.error



def in_process_runner(store, model, **kwargs):
    """显式内存模型接缝；需要验证进程transport的测试使用可spawn的顶层factory。"""
    from codeagent.application.session_runtime import build_registry_for_context
    from codeagent.runtime.runner import AgentRunner
    kwargs['config'] = kwargs.pop('runtime_config', None)
    return AgentRunner(store, model, build_registry_for_context(store.workspace_context()), **kwargs)
