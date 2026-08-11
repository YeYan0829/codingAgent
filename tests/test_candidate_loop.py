import hashlib
import json
import subprocess

import pytest
from typer.testing import CliRunner

from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.candidate import ApplyStatus, CandidateError, CandidateService
from codeagent.runtime.command_service import CommandService
from codeagent.runtime.edit_service import TextPatchService
from codeagent.runtime.local_executor import LocalCommandExecutor
from codeagent.runtime.policy import CommandPolicy
from codeagent.runtime.runner import AgentRunner
from codeagent.config import RuntimeConfig
from codeagent.model_gateway.base import BaseModelClient, LLMResponse, LLMToolCall, ModelRequest
from codeagent.session.store import SessionStore
from codeagent.tools.fs_read import build_fs_tools
from codeagent.tools.registry import ToolRegistry
from codeagent.workspace.git_worktree import GitWorktreeManager, WorkspaceState
from codeagent.workspace.workspace import WorkspaceContext
from codeagent.cli import app


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


def execution_session(tmp_path):
    repo = make_bug_repo(tmp_path)
    session_root = tmp_path / "sessions"
    store = SessionStore(repo, session_root=session_root)
    context = GitWorktreeManager(session_root).create(repo, store.session_id)
    store.create(mode="execution", workspace_context=context)
    return repo, store, context


def command_service(store, context):
    return CommandService(
        context=context,
        policy=CommandPolicy(),
        approval_gate=AutoApprovalGate(allow=True),
        executor=LocalCommandExecutor(),
        session_store=store,
        artifact_store=CommandArtifactStore(store),
    )


def create_passing_candidate(tmp_path):
    repo, store, context = execution_session(tmp_path)
    commands = command_service(store, context)
    baseline = commands.run_check({"kind": "pytest", "targets": ["test_sort_utils.py"], "timeout_seconds": 30})
    assert not baseline.ok
    edit = TextPatchService(context, store).apply_text_patch(
        {"path": "sort_utils.py", "old_text": "sorted(values, reverse=True)", "new_text": "sorted(values)"}
    )
    assert edit.ok
    passing = commands.run_check({"kind": "pytest", "targets": ["test_sort_utils.py"], "timeout_seconds": 30})
    assert passing.ok
    manifest = CandidateService(context, store).freeze()
    return repo, store, context, manifest


def test_minimal_complete_candidate_loop_applies_verified_patch(tmp_path):
    repo, store, context, manifest = create_passing_candidate(tmp_path)
    service = CandidateService(context, store)
    shown = {}

    def approve(candidate, patch):
        shown["hash"] = hashlib.sha256(patch.encode("utf-8")).hexdigest()
        shown["files"] = candidate["changed_files"]
        return True

    receipt = service.apply(manifest["candidate_id"], approve)

    assert receipt["status"] == ApplyStatus.APPLIED.value
    assert receipt["applied_patch_sha256"] == manifest["patch_sha256"]
    assert receipt["final_source_dirty"] is True
    assert shown == {"hash": manifest["patch_sha256"], "files": ["sort_utils.py"]}
    assert "sorted(values)" in (repo / "sort_utils.py").read_text(encoding="utf-8")
    assert git(repo, "diff", "--", "sort_utils.py")
    assert json.loads((store.session_dir / "artifacts" / "applies" / f"{receipt['apply_id']}.json").read_text(encoding="utf-8"))["patch_sha256"] == manifest["patch_sha256"]


class CandidateLoopModel(BaseModelClient):
    def __init__(self):
        self.step = 0

    def complete(self, request: ModelRequest) -> LLMResponse:
        assert {"read_file", "run_check", "apply_text_patch", "freeze_candidate"} <= {tool.name for tool in request.tools}
        assert all(not hasattr(tool, "handler") for tool in request.tools)
        actions = [
            ("read_file", {"path": "sort_utils.py"}),
            ("run_check", {"kind": "pytest", "targets": ["test_sort_utils.py"], "timeout_seconds": 30}),
            ("apply_text_patch", {"path": "sort_utils.py", "old_text": "sorted(values, reverse=True)", "new_text": "sorted(values)"}),
            ("run_check", {"kind": "pytest", "targets": ["test_sort_utils.py"], "timeout_seconds": 30}),
            ("freeze_candidate", {}),
        ]
        if self.step >= len(actions):
            return LLMResponse(text="candidate ready")
        name, arguments = actions[self.step]
        self.step += 1
        return LLMResponse(tool_calls=[LLMToolCall(call_id=f"loop-{self.step}", name=name, arguments=arguments)])


def test_deterministic_model_completes_read_edit_test_freeze_loop(tmp_path):
    repo, store, context = execution_session(tmp_path)
    registry = ToolRegistry(workspace_context=context)
    for tool in build_fs_tools(context):
        registry.register(tool)
    runner = AgentRunner(
        store,
        CandidateLoopModel(),
        registry,
        AutoApprovalGate(allow=True),
        workspace_context=context,
        command_executor=LocalCommandExecutor(),
        command_artifact_store=CommandArtifactStore(store),
    )
    output = runner.run_turn("修复排序 bug 并交付 candidate")
    assert output.final_text == "candidate ready"
    assert [step["tool"] for step in output.steps] == [
        "read_file", "run_check", "apply_text_patch", "run_check", "freeze_candidate"
    ]
    manifest, _ = CandidateService(context, store).load()
    assert manifest["changed_files"] == ["sort_utils.py"]
    assert "reverse=True" in (repo / "sort_utils.py").read_text(encoding="utf-8")


def test_text_patch_is_worktree_only_and_journaled(tmp_path):
    repo, store, context = execution_session(tmp_path)
    source_before = (repo / "sort_utils.py").read_bytes()
    result = TextPatchService(context, store).apply_text_patch(
        {"path": "sort_utils.py", "old_text": "reverse=True", "new_text": "reverse=False"}
    )
    assert result.ok
    assert (repo / "sort_utils.py").read_bytes() == source_before
    journal = json.loads(open(result.metadata["journal"], encoding="utf-8").read())
    assert journal["before_sha256"] == hashlib.sha256(source_before).hexdigest()
    assert journal["after_sha256"] == result.metadata["after_sha256"]
    assert "reverse=True" in journal["patch"]

    readonly = WorkspaceContext.source(repo)
    rejected = TextPatchService(readonly, store).apply_text_patch(
        {"path": "sort_utils.py", "old_text": "reverse=True", "new_text": "reverse=False"}
    )
    assert not rejected.ok
    assert "不能直接写 source" in rejected.error


def test_text_patch_matches_lf_context_against_crlf_file_and_preserves_crlf(tmp_path):
    _, store, context = execution_session(tmp_path)
    target = context.active_root / "crlf.py"
    target.write_bytes(b"def value():\r\n    return 1\r\n")
    result = TextPatchService(context, store).apply_text_patch(
        {
            "path": "crlf.py",
            "old_text": "def value():\n    return 1",
            "new_text": "def value():\n    return 2",
        }
    )
    assert result.ok
    assert target.read_bytes() == b"def value():\r\n    return 2\r\n"


@pytest.mark.parametrize(
    ("path", "old_text", "new_text", "message"),
    [
        ("../escape.py", "", "x", "父目录跳转"),
        (".env", "", "TOKEN=x", "sensitive"),
        ("sort_utils.py", "missing context", "x", "实际 0 次"),
    ],
)
def test_text_patch_rejects_unsafe_or_nonmatching_input(tmp_path, path, old_text, new_text, message):
    _, store, context = execution_session(tmp_path)
    result = TextPatchService(context, store).apply_text_patch(
        {"path": path, "old_text": old_text, "new_text": new_text}
    )
    assert not result.ok
    assert message in result.error


def test_text_patch_rejects_binary_large_and_symlink(tmp_path):
    _, store, context = execution_session(tmp_path)
    (context.active_root / "binary.bin").write_bytes(b"a\0b")
    service = TextPatchService(context, store)
    assert not service.apply_text_patch({"path": "binary.bin", "old_text": "a", "new_text": "b"}).ok
    assert not service.apply_text_patch({"path": "large.txt", "old_text": "", "new_text": "x" * 70_000}).ok

    link = context.active_root / "linked.py"
    try:
        link.symlink_to(context.active_root / "sort_utils.py")
    except OSError:
        pytest.skip("当前环境不能创建 symlink")
    assert not service.apply_text_patch({"path": "linked.py", "old_text": "reverse=True", "new_text": "reverse=False"}).ok


def test_dirty_candidate_worktree_can_resume_and_run_check(tmp_path):
    _, store, context = execution_session(tmp_path)
    TextPatchService(context, store).apply(
        path="sort_utils.py", old_text="reverse=True", new_text="reverse=False"
    )
    report = GitWorktreeManager(store.session_root).inspect(context, candidate_changes=True)
    assert report.state == WorkspaceState.CANDIDATE_CHANGES
    assert report.executable
    assert command_service(store, context).run_check(
        {"kind": "pytest", "targets": ["test_sort_utils.py"], "timeout_seconds": 30}
    ).ok


def test_command_audit_distinguishes_changes_to_already_dirty_candidate_file(tmp_path):
    _, store, context = execution_session(tmp_path)
    TextPatchService(context, store).apply(
        path="sort_utils.py", old_text="reverse=True", new_text="reverse=False"
    )
    result = command_service(store, context).run_check(
        {"kind": "pytest", "targets": ["test_side_effect.py"], "timeout_seconds": 30}
    )
    assert result.ok
    receipt = [event.payload for event in store.read_events() if event.type == "command_receipt"][-1]
    audit = receipt["result"]
    assert audit["preexisting_changed_files"] == ["sort_utils.py"]
    assert audit["command_introduced_changes"] == ["sort_utils.py"]
    assert audit["git_fingerprints_before"]["sort_utils.py"] != audit["git_fingerprints_after"]["sort_utils.py"]


def test_candidate_is_frozen_against_later_worktree_changes(tmp_path):
    _, store, context, manifest = create_passing_candidate(tmp_path)
    service = CandidateService(context, store)
    _, original = service.load(manifest["candidate_id"])
    (context.active_root / "sort_utils.py").write_text("later change\n", encoding="utf-8")
    loaded, frozen = service.load(manifest["candidate_id"])
    assert frozen == original
    assert hashlib.sha256(frozen).hexdigest() == loaded["patch_sha256"]
    assert loaded["workspace_side_effects"] == []


def test_candidate_patch_artifact_tampering_is_rejected(tmp_path):
    _, store, context, manifest = create_passing_candidate(tmp_path)
    patch_path = store.session_dir / "artifacts" / "candidates" / manifest["candidate_id"] / "candidate.patch"
    patch_path.write_bytes(patch_path.read_bytes() + b"# tampered\n")
    with pytest.raises(CandidateError, match="hash"):
        CandidateService(context, store).load(manifest["candidate_id"])


def test_candidate_supports_new_utf8_text_file(tmp_path):
    repo, store, context = execution_session(tmp_path)
    result = TextPatchService(context, store).apply_text_patch(
        {"path": "notes 文档.txt", "old_text": "", "new_text": "候选说明\n"}
    )
    assert result.ok
    assert command_service(store, context).run_check(
        {"kind": "pytest", "targets": ["test_sort_utils.py"], "timeout_seconds": 30}
    ).ok is False
    # 修复原有 bug 后再次测试，使冻结证据明确位于最后一次编辑之后。
    TextPatchService(context, store).apply(
        path="sort_utils.py", old_text="reverse=True", new_text="reverse=False"
    )
    assert command_service(store, context).run_check(
        {"kind": "pytest", "targets": ["test_sort_utils.py"], "timeout_seconds": 30}
    ).ok
    manifest = CandidateService(context, store).freeze()
    assert manifest["changed_files"] == ["notes 文档.txt", "sort_utils.py"]
    receipt = CandidateService(context, store).apply(manifest["candidate_id"], lambda *_: True)
    assert receipt["status"] == ApplyStatus.APPLIED.value
    assert (repo / "notes 文档.txt").read_text(encoding="utf-8") == "候选说明\n"


def test_reject_apply_keeps_source_unchanged(tmp_path):
    repo, store, context, manifest = create_passing_candidate(tmp_path)
    before = (repo / "sort_utils.py").read_bytes()
    receipt = CandidateService(context, store).apply(manifest["candidate_id"], lambda _candidate, _patch: False)
    assert receipt["status"] == ApplyStatus.REJECTED.value
    assert (repo / "sort_utils.py").read_bytes() == before
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == ""


def test_apply_rejects_dirty_source_and_changed_head(tmp_path):
    repo, store, context, manifest = create_passing_candidate(tmp_path)
    source_file = repo / "sort_utils.py"
    source_file.write_text(source_file.read_text(encoding="utf-8") + "# user\n", encoding="utf-8")
    dirty = CandidateService(context, store).apply(manifest["candidate_id"], lambda *_: True)
    assert dirty["status"] == ApplyStatus.SOURCE_DIRTY.value
    git(repo, "restore", "sort_utils.py")
    (repo / "other.txt").write_text("new head\n", encoding="utf-8")
    git(repo, "add", "other.txt")
    git(repo, "commit", "-m", "move head")
    changed = CandidateService(context, store).apply(manifest["candidate_id"], lambda *_: True)
    assert changed["status"] == ApplyStatus.SOURCE_CHANGED.value
    assert "sorted(values, reverse=True)" in source_file.read_text(encoding="utf-8")


def test_apply_rechecks_source_after_approval(tmp_path):
    repo, store, context, manifest = create_passing_candidate(tmp_path)
    source_file = repo / "sort_utils.py"

    def mutate_during_approval(*_):
        source_file.write_text(source_file.read_text(encoding="utf-8") + "# concurrent user edit\n", encoding="utf-8")
        return True

    receipt = CandidateService(context, store).apply(manifest["candidate_id"], mutate_during_approval)
    assert receipt["status"] == ApplyStatus.SOURCE_DIRTY.value
    assert "sorted(values, reverse=True)" in source_file.read_text(encoding="utf-8")
    assert "concurrent user edit" in source_file.read_text(encoding="utf-8")


def test_candidate_requires_successful_test_after_latest_edit(tmp_path):
    _, store, context = execution_session(tmp_path)
    commands = command_service(store, context)
    commands.run_check({"kind": "pytest", "targets": ["test_sort_utils.py"], "timeout_seconds": 30})
    TextPatchService(context, store).apply(
        path="sort_utils.py", old_text="reverse=True", new_text="reverse=False"
    )
    with pytest.raises(CandidateError, match="编辑后没有成功"):
        CandidateService(context, store).freeze()


def test_readonly_registry_does_not_expose_write_tools(tmp_path):
    context = WorkspaceContext.source(tmp_path)
    names = {tool.name for tool in build_fs_tools(context)}
    assert "apply_text_patch" not in names
    assert "freeze_candidate" not in names


class EightToolStepsThenFinalModel(BaseModelClient):
    def __init__(self):
        self.calls = 0

    def complete(self, request: ModelRequest) -> LLMResponse:
        self.calls += 1
        if self.calls <= 8:
            return LLMResponse(
                tool_calls=[LLMToolCall(call_id=f"eight-{self.calls}", name="list_dir", arguments={"path": "."})]
            )
        return LLMResponse(text="八步工具调用后的最终总结")


def test_default_step_budget_allows_eight_tool_steps_plus_final_response(tmp_path):
    workspace = tmp_path / "readonly"
    workspace.mkdir()
    store = SessionStore(workspace, session_root=tmp_path / "sessions").create()
    context = WorkspaceContext.source(workspace)
    registry = ToolRegistry(workspace_context=context)
    for tool in build_fs_tools(context):
        registry.register(tool)
    output = AgentRunner(
        store,
        EightToolStepsThenFinalModel(),
        registry,
        AutoApprovalGate(allow=False),
        config=RuntimeConfig(),
    ).run_turn("执行长链路")
    assert output.final_text == "八步工具调用后的最终总结"
    assert len(output.steps) == 8


def test_candidate_cli_shows_diff_and_noninteractive_apply_denies(tmp_path):
    repo, store, _, manifest = create_passing_candidate(tmp_path)
    runner = CliRunner()
    shown = runner.invoke(app, ["show-candidate", manifest["candidate_id"], "--session-root", str(store.session_root)])
    assert shown.exit_code == 0
    assert manifest["patch_sha256"] in shown.output
    assert "sort_utils.py" in shown.output
    before = (repo / "sort_utils.py").read_bytes()
    denied = runner.invoke(app, ["apply-candidate", manifest["candidate_id"], "--session-root", str(store.session_root)])
    assert denied.exit_code == 1
    assert '"status": "rejected"' in denied.output
    assert (repo / "sort_utils.py").read_bytes() == before


def test_resume_allows_journaled_candidate_changes(tmp_path):
    _, store, context = execution_session(tmp_path)
    TextPatchService(context, store).apply(
        path="sort_utils.py", old_text="reverse=True", new_text="reverse=False"
    )
    result = CliRunner().invoke(
        app, ["resume", store.session_id, "--session-root", str(store.session_root)], input="/exit\n"
    )
    assert result.exit_code == 0
    assert "run_check disabled" not in result.output
    assert store.read_meta()["workspace_state"] == WorkspaceState.CANDIDATE_CHANGES.value
