from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from codeagent.runtime.bubblewrap import BubblewrapBackend, MountPlan, SandboxUnavailable
from codeagent.runtime.artifacts import output_preview
from codeagent.runtime.command import CommandArtifactPaths, CommandExecutionStatus, CommandResult, SandboxExecutionRequest, command_cancellation
from codeagent.runtime.command_environment import (
    EffectiveCommandEnvironment, build_command_environment,
)
from codeagent.workspace.workspace import WorkspaceContext


class SandboxedCommandExecutor:
    OUTPUT_LIMIT_BYTES = 2 * 1024 * 1024

    def __init__(self, backend: BubblewrapBackend | None = None, *, cancelled=None) -> None:
        self.backend = backend or BubblewrapBackend()
        self.cancelled = cancelled or (lambda: False)

    def execute(
        self, request: SandboxExecutionRequest, context: WorkspaceContext,
        artifacts: CommandArtifactPaths | None,
    ) -> CommandResult:
        started_at = datetime.now(timezone.utc).isoformat()
        started = time.monotonic()
        cancelled = command_cancellation(self.cancelled)
        if artifacts is None:
            return self._failure(CommandExecutionStatus.EXECUTOR_REJECTED, "sandbox executor需要runtime目录", started_at, started, request)
        environment = self.command_environment(request, context, artifacts)
        try:
            features = self.backend.probe(context.active_root)
            mask_root = artifacts.command_dir / "masks"
            mask_dir = mask_root / "empty-dir"
            mask_file = mask_root / "empty-file"
            mask_dir.mkdir(parents=True, exist_ok=True)
            mask_file.touch()
            plan = MountPlan.build(request.policy, mask_dir, mask_file)
            marker = artifacts.runtime_home / "payload-started"
            command = request.command.command
            wrapper = f"printf started > \"$1\"; exec /bin/bash --noprofile --norc -c \"$2\""
            payload = ["/bin/bash", "--noprofile", "--norc", "-c", wrapper, "codeagent-launcher", str(marker), command]
            argv = self.backend.invocation(
                features, plan, request.policy, environment.launch_cwd, environment.process_environment, payload,
            )
        except SandboxUnavailable as exc:
            unavailable = self.command_environment(request, context, artifacts, availability="unavailable")
            return self._failure(CommandExecutionStatus.SANDBOX_UNAVAILABLE, str(exc), started_at, started, request, unavailable)
        except (OSError, ValueError) as exc:
            return self._failure(CommandExecutionStatus.SANDBOX_SETUP_FAILED, str(exc), started_at, started, request, environment)

        process = None
        try:
            process = subprocess.Popen(
                argv, cwd=context.active_root, env={}, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                start_new_session=True, close_fds=True,
            )
            stdout = _CappedOutput(process.stdout, artifacts.stdout_path, self.OUTPUT_LIMIT_BYTES)
            stderr = _CappedOutput(process.stderr, artifacts.stderr_path, self.OUTPUT_LIMIT_BYTES)
            stdout.start()
            stderr.start()
            timed_out = False
            tree_stopped = True
            try:
                command_deadline = started + request.command.timeout_seconds
                while process.poll() is None:
                    if cancelled():
                        status = CommandExecutionStatus.CANCELLED
                        tree_stopped = _terminate_group(process)
                        break
                    remaining = command_deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(argv, request.command.timeout_seconds)
                    try:
                        process.wait(timeout=min(0.05, remaining))
                    except subprocess.TimeoutExpired:
                        pass
                else:
                    status = CommandExecutionStatus.COMPLETED
                exit_code = process.returncode
                # 真实bwrap的PID namespace会回收子树；同时清理宿主侧同进程组的包装进程。
                tree_stopped = _terminate_group(process) and tree_stopped
            except subprocess.TimeoutExpired:
                timed_out = True
                status = CommandExecutionStatus.TIMED_OUT
                tree_stopped = _terminate_group(process)
                exit_code = process.returncode
            except KeyboardInterrupt:
                status = CommandExecutionStatus.CANCELLED
                tree_stopped = _terminate_group(process)
                exit_code = process.returncode
            stdout.join()
            stderr.join()
        except BaseException as exc:
            if process is not None:
                _terminate_group(process)
            if not isinstance(exc, OSError):
                raise
            return self._failure(CommandExecutionStatus.SPAWN_FAILED, str(exc), started_at, started, request, environment)
        payload_started = marker.exists()
        if not payload_started and status == CommandExecutionStatus.COMPLETED and exit_code != 0:
            status = CommandExecutionStatus.SANDBOX_SETUP_FAILED
        stdout_text, stdout_preview_truncated = output_preview(artifacts.stdout_path)
        stderr_text, stderr_preview_truncated = output_preview(artifacts.stderr_path)
        return CommandResult(
            exit_code=exit_code, status=status, stdout=stdout_text, stderr=stderr_text,
            stdout_preview_truncated=stdout_preview_truncated, stderr_preview_truncated=stderr_preview_truncated,
            started_at=started_at, finished_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=max(0, round((time.monotonic() - started) * 1000)), timed_out=timed_out,
            spawn_error=None if payload_started else "sandbox在payload启动前失败",
            stdout_truncated=stdout.truncated, stderr_truncated=stderr.truncated,
            stdout_artifact=str(artifacts.stdout_path), stderr_artifact=str(artifacts.stderr_path),
            payload_started=payload_started,
            process_tree_stopped=tree_stopped, effective_policy=request.policy.summary(),
            **environment.provenance(),
        )

    def command_environment(self, request, context, artifacts, *, availability="available") -> EffectiveCommandEnvironment:
        return build_command_environment(
            launch_cwd=context.active_root / request.command.cwd, runtime_home=artifacts.runtime_home,
            policy=request.policy, backend="bubblewrap", backend_availability=availability,
        )

    def environment_snapshot(self, context, artifacts, policy) -> dict:
        availability = "executable_available"
        try:
            discover = getattr(self.backend, "discover", None)
            if discover is not None:
                discover(context.active_root)
        except SandboxUnavailable:
            availability = "unavailable"
        environment = build_command_environment(
            launch_cwd=context.active_root, runtime_home=artifacts.runtime_root / "next-command" / "home",
            policy=policy, backend="bubblewrap", backend_availability=availability,
            create_directories=False,
        )
        return environment.snapshot()

    @staticmethod
    def _failure(status, error, started_at, started, request, environment=None) -> CommandResult:
        provenance = environment.provenance() if environment else {}
        return CommandResult(
            exit_code=None, status=status, spawn_error=error, started_at=started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=max(0, round((time.monotonic() - started) * 1000)),
            effective_policy=request.policy.summary(),
            **provenance,
        )


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


def _terminate_group(process: subprocess.Popen) -> bool:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except ProcessLookupError:
        return True
    except subprocess.TimeoutExpired:
        pass
    # leader可能已经退出，组内的后台包装进程仍可能忽略TERM。
    try:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    except ProcessLookupError:
        return process.poll() is not None
    except (OSError, subprocess.TimeoutExpired):
        return False
    return process.poll() is not None
