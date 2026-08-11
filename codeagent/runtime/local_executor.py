from __future__ import annotations

import os
import hashlib
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from codeagent.runtime.command import (
    CommandArtifactPaths,
    CommandExecutionStatus,
    CommandResult,
    CommandSpec,
)
from codeagent.workspace.workspace import WorkspaceContext

SUMMARY_HEAD_BYTES = 1024
SUMMARY_TAIL_BYTES = 3072


class LocalCommandExecutor:
    """执行已经批准的固定 argv；不接受或解释 shell 字符串。"""

    def execute(
        self,
        spec: CommandSpec,
        workspace_context: WorkspaceContext,
        artifacts: CommandArtifactPaths | None,
    ) -> CommandResult:
        started_at = _now()
        started = time.monotonic()
        if artifacts is None:
            return self._rejected(started_at, started, "LocalCommandExecutor 需要 artifact 目标")
        rejection = self._validate(spec, workspace_context)
        if rejection:
            return self._rejected(started_at, started, rejection, artifacts)

        cwd = (workspace_context.active_root / spec.cwd).resolve()
        environment = _minimal_environment(artifacts)
        before, audit_error = _git_status(workspace_context.active_root)
        before_fingerprints = _workspace_fingerprints(workspace_context.active_root, before)
        popen_kwargs = {
            "cwd": cwd,
            "env": environment,
            "stdin": subprocess.DEVNULL,
            "shell": False,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        try:
            with artifacts.stdout_path.open("wb") as stdout_file, artifacts.stderr_path.open("wb") as stderr_file:
                process = subprocess.Popen(
                    list(spec.argv), stdout=stdout_file, stderr=stderr_file, **popen_kwargs
                )
                try:
                    exit_code = process.wait(timeout=spec.timeout_seconds)
                    status = CommandExecutionStatus.COMPLETED
                    timed_out = False
                except subprocess.TimeoutExpired:
                    _terminate_process_tree(process)
                    exit_code = process.wait(timeout=5) if process.poll() is None else process.returncode
                    status = CommandExecutionStatus.TIMED_OUT
                    timed_out = True
        except OSError as exc:
            audit = _workspace_audit(workspace_context.active_root, artifacts, before, before_fingerprints, audit_error)
            return self._result(
                status=CommandExecutionStatus.SPAWN_FAILED,
                exit_code=None,
                started_at=started_at,
                started=started,
                artifacts=artifacts,
                environment=environment,
                spawn_error=str(exc),
                audit=audit,
            )

        audit = _workspace_audit(workspace_context.active_root, artifacts, before, before_fingerprints, audit_error)
        return self._result(
            status=status,
            exit_code=exit_code,
            started_at=started_at,
            started=started,
            artifacts=artifacts,
            environment=environment,
            timed_out=timed_out,
            audit=audit,
        )

    @staticmethod
    def _validate(spec: CommandSpec, context: WorkspaceContext) -> str | None:
        if context.workspace_kind != "git_worktree":
            return "只允许在 Git worktree 中执行"
        if spec.command_kind != "pytest":
            return "LocalCommandExecutor 只支持 pytest"
        if not spec.argv or spec.argv[0] != sys.executable or spec.argv[1:3] != ("-m", "pytest"):
            return "argv 必须使用已批准的 sys.executable -m pytest"
        try:
            cwd = (context.active_root / spec.cwd).resolve()
            cwd.relative_to(context.active_root)
        except ValueError:
            return "cwd 越出 active workspace"
        if not cwd.is_dir():
            return "cwd 不存在或不是目录"
        return None

    def _rejected(
        self,
        started_at: str,
        started: float,
        reason: str,
        artifacts: CommandArtifactPaths | None = None,
    ) -> CommandResult:
        return self._result(
            CommandExecutionStatus.EXECUTOR_REJECTED,
            None,
            started_at,
            started,
            artifacts,
            {},
            spawn_error=reason,
        )

    @staticmethod
    def _result(
        status: CommandExecutionStatus,
        exit_code: int | None,
        started_at: str,
        started: float,
        artifacts: CommandArtifactPaths | None,
        environment: dict[str, str],
        timed_out: bool = False,
        spawn_error: str | None = None,
        audit: dict | None = None,
    ) -> CommandResult:
        stdout, stdout_truncated = _summarize(artifacts.stdout_path) if artifacts else ("", False)
        stderr, stderr_truncated = _summarize(artifacts.stderr_path) if artifacts else ("", False)
        return CommandResult(
            status=status,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            started_at=started_at,
            finished_at=_now(),
            duration_ms=max(0, round((time.monotonic() - started) * 1000)),
            timed_out=timed_out,
            spawn_error=spawn_error,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            stdout_artifact=str(artifacts.stdout_path) if artifacts else None,
            stderr_artifact=str(artifacts.stderr_path) if artifacts else None,
            environment_names=tuple(sorted(environment)),
            workspace_changed=audit.get("changed") if audit else None,
            changed_files=tuple(audit.get("files", ())) if audit else (),
            preexisting_changed_files=tuple(audit.get("preexisting_files", ())) if audit else (),
            command_introduced_changes=tuple(audit.get("introduced_files", ())) if audit else (),
            workspace_change_artifact=str(artifacts.workspace_change_path) if artifacts else None,
            workspace_audit_error=audit.get("error") if audit else None,
            git_status_before=audit.get("before") if audit else None,
            git_status_after=audit.get("after") if audit else None,
            git_fingerprints_before=audit.get("before_fingerprints") if audit else None,
            git_fingerprints_after=audit.get("after_fingerprints") if audit else None,
        )


def _minimal_environment(artifacts: CommandArtifactPaths) -> dict[str, str]:
    canonical = {
        "PATH": "PATH", "SYSTEMROOT": "SYSTEMROOT", "WINDIR": "WINDIR",
        "COMSPEC": "COMSPEC", "PATHEXT": "PATHEXT", "SYSTEMDRIVE": "SYSTEMDRIVE",
        "LANG": "LANG", "LC_ALL": "LC_ALL", "TZ": "TZ",
    }
    env = {
        canonical[name.upper()]: value
        for name, value in os.environ.items()
        if name.upper() in canonical
    }
    home = str(artifacts.runtime_home)
    temp = str(artifacts.runtime_temp)
    env.update({"HOME": home, "USERPROFILE": home, "TEMP": temp, "TMP": temp, "TMPDIR": temp})
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _summarize(path: Path) -> tuple[str, bool]:
    size = path.stat().st_size
    limit = SUMMARY_HEAD_BYTES + SUMMARY_TAIL_BYTES
    with path.open("rb") as stream:
        if size <= limit:
            content = stream.read()
            truncated = False
        else:
            head = stream.read(SUMMARY_HEAD_BYTES)
            stream.seek(-SUMMARY_TAIL_BYTES, os.SEEK_END)
            tail = stream.read(SUMMARY_TAIL_BYTES)
            content = head + b"\n...[middle truncated]...\n" + tail
            truncated = True
    return content.decode("utf-8", errors="replace"), truncated


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        system_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
        if not system_root:
            process.kill()
            return
        taskkill = str(Path(system_root) / "System32" / "taskkill.exe")
        subprocess.run(
            [taskkill, "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            check=False,
            timeout=5,
        )
        if process.poll() is None:
            process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_status(root: Path) -> tuple[str | None, str | None]:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"], cwd=root,
            text=True, encoding="utf-8", errors="replace", capture_output=True, shell=False, check=False, timeout=10,
        )
        if proc.returncode:
            return None, proc.stderr.strip() or "git status failed"
        return proc.stdout, None
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)


def _workspace_audit(
    root: Path,
    artifacts: CommandArtifactPaths,
    before: str | None,
    before_fingerprints: dict[str, str],
    prior_error: str | None,
) -> dict:
    after, after_error = _git_status(root)
    errors = [item for item in (prior_error, after_error) if item]
    patch_text = ""
    try:
        diff = subprocess.run(
            ["git", "diff", "--binary", "--no-ext-diff"], cwd=root,
            text=True, encoding="utf-8", errors="replace", capture_output=True, shell=False, check=False, timeout=10,
        )
        if diff.returncode:
            errors.append(diff.stderr.strip() or "git diff failed")
        else:
            patch_text = diff.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        errors.append(str(exc))
    before_rows = {line for line in (before or "").splitlines() if len(line) > 3}
    after_rows = {line for line in (after or "").splitlines() if len(line) > 3}
    after_fingerprints = _workspace_fingerprints(root, after)
    files = sorted({line[3:] for line in after_rows})
    preexisting_files = sorted({line[3:] for line in before_rows})
    introduced_files = sorted(
        path
        for path in set(before_fingerprints) | set(after_fingerprints)
        if before_fingerprints.get(path) != after_fingerprints.get(path)
    )
    untracked = [line[3:] for line in (after or "").splitlines() if line.startswith("?? ")]
    if untracked:
        patch_text += "\n# Untracked files\n"
        for relative in untracked:
            try:
                untracked_diff = subprocess.run(
                    ["git", "diff", "--no-index", "--binary", "--", "/dev/null", relative],
                    cwd=root, text=True, encoding="utf-8", errors="replace", capture_output=True, shell=False, check=False, timeout=10,
                )
                if untracked_diff.returncode not in {0, 1}:
                    errors.append(untracked_diff.stderr.strip() or f"untracked diff failed: {relative}")
                    patch_text += f"# unavailable: {relative}\n"
                else:
                    patch_text += untracked_diff.stdout or ""
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append(f"{relative}: {exc}")
    try:
        artifacts.workspace_change_path.write_text(patch_text, encoding="utf-8")
    except OSError as exc:
        errors.append(str(exc))
    return {
        "before": before,
        "after": after,
        "changed": None if before is None or after is None else before != after,
        "files": files,
        "preexisting_files": preexisting_files,
        "introduced_files": introduced_files,
        "before_fingerprints": before_fingerprints,
        "after_fingerprints": after_fingerprints,
        "error": "; ".join(errors) or None,
    }


def _workspace_fingerprints(root: Path, status: str | None) -> dict[str, str]:
    fingerprints: dict[str, str] = {}
    for line in (status or "").splitlines():
        if len(line) <= 3:
            continue
        relative = line[3:].replace("\\", "/")
        # 第一版编辑不支持 rename；对 Git rename 的展示形式仍保留可审计 marker。
        if " -> " in relative:
            relative = relative.split(" -> ", 1)[1]
        target = root / relative
        try:
            fingerprints[relative] = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else "<missing>"
        except OSError as exc:
            fingerprints[relative] = f"<unavailable:{type(exc).__name__}>"
    return fingerprints
