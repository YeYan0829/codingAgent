from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from codeagent.benchmark.swebench_executor import (
    DockerExecutionOutcome,
    SWEbenchDockerCommandExecutor,
)
from codeagent.benchmark.swebench_source import PreparedSWEbenchSource
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.command import CommandRequest, SandboxExecutionRequest
from codeagent.runtime.command_service import CommandService
from codeagent.runtime.permissions import EffectiveSandboxPolicy, NetworkMode
from runtime_fixtures import execution_session


DIGEST = "sha256:" + "b" * 64


@pytest.mark.parametrize('exit_code', [0, 7])
def test_long_command_streams_survive_backend_cleanup_and_can_be_reread(tmp_path, exit_code):
    from codeagent.tools.observations import build_observation_tool
    _, store, context = execution_session(tmp_path)
    stdout = '正文\n' * 30000 + 'stdout-end\n'
    stderr = 'diagnostic\n' * 10000 + 'stderr-end\n'
    class LongBackend(FakeDockerExecutionBackend):
        def execute(self, container_id, argv, stdout_path, stderr_path, timeout_seconds, output_limit_bytes, cancelled):
            result = super().execute(container_id, argv, stdout_path, stderr_path, timeout_seconds, output_limit_bytes, cancelled)
            stdout_path.write_text(stdout)
            stderr_path.write_text(stderr)
            return result
    backend = LongBackend(context.active_root, exit_code=exit_code)
    executor = SWEbenchDockerCommandExecutor(prepared(context), backend, import_probe=lambda *_: None)
    service = CommandService(context, AutoApprovalGate(False), executor, store, CommandArtifactStore(store))
    result = service.run_command({'command': 'fixture streams'})
    assert result.ok is (exit_code == 0)
    assert result.metadata['exit_code'] == exit_code
    assert result.metadata['stdout_preview_truncated'] and not result.metadata['stdout_capture_truncated']
    assert not backend.removed
    executor.close()
    assert backend.removed == ['container-456']
    reader = build_observation_tool(store)
    for name, expected in [('stdout', stdout), ('stderr', stderr)]:
        parts, cursor = [], 0
        while True:
            page = reader.handler({'source_ref': result.metadata['source_ref'], 'stream': name,
                                   'start_byte': cursor, 'max_bytes': 65536})
            assert page.ok
            parts.append(page.content)
            cursor = page.metadata['next_start_byte']
            if cursor is None:
                break
        assert ''.join(parts) == expected


def prepared(context) -> PreparedSWEbenchSource:
    return PreparedSWEbenchSource(
        instance_id="owner__repo-1",
        image_ref="registry:5000/team/image:latest",
        image_digest=DIGEST,
        dataset_base_commit="test-input-commit",
        prepared_head="test-input-commit",
        prepared_tree=git(context.source_root, "rev-parse", "HEAD^{tree}"),
        source_root=context.source_root,
        manifest_path=context.source_root.parent / "manifest.json",
        cache_hit=True,
    )


def test_scoped_waiting_command_cancel_does_not_cancel_active_command(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from codeagent.runtime.command import ScopedCommandExecutor
    _, store, context = execution_session(tmp_path)
    started, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    class BlockingBackend(FakeDockerExecutionBackend):
        def execute(self, *args):
            started.set()
            assert release.wait(10)
            assert not args[-1](), "取消排队子命令不能串到正在运行的父命令"
            return super().execute(*args)
    backend = BlockingBackend(context.active_root)
    executor = SWEbenchDockerCommandExecutor(prepared(context), backend, import_probe=lambda *_: None)
    parent = CommandService(context, AutoApprovalGate(False), executor, store, CommandArtifactStore(store))
    child = CommandService(context, AutoApprovalGate(False), ScopedCommandExecutor(executor, cancelled.is_set),
                           store, CommandArtifactStore(store))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(parent.run_command, {"command": "active parent"})
        assert started.wait(5)
        second = pool.submit(child.run_command, {"command": "waiting child"})
        cancelled.set()
        try:
            result = second.result(timeout=3)
            assert not result.ok and result.error_code == "execution_cancelled"
        finally:
            release.set()
        assert first.result(timeout=5).ok
    assert len(backend.created) == len(backend.executions) == 1
    assert not executor.cancelled()
    executor.close()


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=True
    ).stdout.strip()


class FakeDockerExecutionBackend:
    def __init__(
        self, workspace_root: Path, *, exit_code: int = 0,
        timed_out: bool = False, cleanup_error: bool = False,
    ) -> None:
        self.workspace_root = workspace_root
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.cleanup_error = cleanup_error
        self.created_argv: list[str] | None = None
        self.executed_argv = None
        self.created = []
        self.started = []
        self.executions = []
        self.removed: list[str] = []

    def create(self, argv):
        self.created_argv = list(argv)
        self.created.append(list(argv))
        return "container-456"

    def start(self, container_id):
        self.started.append(container_id)

    def execute(self, container_id, argv, stdout_path, stderr_path, timeout_seconds, output_limit_bytes, cancelled):
        assert container_id == "container-456"
        self.executed_argv = list(argv)
        self.executions.append(list(argv))
        assert self.created_argv is not None
        home_env = next(value for index, value in enumerate(self.created_argv) if self.created_argv[index - 1:index] == ["--env"] and value.startswith("HOME="))
        assert home_env == "HOME=/codeagent-runtime/home"
        # Docker bind mount 的效果由 fixture 直接作用于同一 host Candidate 模拟。
        (self.workspace_root / "from-container.txt").write_text("visible on host\n", encoding="utf-8")
        marker_mount = next(
            value for index, value in enumerate(self.created_argv)
            if self.created_argv[index - 1:index] == ["--mount"] and ("dst=/codeagent-runtime," in value or value.endswith("dst=/codeagent-runtime"))
        )
        runtime_home = Path(marker_mount.split("src=", 1)[1].split(",dst=", 1)[0])
        (runtime_home / 'markers' / Path(argv[-2]).name).write_text("started", encoding="utf-8")
        stdout_path.write_text("container stdout\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        return DockerExecutionOutcome(self.exit_code, self.timed_out, True, False, False)

    def remove(self, container_id):
        self.removed.append(container_id)
        if self.cleanup_error:
            raise RuntimeError("cannot remove container")


def sandbox_request(context, command="git status --short", cwd=".", network=NetworkMode.OFF,
                    purpose="utility"):
    raw = CommandRequest(
        "exec", command, cwd, 10, purpose, (), command_sha256="hash",
    )
    runtime = context.active_root.parent / "runtime"
    runtime.mkdir(exist_ok=True)
    policy = EffectiveSandboxPolicy(
        "v1", context.active_root, context.source_root, runtime,
        (), (), (), network,
    )
    return SandboxExecutionRequest(raw, policy)


def test_projection_pins_image_and_mounts_candidate_and_git_metadata(tmp_path):
    _, store, context = execution_session(tmp_path)
    backend = FakeDockerExecutionBackend(context.active_root)
    executor = SWEbenchDockerCommandExecutor(prepared(context), backend)
    request = sandbox_request(context, cwd="tests")
    (context.active_root / "tests").mkdir()
    artifacts = CommandArtifactStore(store).prepare(request.command)

    result = executor.execute(request, context, artifacts)

    assert result.exit_code == 0 and result.payload_started
    assert result.stdout == "container stdout\n"
    assert not backend.removed
    executor.close()
    executor.close()
    assert backend.removed == ["container-456"]
    argv = backend.created_argv
    assert argv is not None
    assert "registry:5000/team/image@" + DIGEST in argv
    assert ["--network", "none"] == argv[0:2]
    assert argv[2:4] == ["--user", f"{os.getuid()}:{os.getgid()}"]
    assert "/testbed/tests" in backend.executed_argv
    mounts = [value for index, value in enumerate(argv) if argv[index - 1:index] == ["--mount"]]
    assert any(f"src={context.active_root},dst=/testbed" in value and "readonly" not in value for value in mounts)
    assert any("dst=/testbed/.git,readonly" in value for value in mounts)
    common = git(context.active_root, "rev-parse", "--git-common-dir")
    common = (context.active_root / common).resolve()
    assert any(f"src={common},dst=/testbed/.git,readonly" in value for value in mounts)


def test_container_preserves_same_shell_semantics_for_all_purposes(tmp_path):
    _, store, context = execution_session(tmp_path)
    utility_backend = FakeDockerExecutionBackend(context.active_root)
    utility = sandbox_request(context, command="false | tee result", purpose="utility")
    SWEbenchDockerCommandExecutor(prepared(context), utility_backend).execute(
        utility, context, CommandArtifactStore(store).prepare(utility.command)
    )
    validation_backend = FakeDockerExecutionBackend(context.active_root)
    validation = sandbox_request(context, command="false | tee result", purpose="validation")
    validation = SandboxExecutionRequest(
        CommandRequest(**{**validation.command.__dict__, "execution_id": "validation"}),
        validation.policy,
    )
    SWEbenchDockerCommandExecutor(prepared(context), validation_backend).execute(
        validation, context, CommandArtifactStore(store).prepare(validation.command)
    )

    assert "-o pipefail" not in utility_backend.executed_argv[-4]
    assert "-o pipefail" not in validation_backend.executed_argv[-4]
    assert utility_backend.executed_argv[-1] == validation_backend.executed_argv[-1] == "false | tee result"


def test_command_service_audits_container_changes_on_host_candidate(tmp_path):
    _, store, context = execution_session(tmp_path)
    backend = FakeDockerExecutionBackend(context.active_root)
    service = CommandService(
        context,
        AutoApprovalGate(True),
        SWEbenchDockerCommandExecutor(prepared(context), backend),
        store,
        CommandArtifactStore(store),
    )

    result = service.run_command({"command": "generate", "purpose": "utility"})

    assert result.ok
    assert "after_candidate_revision" not in result.metadata
    assert (context.active_root / "from-container.txt").read_text() == "visible on host\n"


def test_rejects_network_semantics_and_wrong_source_before_container_create(tmp_path):
    _, store, context = execution_session(tmp_path)
    backend = FakeDockerExecutionBackend(context.active_root)
    executor = SWEbenchDockerCommandExecutor(prepared(context), backend)
    artifacts = CommandArtifactStore(store).prepare(sandbox_request(context).command)

    network = executor.execute(sandbox_request(context, network=NetworkMode.HOST), context, artifacts)

    assert network.status.value == "sandbox_setup_failed"
    assert "network" in (network.spawn_error or "")
    assert backend.created_argv is None

    wrong = PreparedSWEbenchSource(
        **{**prepared(context).__dict__, "source_root": tmp_path / "wrong"}
    )
    wrong_result = SWEbenchDockerCommandExecutor(wrong, backend).execute(
        sandbox_request(context), context, artifacts
    )
    assert wrong_result.exit_code == 0 and backend.created_argv is not None


def test_timeout_and_cleanup_failure_are_not_reported_as_success(tmp_path):
    _, store, context = execution_session(tmp_path)
    timeout_backend = FakeDockerExecutionBackend(context.active_root, exit_code=137, timed_out=True)
    timeout_request = sandbox_request(context)
    timeout_artifacts = CommandArtifactStore(store).prepare(timeout_request.command)

    timed_out = SWEbenchDockerCommandExecutor(prepared(context), timeout_backend).execute(
        timeout_request, context, timeout_artifacts
    )

    assert timed_out.status.value == "execution_timed_out"
    assert timed_out.timed_out and timed_out.process_tree_stopped

    assert timeout_backend.removed == []  # 单命令超时不重建或销毁整题环境。
    cleanup_backend = FakeDockerExecutionBackend(context.active_root, cleanup_error=True)
    executor = SWEbenchDockerCommandExecutor(prepared(context), cleanup_backend)
    request = sandbox_request(context)
    request = SandboxExecutionRequest(CommandRequest(**{**request.command.__dict__, 'execution_id': 'cleanup'}), request.policy)
    result = executor.execute(request, context, CommandArtifactStore(store).prepare(request.command))
    assert result.exit_code == 0
    with pytest.raises(RuntimeError, match='cannot remove'):
        executor.close()


def test_rejects_mutated_prepared_source_cache(tmp_path):
    _, store, context = execution_session(tmp_path)
    backend = FakeDockerExecutionBackend(context.active_root)
    request = sandbox_request(context)
    artifacts = CommandArtifactStore(store).prepare(request.command)
    (context.source_root / "unexpected.txt").write_text("dirty\n", encoding="utf-8")

    result = SWEbenchDockerCommandExecutor(prepared(context), backend).execute(
        request, context, artifacts
    )

    assert result.status.value == "completed"
    assert (context.active_root / "unexpected.txt").read_text() == "dirty\n"
    assert backend.created_argv is not None


def test_reuses_one_container_until_explicit_task_close(tmp_path):
    _, store, context = execution_session(tmp_path)
    backend = FakeDockerExecutionBackend(context.active_root)
    executor = SWEbenchDockerCommandExecutor(prepared(context), backend)
    service = CommandService(context, AutoApprovalGate(False), executor, store, CommandArtifactStore(store))
    for command in ('first', 'second', 'third'):
        assert service.run_command({'command': command}).ok
    assert len(backend.created) == len(backend.started) == 1
    assert len(backend.executions) == 3 and not backend.removed
    executor.close()
    executor.close()
    assert backend.removed == ['container-456']
    assert not executor._task_root.exists()
    result = service.run_command({'command': 'after-close'})
    assert not result.ok and '已关闭' in result.metadata['diagnostic']
    assert len(backend.created) == 1


def test_cancel_before_first_command_does_not_create_container(tmp_path):
    _, store, context = execution_session(tmp_path)
    backend = FakeDockerExecutionBackend(context.active_root)
    executor = SWEbenchDockerCommandExecutor(prepared(context), backend)
    executor.cancelled = lambda: True
    service = CommandService(context, AutoApprovalGate(False), executor, store, CommandArtifactStore(store))
    result = service.run_command({'command': 'must-not-execute'})
    assert result.error_code == 'execution_cancelled' and not backend.created


def test_failed_container_start_is_cleaned_and_not_implicitly_recreated(tmp_path):
    _, store, context = execution_session(tmp_path)
    class BrokenBackend(FakeDockerExecutionBackend):
        def start(self, ident):
            raise RuntimeError('start failed')
    backend = BrokenBackend(context.active_root)
    executor = SWEbenchDockerCommandExecutor(prepared(context), backend)
    service = CommandService(context, AutoApprovalGate(False), executor, store, CommandArtifactStore(store))
    assert not service.run_command({'command': 'first'}).ok
    assert backend.removed == ['container-456']
    assert not service.run_command({'command': 'second'}).ok
    assert len(backend.created) == 1


def test_persistent_mounts_do_not_retain_once_host_grants(tmp_path):
    from dataclasses import replace
    _, store, context = execution_session(tmp_path)
    backend = FakeDockerExecutionBackend(context.active_root)
    executor = SWEbenchDockerCommandExecutor(prepared(context), backend)
    request = sandbox_request(context)
    request = replace(request, policy=replace(request.policy, read_grants=(tmp_path,)))
    result = executor.execute(request, context, CommandArtifactStore(store).prepare(request.command))
    assert not result.payload_started and 'once' in result.spawn_error
    assert not backend.created


def test_mount_policy_change_cannot_reuse_old_access(tmp_path):
    from dataclasses import replace
    _, store, context = execution_session(tmp_path)
    backend = FakeDockerExecutionBackend(context.active_root)
    executor = SWEbenchDockerCommandExecutor(prepared(context), backend)
    request = sandbox_request(context)
    executor.execute(request, context, CommandArtifactStore(store).prepare(request.command))
    changed = replace(request, command=replace(request.command, execution_id='changed'),
                      policy=replace(request.policy, hard_deny_paths=(context.active_root / 'private',)))
    result = executor.execute(changed, context, CommandArtifactStore(store).prepare(changed.command))
    assert result.status.value == 'sandbox_setup_failed' and '权限' in result.spawn_error
    assert len(backend.executions) == 1 and backend.removed == ['container-456']
