from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, Sequence

from codeagent.benchmark.swebench_source import PreparedSWEbenchSource, _replace_tag_with_digest
from codeagent.runtime.command import (
    CommandArtifactPaths,
    CommandExecutionStatus,
    CommandResult,
    SandboxExecutionRequest,
)
from codeagent.runtime.permissions import NetworkMode
from codeagent.workspace.workspace import WorkspaceContext


@dataclass(frozen=True)
class DockerExecutionOutcome:
    exit_code: int | None
    timed_out: bool
    process_tree_stopped: bool
    stdout_truncated: bool
    stderr_truncated: bool


class DockerExecutionBackend(Protocol):
    def create(self, argv: Sequence[str]) -> str: ...

    def start_attached(
        self,
        container_id: str,
        stdout_path: Path,
        stderr_path: Path,
        timeout_seconds: int,
        output_limit_bytes: int,
    ) -> DockerExecutionOutcome: ...

    def remove(self, container_id: str) -> None: ...


class DockerCLIExecutionBackend:
    """只用固定 argv 驱动 Docker；不向 shell 拼接 task 或 Agent 输入。"""

    def create(self, argv: Sequence[str]) -> str:
        try:
            proc = subprocess.run(
                ["docker", "create", *argv], capture_output=True, text=True,
                shell=False, check=False, timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"docker create 调用失败: {exc}") from exc
        if proc.returncode:
            raise RuntimeError((proc.stderr or proc.stdout or "docker create failed").strip()[-4000:])
        container_id = proc.stdout.strip()
        if not container_id:
            raise RuntimeError("docker create 未返回 container id")
        return container_id

    def start_attached(
        self,
        container_id: str,
        stdout_path: Path,
        stderr_path: Path,
        timeout_seconds: int,
        output_limit_bytes: int,
    ) -> DockerExecutionOutcome:
        try:
            process = subprocess.Popen(
                ["docker", "start", "--attach", container_id],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                shell=False, start_new_session=True, close_fds=True,
            )
        except OSError as exc:
            raise RuntimeError(f"docker start 调用失败: {exc}") from exc
        stdout = _CappedOutput(process.stdout, stdout_path, output_limit_bytes)
        stderr = _CappedOutput(process.stderr, stderr_path, output_limit_bytes)
        stdout.start()
        stderr.start()
        timed_out = False
        stopped = True
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            stopped = self._stop(container_id, process)
        stdout.join()
        stderr.join()
        exit_code = self._container_exit_code(container_id) if stopped else None
        return DockerExecutionOutcome(
            exit_code, timed_out, stopped, stdout.truncated, stderr.truncated
        )

    def remove(self, container_id: str) -> None:
        try:
            proc = subprocess.run(
                ["docker", "rm", "--force", container_id], capture_output=True,
                text=True, shell=False, check=False, timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"docker rm 调用失败: {exc}") from exc
        if proc.returncode:
            raise RuntimeError((proc.stderr or proc.stdout or "docker rm failed").strip()[-4000:])

    @staticmethod
    def _container_exit_code(container_id: str) -> int | None:
        try:
            proc = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.ExitCode}}", container_id],
                capture_output=True, text=True, shell=False, check=False, timeout=10,
            )
            return int(proc.stdout.strip()) if proc.returncode == 0 else None
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    @staticmethod
    def _stop(container_id: str, process: subprocess.Popen) -> bool:
        try:
            subprocess.run(
                ["docker", "kill", container_id], capture_output=True, shell=False,
                check=False, timeout=10,
            )
            process.wait(timeout=10)
        except (OSError, subprocess.SubprocessError):
            try:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                return False
        return process.poll() is not None


class SWEbenchDockerCommandExecutor:
    """在官方 instance image 中执行命令，workspace 事实仍保留在 host Candidate。"""

    OUTPUT_LIMIT_BYTES = 2 * 1024 * 1024
    CONTAINER_WORKSPACE = Path("/testbed")
    RUNTIME_ROOT = Path("/codeagent-runtime")
    ENVIRONMENT_CONTRACT_REVISION = "swebench-prepared-environment-v1"

    def __init__(
        self,
        prepared_source: PreparedSWEbenchSource,
        backend: DockerExecutionBackend | None = None,
    ) -> None:
        self.prepared_source = prepared_source
        self.backend = backend or DockerCLIExecutionBackend()

    def execute(
        self,
        request: SandboxExecutionRequest,
        context: WorkspaceContext,
        artifacts: CommandArtifactPaths | None,
    ) -> CommandResult:
        started_at = datetime.now(timezone.utc).isoformat()
        started = time.monotonic()
        if artifacts is None:
            return self._failure("Docker executor需要runtime目录", started_at, started, request)
        identity_error = self._identity_error(context)
        if identity_error:
            return self._failure(identity_error, started_at, started, request)
        if request.policy.network_mode != NetworkMode.OFF:
            return self._failure("SWE-bench Docker executor尚不支持获批的host network语义", started_at, started, request)

        marker = artifacts.runtime_home / "payload-started"
        container_id: str | None = None
        cleanup_error: str | None = None
        try:
            argv = self._create_argv(request, context, artifacts, marker)
            container_id = self.backend.create(argv)
            outcome = self.backend.start_attached(
                container_id, artifacts.stdout_path, artifacts.stderr_path,
                request.command.timeout_seconds, self.OUTPUT_LIMIT_BYTES,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return self._failure(str(exc), started_at, started, request)
        finally:
            if container_id:
                try:
                    self.backend.remove(container_id)
                except RuntimeError as exc:
                    cleanup_error = str(exc)

        payload_started = marker.exists()
        status = CommandExecutionStatus.TIMED_OUT if outcome.timed_out else CommandExecutionStatus.COMPLETED
        if not payload_started and not outcome.timed_out:
            status = CommandExecutionStatus.SANDBOX_SETUP_FAILED
        if cleanup_error:
            status = CommandExecutionStatus.SANDBOX_SETUP_FAILED
        return CommandResult(
            exit_code=outcome.exit_code,
            stdout=_summary(artifacts.stdout_path),
            stderr=_summary(artifacts.stderr_path),
            status=status,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=max(0, round((time.monotonic() - started) * 1000)),
            timed_out=outcome.timed_out,
            spawn_error=cleanup_error or (None if payload_started else "container在payload启动前失败"),
            stdout_truncated=outcome.stdout_truncated,
            stderr_truncated=outcome.stderr_truncated,
            stdout_artifact=str(artifacts.stdout_path),
            stderr_artifact=str(artifacts.stderr_path),
            environment_names=("HOME", "TMPDIR", "XDG_CACHE_HOME", "PYTHONIOENCODING", "PYTHONDONTWRITEBYTECODE"),
            payload_started=payload_started,
            process_tree_stopped=outcome.process_tree_stopped,
            effective_policy=request.policy.summary(),
            environment_contract_revision=self.ENVIRONMENT_CONTRACT_REVISION,
            backend="swebench_docker", backend_availability="available",
            launch_cwd=str(self.CONTAINER_WORKSPACE / request.command.cwd),
            effective_network_policy="off",
            effective_filesystem_policy=_filesystem_policy(request.policy.summary()),
        )

    def environment_snapshot(self, context, artifacts, policy) -> dict:
        return {
            "contract_revision": self.ENVIRONMENT_CONTRACT_REVISION,
            "backend": "swebench_docker", "backend_availability": "prepared",
            "shell": "/bin/bash --noprofile --norc", "default_cwd": ".",
            "default_cwd_resolves_to": str(self.CONTAINER_WORKSPACE),
            "path_source": "prepared_image_default",
            "environment_names": [
                "GIT_OPTIONAL_LOCKS", "HOME", "PYTHONDONTWRITEBYTECODE", "PYTHONIOENCODING",
                "TMPDIR", "XDG_CACHE_HOME",
            ],
            "home_kind": "private_runtime_home", "tilde_is_host_home": False,
            "tmp_kind": "private_tmp", "network_mode": "off",
            "commands_are_fresh_processes": True, "shell_state_persists": False,
            "source_workspace_access": "prepared_backend_policy",
            "active_workspace_access": "read_write",
        }

    def _create_argv(
        self,
        request: SandboxExecutionRequest,
        context: WorkspaceContext,
        artifacts: CommandArtifactPaths,
        marker: Path,
    ) -> list[str]:
        common_git = _git_path(context.active_root, "--git-common-dir")
        active_git = context.active_root / ".git"
        if not active_git.is_file() or not common_git.is_dir():
            raise ValueError("Candidate不是可投影的linked Git worktree")
        image = _replace_tag_with_digest(
            self.prepared_source.image_ref, self.prepared_source.image_digest
        )
        container_cwd = self.CONTAINER_WORKSPACE / request.command.cwd
        container_home = self.RUNTIME_ROOT / "home"
        container_temp = self.RUNTIME_ROOT / "tmp"
        mounts: list[tuple[Path, Path, bool]] = [
            (context.active_root, self.CONTAINER_WORKSPACE, False),
            (active_git, self.CONTAINER_WORKSPACE / ".git", True),
            (common_git, common_git, True),
            (artifacts.runtime_home, container_home, False),
            (artifacts.runtime_temp, container_temp, False),
        ]
        for path in request.policy.read_grants:
            mounts.append((path, path, True))
        for path in request.policy.write_grants:
            mounts.append((path, path, False))
        mounts = _deduplicate_mounts(mounts)
        argv = [
            "--network", "none",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--workdir", str(container_cwd),
        ]
        for name, value in {
            "HOME": str(container_home),
            "TMPDIR": str(container_temp),
            "XDG_CACHE_HOME": str(container_home / "cache"),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        }.items():
            argv.extend(("--env", f"{name}={value}"))
        for source, destination, readonly in mounts:
            value = f"type=bind,src={source},dst={destination}"
            if readonly:
                value += ",readonly"
            argv.extend(("--mount", value))
        wrapper = (
            'printf started > "$1"; '
            'if [ -f /opt/miniconda3/etc/profile.d/conda.sh ]; then '
            'source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed; fi; '
            'exec /bin/bash --noprofile --norc -c "$2"'
        )
        container_marker = container_home / marker.name
        argv.extend((
            image, "/bin/bash", "--noprofile", "--norc", "-c", wrapper,
            "codeagent-docker-launcher", str(container_marker), request.command.command,
        ))
        return argv

    def _identity_error(self, context: WorkspaceContext) -> str | None:
        if context.workspace_kind != "git_worktree":
            return "SWE-bench Docker executor只接受Git Candidate worktree"
        if context.source_root.resolve() != self.prepared_source.source_root.resolve():
            return "Workspace source与prepared SWE-bench source不一致"
        if context.base_commit != self.prepared_source.prepared_head:
            return "Workspace base commit与prepared HEAD不一致"
        expected = self.prepared_source
        checks = (
            (context.source_root, ("rev-parse", "HEAD"), expected.prepared_head, "source HEAD"),
            (context.source_root, ("rev-parse", "HEAD^{tree}"), expected.prepared_tree, "source tree"),
            (context.active_root, ("rev-parse", "HEAD"), expected.prepared_head, "Candidate HEAD"),
        )
        for root, args, wanted, label in checks:
            try:
                actual = _git_text(root, *args)
            except ValueError as exc:
                return f"无法验证{label}: {exc}"
            if actual != wanted:
                return f"{label}与prepared identity不一致"
        try:
            if _git_text(context.source_root, "status", "--porcelain=v1", "--untracked-files=all"):
                return "prepared source cache出现未归因变化"
        except ValueError as exc:
            return f"无法验证prepared source clean状态: {exc}"
        return None

    @staticmethod
    def _failure(detail: str, started_at: str, started: float, request: SandboxExecutionRequest) -> CommandResult:
        return CommandResult(
            exit_code=None,
            status=CommandExecutionStatus.SANDBOX_SETUP_FAILED,
            spawn_error=detail,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=max(0, round((time.monotonic() - started) * 1000)),
            process_tree_stopped=True,
            effective_policy=request.policy.summary(),
            environment_contract_revision=SWEbenchDockerCommandExecutor.ENVIRONMENT_CONTRACT_REVISION,
            backend="swebench_docker", backend_availability="unavailable",
            launch_cwd=str(SWEbenchDockerCommandExecutor.CONTAINER_WORKSPACE / request.command.cwd),
            effective_network_policy=request.policy.network_mode.value,
            effective_filesystem_policy=_filesystem_policy(request.policy.summary()),
        )


def _filesystem_policy(policy: dict) -> dict:
    return {key: policy[key] for key in (
        "policy_revision", "read_grants", "write_grants", "hard_deny_paths", "protected_readonly_paths",
    )}


def _git_path(root: Path, flag: str) -> Path:
    path = Path(_git_text(root, "rev-parse", flag))
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _git_text(root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True,
            shell=False, check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"Git identity调用失败: {exc}") from exc
    if proc.returncode:
        raise ValueError((proc.stderr or proc.stdout or "Git identity command failed").strip())
    return proc.stdout.strip()


def _deduplicate_mounts(mounts: list[tuple[Path, Path, bool]]) -> list[tuple[Path, Path, bool]]:
    by_destination: dict[Path, tuple[Path, Path, bool]] = {}
    for source, destination, readonly in mounts:
        source = source.resolve(strict=True)
        destination = destination if destination.is_absolute() else destination.resolve()
        current = by_destination.get(destination)
        if current is not None and current[0] != source:
            raise ValueError(f"Docker mount destination冲突: {destination}")
        by_destination[destination] = (source, destination, readonly and (current[2] if current else True))
    return list(by_destination.values())


class _CappedOutput(threading.Thread):
    def __init__(self, source, target: Path, limit: int) -> None:
        super().__init__(daemon=True)
        self.source = source
        self.target = target
        self.limit = limit
        self.truncated = False

    def run(self) -> None:
        written = 0
        with self.target.open("wb") as output:
            while True:
                chunk = self.source.read(65536)
                if not chunk:
                    break
                remaining = max(0, self.limit - written)
                if remaining:
                    output.write(chunk[:remaining])
                    written += min(len(chunk), remaining)
                if len(chunk) > remaining:
                    self.truncated = True


def _summary(path: Path, head: int = 1024, tail: int = 3072) -> str:
    data = path.read_bytes()
    if len(data) > head + tail:
        data = data[:head] + b"\n...[middle truncated]...\n" + data[-tail:]
    return data.decode("utf-8", errors="replace")
