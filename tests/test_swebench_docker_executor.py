from __future__ import annotations

import os
import subprocess
from pathlib import Path

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
from test_candidate_loop import execution_session


DIGEST = "sha256:" + "b" * 64


def prepared(context) -> PreparedSWEbenchSource:
    return PreparedSWEbenchSource(
        instance_id="owner__repo-1",
        image_ref="registry:5000/team/image:latest",
        image_digest=DIGEST,
        dataset_base_commit=context.base_commit,
        prepared_head=context.base_commit,
        prepared_tree=git(context.source_root, "rev-parse", "HEAD^{tree}"),
        source_root=context.source_root,
        manifest_path=context.source_root.parent / "manifest.json",
        cache_hit=True,
    )


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=True
    ).stdout.strip()


class FakeDockerExecutionBackend:
    def __init__(
        self, candidate_root: Path, *, exit_code: int = 0,
        timed_out: bool = False, cleanup_error: bool = False,
    ) -> None:
        self.candidate_root = candidate_root
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.cleanup_error = cleanup_error
        self.created_argv: list[str] | None = None
        self.removed: list[str] = []

    def create(self, argv):
        self.created_argv = list(argv)
        return "container-456"

    def start_attached(self, container_id, stdout_path, stderr_path, timeout_seconds, output_limit_bytes):
        assert container_id == "container-456"
        assert self.created_argv is not None
        home_env = next(value for index, value in enumerate(self.created_argv) if self.created_argv[index - 1:index] == ["--env"] and value.startswith("HOME="))
        assert home_env == "HOME=/codeagent-runtime/home"
        # Docker bind mount 的效果由 fixture 直接作用于同一 host Candidate 模拟。
        (self.candidate_root / "from-container.txt").write_text("visible on host\n", encoding="utf-8")
        marker_mount = next(
            value for index, value in enumerate(self.created_argv)
            if self.created_argv[index - 1:index] == ["--mount"] and "dst=/codeagent-runtime/home" in value
        )
        runtime_home = Path(marker_mount.split("src=", 1)[1].split(",dst=", 1)[0])
        (runtime_home / "payload-started").write_text("started", encoding="utf-8")
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
        "exec", command, cwd, 10, purpose, (), context.workspace_revision,
        context.base_commit, 0, "tree", "hash",
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
    assert backend.removed == ["container-456"]
    argv = backend.created_argv
    assert argv is not None
    assert "registry:5000/team/image@" + DIGEST in argv
    assert ["--network", "none"] == argv[0:2]
    assert argv[2:4] == ["--user", f"{os.getuid()}:{os.getgid()}"]
    assert "/testbed/tests" in argv
    mounts = [value for index, value in enumerate(argv) if argv[index - 1:index] == ["--mount"]]
    assert any(f"src={context.active_root},dst=/testbed" in value and "readonly" not in value for value in mounts)
    assert any("dst=/testbed/.git,readonly" in value for value in mounts)
    common = git(context.active_root, "rev-parse", "--git-common-dir")
    common = (context.active_root / common).resolve()
    assert any(f"src={common},dst={common},readonly" in value for value in mounts)


def test_container_inner_shell_enables_pipefail_only_for_validation(tmp_path):
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

    assert "-o pipefail" not in utility_backend.created_argv[-4]
    assert "-o pipefail" in validation_backend.created_argv[-4]


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
    assert result.metadata["after_candidate_revision"] == 1
    assert result.metadata["command_induced_changes"] == [
        {"path": "from-container.txt", "kind": "create"}
    ]
    assert not (context.source_root / "from-container.txt").exists()


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
    assert "source" in (wrong_result.spawn_error or "")
    assert backend.created_argv is None


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

    cleanup_backend = FakeDockerExecutionBackend(context.active_root, cleanup_error=True)
    cleanup_request = sandbox_request(context)
    cleanup_request = SandboxExecutionRequest(
        CommandRequest(**{**cleanup_request.command.__dict__, "execution_id": "cleanup"}),
        cleanup_request.policy,
    )
    cleanup_artifacts = CommandArtifactStore(store).prepare(cleanup_request.command)
    cleanup = SWEbenchDockerCommandExecutor(prepared(context), cleanup_backend).execute(
        cleanup_request, context, cleanup_artifacts
    )

    assert cleanup.status.value == "sandbox_setup_failed"
    assert "cannot remove" in (cleanup.spawn_error or "")


def test_rejects_mutated_prepared_source_cache(tmp_path):
    _, store, context = execution_session(tmp_path)
    backend = FakeDockerExecutionBackend(context.active_root)
    request = sandbox_request(context)
    artifacts = CommandArtifactStore(store).prepare(request.command)
    (context.source_root / "unexpected.txt").write_text("dirty\n", encoding="utf-8")

    result = SWEbenchDockerCommandExecutor(prepared(context), backend).execute(
        request, context, artifacts
    )

    assert result.status.value == "sandbox_setup_failed"
    assert "source" in (result.spawn_error or "")
    assert backend.created_argv is None
