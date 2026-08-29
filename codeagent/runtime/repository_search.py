from __future__ import annotations

import json
import os
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codeagent.safety.path_guard import PathGuard, PathGuardError
from codeagent.safety.sensitive import is_sensitive_path
from codeagent.workspace.workspace import WorkspaceContext

SEARCH_TIMEOUT_SECONDS = 10
MAX_RAW_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_STDERR_BYTES = 8192


class RepositorySearchError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SearchOutcome:
    rows: tuple[str, ...]
    metadata: dict[str, Any]
    truncated: bool = False


class RepositorySearchService:
    """使用固定 ripgrep argv 提供有界 repository 搜索。"""

    def __init__(self, context: WorkspaceContext, executable: str | None = None) -> None:
        self.context = context
        self.guard = PathGuard(context.active_root)
        resolved = executable if executable is not None else shutil.which("rg")
        self.executable = self._trusted_executable(resolved)

    def search_text(
        self,
        *,
        query: str,
        path: str,
        mode: str,
        case: str,
        include: list[str],
        exclude: list[str],
        include_hidden: bool,
        context_lines: int,
        max_results: int,
    ) -> SearchOutcome:
        root = self._input_path(path)
        argv = [self._require_executable(), "--json", "--no-config", "--no-follow"]
        if mode == "literal":
            argv.append("--fixed-strings")
        argv.append({"sensitive": "--case-sensitive", "insensitive": "--ignore-case", "smart": "--smart-case"}[case])
        argv.extend(self._glob_argv(include, exclude))
        if include_hidden:
            argv.append("--hidden")
        if context_lines:
            argv.extend(["--context", str(context_lines)])
        argv.extend(["--", query, self._relative_argument(root)])

        record_limit = max_results * (2 * context_lines + 4) + 100
        messages, returncode, stderr, stopped = self._run_records(argv, delimiter=b"\n", record_limit=record_limit)
        if returncode not in {0, 1} and not stopped:
            code = "invalid_arguments" if "regex parse error" in stderr.lower() else "command_failed"
            raise RepositorySearchError(code, stderr or f"ripgrep exited with {returncode}")

        rows: list[str] = []
        files: set[str] = set()
        for raw in messages:
            if not raw:
                continue
            try:
                message = json.loads(raw)
                if message.get("type") not in {"match", "context"}:
                    continue
                data = message["data"]
                relative = self._safe_result_path(_json_text(data["path"]))
                line = _json_text(data["lines"]).rstrip("\r\n")
                line_number = int(data.get("line_number") or 0)
                submatches = data.get("submatches") or []
                column = int(submatches[0]["start"]) + 1 if submatches else 1
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RepositorySearchError("internal_error", f"无法解析 ripgrep JSON 输出: {exc}") from exc
            if relative is None:
                continue
            prefix = ">" if message["type"] == "match" else "-"
            rows.append(f"{prefix} {relative}:{line_number}:{column}: {line}")
            if message["type"] == "match":
                files.add(relative)

        truncated = stopped or len(rows) > max_results
        rows = rows[:max_results]
        return SearchOutcome(
            tuple(rows),
            {
                "match_count": sum(row.startswith("> ") for row in rows),
                "files_with_matches": len(files),
                "limit_reason": "max_results" if truncated else None,
                "search_root": self._relative_argument(root),
                "mode": mode,
                "case": case,
            },
            truncated,
        )

    def find_files(
        self,
        *,
        path: str,
        include: list[str],
        exclude: list[str],
        include_hidden: bool,
        max_results: int,
    ) -> SearchOutcome:
        root = self._input_path(path)
        argv = [self._require_executable(), "--files", "--null", "--no-config", "--no-follow"]
        argv.extend(self._glob_argv(include, exclude))
        if include_hidden:
            argv.append("--hidden")
        argv.append(self._relative_argument(root))
        records, returncode, stderr, stopped = self._run_records(argv, delimiter=b"\0", record_limit=max_results + 1)
        if returncode not in {0, 1} and not stopped:
            raise RepositorySearchError("command_failed", stderr or f"ripgrep exited with {returncode}")
        rows = []
        for raw in records:
            if not raw:
                continue
            try:
                rendered = raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise RepositorySearchError("internal_error", "ripgrep 返回了非 UTF-8 路径") from exc
            relative = self._safe_result_path(rendered)
            if relative is not None:
                rows.append(relative)
        unique = sorted(set(rows))
        truncated = stopped or len(unique) > max_results
        unique = unique[:max_results]
        return SearchOutcome(
            tuple(unique),
            {
                "file_count": len(unique),
                "limit_reason": "max_results" if truncated else None,
                "search_root": self._relative_argument(root),
            },
            truncated,
        )

    def _run_records(
        self, argv: list[str], *, delimiter: bytes, record_limit: int
    ) -> tuple[list[bytes], int | None, str, bool]:
        records: list[bytes] = []
        buffer = bytearray()
        total = 0
        stopped = False
        deadline = time.monotonic() + SEARCH_TIMEOUT_SECONDS
        with tempfile.TemporaryFile() as stderr_file:
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=self.context.active_root,
                    env=_search_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=stderr_file,
                    shell=False,
                    start_new_session=True,
                )
            except OSError as exc:
                raise RepositorySearchError("tool_unavailable", f"无法启动 ripgrep: {exc}") from exc
            assert process.stdout is not None
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        _terminate_group(process)
                        raise RepositorySearchError("timed_out", "ripgrep 搜索超时")
                    events = selector.select(timeout=remaining)
                    if not events:
                        if process.poll() is not None:
                            break
                        continue
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_RAW_OUTPUT_BYTES:
                        stopped = True
                        _terminate_group(process)
                        break
                    buffer.extend(chunk)
                    while True:
                        index = buffer.find(delimiter)
                        if index < 0:
                            break
                        records.append(bytes(buffer[:index]))
                        del buffer[: index + len(delimiter)]
                        if len(records) >= record_limit:
                            stopped = True
                            _terminate_group(process)
                            break
                    if stopped:
                        break
                if buffer and not stopped:
                    records.append(bytes(buffer))
                returncode = process.wait(timeout=2) if process.poll() is None else process.returncode
            except subprocess.TimeoutExpired:
                _terminate_group(process)
                raise RepositorySearchError("timed_out", "ripgrep 终止超时")
            finally:
                selector.close()
                process.stdout.close()
            stderr_file.seek(0)
            stderr = stderr_file.read(MAX_STDERR_BYTES).decode("utf-8", errors="replace").strip()
        return records, returncode, stderr, stopped

    def _input_path(self, value: str) -> Path:
        try:
            raw = Path(value)
            if raw.is_absolute() or ".." in raw.parts or "\x00" in value:
                raise PathGuardError("path 必须是 workspace-relative 且不能包含父目录跳转")
            candidate = self.guard.resolve(raw)
        except (OSError, PathGuardError) as exc:
            raise RepositorySearchError("path_rejected", str(exc)) from exc
        if not candidate.exists():
            raise RepositorySearchError("not_found", f"path 不存在: {value}")
        if _has_symlink_component(self.context.active_root, raw):
            raise RepositorySearchError("path_rejected", "搜索路径不能经过 symlink")
        return candidate

    def _safe_result_path(self, value: str) -> str | None:
        try:
            raw = Path(value)
            candidate = self.guard.resolve(raw, reject_sensitive=False)
            relative = candidate.relative_to(self.context.active_root)
            if is_sensitive_path(relative) or _has_symlink_component(self.context.active_root, raw):
                return None
            return relative.as_posix()
        except (OSError, ValueError, PathGuardError):
            return None

    def _relative_argument(self, path: Path) -> str:
        relative = path.relative_to(self.context.active_root).as_posix()
        return relative or "."

    def _require_executable(self) -> str:
        if self.executable is None:
            raise RepositorySearchError("tool_unavailable", "required dependency ripgrep (rg) 不可用")
        return self.executable

    def _trusted_executable(self, value: str | None) -> str | None:
        if not value:
            return None
        resolved = Path(value).expanduser().resolve()
        try:
            resolved.relative_to(self.context.active_root)
        except ValueError:
            return str(resolved)
        return None

    @staticmethod
    def _glob_argv(include: list[str], exclude: list[str]) -> list[str]:
        argv: list[str] = []
        for pattern in include:
            argv.extend(["--glob", pattern])
        for pattern in exclude:
            argv.extend(["--glob", f"!{pattern}"])
        return argv


def _json_text(value: dict[str, Any]) -> str:
    text = value.get("text")
    if isinstance(text, str):
        return text
    raise ValueError("ripgrep JSON field is not UTF-8 text")


def _has_symlink_component(root: Path, relative: Path) -> bool:
    current = root
    for part in relative.parts:
        if part in {"", "."}:
            continue
        current = current / part
        if current.is_symlink():
            return True
    return False


def _search_environment() -> dict[str, str]:
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "XDG_CONFIG_HOME"}
    return {name: value for name, value in os.environ.items() if name in allowed}


def _terminate_group(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=1)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
