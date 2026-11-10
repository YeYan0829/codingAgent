from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from codeagent.safety.path_guard import PathGuard, PathGuardError
from codeagent.tools.base import PermissionLevel, ToolResult, ToolSpec
from codeagent.workspace.workspace import WorkspaceContext

MAX_STATUS_ENTRIES = 200
MAX_STATUS_BYTES = 2 * 1024 * 1024
MAX_DIFF_BYTES = 64 * 1024
MAX_STDERR_BYTES = 8192
GIT_TIMEOUT_SECONDS = 10


class GitReadService:
    def __init__(self, context: WorkspaceContext, executable: str | None = None) -> None:
        self.context = context
        self.guard = PathGuard(context.active_root)
        candidate = executable if executable is not None else shutil.which("git")
        self.executable = _trusted_executable(candidate, context.active_root)

    def status(self) -> ToolResult:
        try:
            stdout, stderr, returncode, truncated = self._run(
                ["status", "--porcelain=v1", "-z", "--untracked-files=all"], MAX_STATUS_BYTES
            )
            if returncode:
                return _git_failure(stderr, returncode)
            entries = _parse_porcelain_v1_z(stdout)
            limited = len(entries) > MAX_STATUS_ENTRIES
            entries = entries[:MAX_STATUS_ENTRIES]
            lines = []
            metadata_entries = []
            for entry in entries:
                rendered = f"{entry['index']}{entry['worktree']} {entry['path']}"
                if entry["original_path"] is not None:
                    rendered = f"{entry['index']}{entry['worktree']} {entry['original_path']} -> {entry['path']}"
                lines.append(rendered)
                metadata_entries.append(entry)
            return ToolResult(
                ok=True,
                content="\n".join(lines),
                truncated=truncated or limited,
                metadata={"entries": metadata_entries, "entry_count": len(metadata_entries)},
            )
        except GitReadError as exc:
            return ToolResult(ok=False, error=str(exc), error_code=exc.code)

    def diff(self, args: dict[str, Any], *, stat: bool = False) -> ToolResult:
        try:
            path = _relative_path(args.get("path", "."), self.guard)
            context_lines = _bounded_int(args.get("context_lines", 3), "context_lines", 0, 10)
            command = ["diff", "--no-ext-diff", "--no-textconv"]
            command.append("--stat" if stat else f"--unified={context_lines}")
            command.extend(["--", path])
            stdout, stderr, returncode, truncated = self._run(command, MAX_DIFF_BYTES)
            if returncode:
                return _git_failure(stderr, returncode)
            text = stdout.decode("utf-8", errors="replace")
            if truncated:
                text += "\n...[truncated]"
            return ToolResult(ok=True, content=text.rstrip(), truncated=truncated, metadata={
                "path": path, "content_bytes": len(stdout), "returncode": returncode,
            })
        except GitReadError as exc:
            return ToolResult(ok=False, error=str(exc), error_code=exc.code)
        except PathGuardError as exc:
            return ToolResult(ok=False, error=str(exc), error_code="path_rejected")
        except (TypeError, ValueError) as exc:
            return ToolResult(ok=False, error=str(exc), error_code="invalid_arguments")

    def _run(self, arguments: list[str], stdout_limit: int) -> tuple[bytes, str, int, bool]:
        if self.executable is None:
            raise GitReadError("tool_unavailable", "required dependency git 不可用")
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            try:
                process = subprocess.Popen(
                    [self.executable, *arguments], cwd=self.context.active_root,
                    stdin=subprocess.DEVNULL, stdout=stdout_file, stderr=stderr_file,
                    shell=False, start_new_session=True,
                )
                returncode = process.wait(timeout=GIT_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait()
                raise GitReadError("timed_out", "Git 读取命令超时") from exc
            except OSError as exc:
                raise GitReadError("tool_unavailable", f"无法启动 Git: {exc}") from exc
            stdout_file.seek(0)
            stdout = stdout_file.read(stdout_limit + 1)
            truncated = len(stdout) > stdout_limit
            stdout = stdout[:stdout_limit]
            stderr_file.seek(0)
            stderr = stderr_file.read(MAX_STDERR_BYTES).decode("utf-8", errors="replace").strip()
        return stdout, stderr, returncode, truncated


class GitReadError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def build_git_tools(context: WorkspaceContext) -> list[ToolSpec]:
    service = GitReadService(context)
    empty_schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    diff_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对 workspace 的路径，默认 .。"},
            "context_lines": {"type": "integer", "minimum": 0, "maximum": 10},
        },
        "required": [],
        "additionalProperties": False,
    }
    return [
        ToolSpec("git_status", "读取稳定、机器解析的 Git working tree 状态。", PermissionLevel.READ, empty_schema, lambda args: service.status()),
        ToolSpec("git_diff", "读取当前 working tree 的实际 unstaged Git diff。", PermissionLevel.READ, diff_schema, service.diff),
        ToolSpec("git_diff_stat", "读取当前 working tree 的 Git diff stat。", PermissionLevel.READ, diff_schema, lambda args: service.diff(args, stat=True)),
    ]


def _parse_porcelain_v1_z(value: bytes) -> list[dict[str, str | None]]:
    records = value.split(b"\0")
    if records and records[-1] == b"":
        records.pop()
    entries: list[dict[str, str | None]] = []
    index = 0
    while index < len(records):
        record = records[index]
        if len(record) < 4 or record[2:3] != b" ":
            raise GitReadError("internal_error", "无法解析 git status porcelain v1 -z 输出")
        try:
            status = record[:2].decode("ascii")
            path = record[3:].decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise GitReadError("internal_error", "Git status 包含非 UTF-8 path") from exc
        original_path = None
        if status[0] in {"R", "C"} or status[1] in {"R", "C"}:
            index += 1
            if index >= len(records):
                raise GitReadError("internal_error", "Git rename status 缺少 original path")
            try:
                original_path = records[index].decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise GitReadError("internal_error", "Git status 包含非 UTF-8 path") from exc
        entries.append({"index": status[0], "worktree": status[1], "path": path, "original_path": original_path})
        index += 1
    return entries


def _relative_path(value: Any, guard: PathGuard) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("path 必须是非空字符串")
    raw = Path(value)
    if raw.is_absolute() or ".." in raw.parts:
        raise PathGuardError("path 必须是 workspace-relative 且不能包含父目录跳转")
    resolved = guard.resolve(raw, reject_sensitive=False)
    return resolved.relative_to(guard.workspace_root).as_posix() or "."


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须是 {minimum}..{maximum} 的整数")
    return value


def _trusted_executable(value: str | None, workspace: Path) -> str | None:
    if not value:
        return None
    resolved = Path(value).expanduser().resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError:
        return str(resolved)
    return None


def _git_failure(stderr: str, returncode: int) -> ToolResult:
    if "not a git repository" in stderr:
        message = f"git command executed, but workspace is not a valid Git repository: {stderr}"
    else:
        message = stderr or f"Git exited with {returncode}"
    return ToolResult(ok=False, error=message, error_code="command_failed", metadata={"returncode": returncode})
