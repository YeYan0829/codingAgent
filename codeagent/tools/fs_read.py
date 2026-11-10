from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from codeagent.runtime.repository_search import RepositorySearchError, RepositorySearchService
from codeagent.safety.path_guard import PathGuard, PathGuardError
from codeagent.tools.output import ToolOutput as ToolResult
from codeagent.workspace.workspace import WorkspaceContext

MAX_OUTPUT_CHARS = 64 * 1024
MAX_READ_LINES = 1000
MAX_ENTRIES = 1000
MAX_SEARCH_CONTEXT_LINES = 10


def read_fragment(path, relative, offset, sha256, limit=MAX_OUTPUT_CHARS):
    """长行按原始 UTF-8 字节续读；游标由文件 hash 约束，不拼接新旧内容。"""
    with path.open("rb") as stream:
        remaining, line = offset, 1
        while remaining:
            chunk = stream.read(min(remaining, 65536))
            if not chunk:
                raise ValueError("start_byte 超出文件范围")
            line += chunk.count(b"\n")
            remaining -= len(chunk)
        raw = stream.read(limit + 4)
    # 只容许截断尾部字符；非法起始游标或中间编码错误仍拒绝。
    data = raw[:limit]
    while True:
        try:
            content = data.decode("utf-8")
            break
        except UnicodeDecodeError as exc:
            if exc.reason != "unexpected end of data":
                raise
            data = data[:exc.start]
    next_byte = offset + len(data)
    more = next_byte < path.stat().st_size
    end_line = line + content.count("\n") - int(content.endswith("\n")) if content else line
    return ToolResult(ok=True, content=content, truncated=more, metadata={
        "path": relative, "sha256": sha256, "start_line": line, "end_line": end_line,
        "returned_range": {"start_line": line, "end_line": end_line} if content else None, "line_fragment": True,
        "start_byte": offset, "next_start_byte": next_byte if more else None,
        "content_bytes": len(data), "has_more_after": more,
        "read_hint": "续读使用 path、start_byte=next_start_byte 和 expected_sha256=sha256，不混用行范围。"})


def build_fs_handlers(
    context: WorkspaceContext,
    search_service: RepositorySearchService | None = None,
) -> dict:
    guard = context.path_guard()
    search = search_service or RepositorySearchService(context)

    def list_dir(args: dict[str, Any]) -> ToolResult:
        try:
            path, relative = _directory(guard, args.get("path", "."))
            rows: list[str] = []
            limited = False
            for item in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                if len(rows) >= MAX_ENTRIES:
                    limited = True
                    break
                rendered = _safe_listing_item(guard, item)
                if rendered is not None:
                    rows.append(rendered)
            content, output_truncated = _trim("\n".join(rows))
            truncated = output_truncated or limited
            return ToolResult(ok=True, content=content, truncated=truncated, metadata={
                "path": relative, "entry_count": len(rows),
                "limit_reason": "entries" if limited else "output_bytes" if output_truncated else None,
            })
        except FileNotFoundError as exc:
            return _failure("not_found", str(exc))
        except PathGuardError as exc:
            return _failure("path_rejected", str(exc))
        except ValueError as exc:
            return _failure("invalid_arguments", str(exc))
        except (OSError, TypeError) as exc:
            return _failure("internal_error", str(exc))

    def show_tree(args: dict[str, Any]) -> ToolResult:
        try:
            root, relative = _directory(guard, args.get("path", "."))
            max_depth = _bounded_int(args.get("max_depth", 2), "max_depth", 1, 4)
            lines: list[str] = [relative if relative != "." else root.name or "."]
            unreadable = 0
            limited = False

            def walk(current: Path, depth: int, prefix: str = "") -> None:
                nonlocal unreadable, limited
                if depth >= max_depth or limited:
                    return
                try:
                    entries = []
                    for item in sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                        rendered = _safe_listing_item(guard, item)
                        if rendered is not None:
                            entries.append((item, rendered))
                except OSError:
                    unreadable += 1
                    return
                for index, (entry, rendered) in enumerate(entries):
                    if len(lines) >= MAX_ENTRIES:
                        limited = True
                        return
                    last = index == len(entries) - 1
                    lines.append(f"{prefix}{'`-- ' if last else '|-- '}{rendered}")
                    if entry.is_dir() and not entry.is_symlink():
                        walk(entry, depth + 1, prefix + ("    " if last else "|   "))

            walk(root, 0)
            content, output_truncated = _trim("\n".join(lines))
            truncated = limited or output_truncated or unreadable > 0
            return ToolResult(ok=True, content=content, truncated=truncated, metadata={
                "path": relative, "entry_count": max(0, len(lines) - 1), "unreadable_count": unreadable,
                "limit_reason": "entries" if limited else "output_bytes" if output_truncated else None,
            })
        except FileNotFoundError as exc:
            return _failure("not_found", str(exc))
        except PathGuardError as exc:
            return _failure("path_rejected", str(exc))
        except (TypeError, ValueError) as exc:
            return _failure("invalid_arguments", str(exc))
        except OSError as exc:
            return _failure("internal_error", str(exc))

    def read_file(args: dict[str, Any]) -> ToolResult:
        try:
            value = _relative_path_argument(args.get("path"), required=True)
            if _has_symlink_component(guard.workspace_root, Path(value)):
                return _failure("path_rejected", "读取路径不能经过 symlink")
            path = guard.resolve(value)
            if not path.exists():
                return _failure("not_found", f"path 不存在: {value}")
            if path.is_symlink() or not path.is_file():
                return _failure("unsupported_file_type", "path 不是可读取的普通文件")
            if _has_nul(path):
                return _failure("unsupported_file_type", "binary files are not readable")
            digest = hashlib.sha256()
            with path.open("rb") as binary_stream:
                for chunk in iter(lambda: binary_stream.read(64 * 1024), b""):
                    digest.update(chunk)
            if args.get("expected_sha256") is not None and args["expected_sha256"] != digest.hexdigest():
                return _failure("cursor_stale", "文件hash与提供的版本不符；重新读取当前文件")
            if "start_byte" in args:
                if "start_line" in args or "end_line" in args:
                    raise ValueError("字节续读不能混用行范围")
                if args.get("expected_sha256") != digest.hexdigest():
                    return _failure("cursor_stale", "文件 hash 与续读版本不符；重新读取目标文件，不拼接旧片段")
                offset = _bounded_int(args["start_byte"], "start_byte", 0, path.stat().st_size)
                return read_fragment(path, value, offset, digest.hexdigest())
            start = _bounded_int(args.get("start_line", 1), "start_line", 1, 10**9)
            end_value = args.get("end_line")
            end = _bounded_int(end_value, "end_line", start, 10**9) if end_value is not None else start + MAX_READ_LINES - 1
            range_limited = end - start + 1 > MAX_READ_LINES
            end = min(end, start + MAX_READ_LINES - 1)
            selected: list[str] = []
            total_lines = 0
            output_chars = 0
            output_limited = False
            start_byte, byte_cursor = 0, 0
            with path.open("r", encoding="utf-8", errors="strict", newline="") as stream:
                for number, line in enumerate(stream, 1):
                    if number == start:
                        start_byte = byte_cursor
                    byte_cursor += len(line.encode("utf-8"))
                    total_lines = number
                    if number < start:
                        continue
                    if number > end:
                        continue
                    if output_limited:
                        continue
                    normalized = line.rstrip("\r\n")
                    if output_chars + len(normalized.encode("utf-8")) + 1 > MAX_OUTPUT_CHARS:
                        output_limited = True
                        continue
                    selected.append(normalized)
                    output_chars += len(normalized.encode("utf-8")) + 1
            content = "\n".join(selected)
            if output_limited and not selected:
                with path.open("rb") as stream:
                    for _ in range(start - 1):
                        stream.readline()
                    offset = stream.tell()
                return read_fragment(path, value, offset, digest.hexdigest())
            actual_end = start + len(selected) - 1 if selected else start - 1
            has_more_before = start > 1 and total_lines > 0
            has_more_after = actual_end < total_lines
            implicit_limit = end_value is None and has_more_after
            return ToolResult(ok=True, content=content, truncated=output_limited or implicit_limit or (range_limited and has_more_after), metadata={
                "path": path.relative_to(guard.workspace_root).as_posix(), "start_line": start,
                "end_line": actual_end, "total_lines": total_lines, "content_bytes": len(content.encode("utf-8")),
                "requested_range": {"start_line": start, "end_line": end_value},
                "returned_range": {"start_line": start, "end_line": actual_end} if selected else None,
                "file_total_lines": total_lines, "has_more_before": has_more_before,
                "has_more_after": has_more_after,
                "next_start_line": actual_end + 1 if has_more_after else None,
                "sha256": digest.hexdigest(),
                "start_byte": start_byte,
            })
        except UnicodeDecodeError:
            return _failure("unsupported_text_encoding", "文件不是合法 UTF-8 文本")
        except KeyError as exc:
            return _failure("invalid_arguments", f"缺少参数: {exc}")
        except PathGuardError as exc:
            return _failure("path_rejected", str(exc))
        except (TypeError, ValueError) as exc:
            return _failure("invalid_arguments", str(exc))
        except OSError as exc:
            return _failure("internal_error", str(exc))

    def search_text(args: dict[str, Any]) -> ToolResult:
        try:
            query = args.get("query")
            if not isinstance(query, str) or not 1 <= len(query) <= 4096:
                raise ValueError("query 必须是 1..4096 字符的字符串")
            mode = _choice(args.get("mode", "literal"), "mode", {"literal", "regex"})
            case = _choice(args.get("case", "smart"), "case", {"sensitive", "insensitive", "smart"})
            requested_context = args.get("context_lines", 0)
            if isinstance(requested_context, bool) or not isinstance(requested_context, int) or requested_context < 0:
                raise ValueError("context_lines 必须是非负整数")
            # 超过上限时按上限执行并说明，不让一次可用的搜索因上下文行数被拒绝。
            context_lines = min(requested_context, MAX_SEARCH_CONTEXT_LINES)
            outcome = search.search_text(
                query=query, path=_relative_path_argument(args.get("path", ".")),
                mode=mode,
                case=case,
                include=_patterns(args.get("include", []), "include"),
                exclude=_patterns(args.get("exclude", []), "exclude"),
                include_hidden=_boolean(args.get("include_hidden", False), "include_hidden"),
                context_lines=context_lines,
                max_results=_bounded_int(args.get("max_results", 100), "max_results", 1, 200),
            )
            result = _outcome_result(outcome)
            result.metadata.update({
                "query": query,
                "query_mode": mode,
                "query_interpretation": (
                    "exact literal text; regex metacharacters are not interpreted"
                    if mode == "literal" else "ripgrep regular expression"
                ),
                "case_effective": case if case != "smart" else ("sensitive" if query != query.lower() else "insensitive"),
            })
            if context_lines != requested_context:
                result.metadata["context_lines_applied"] = context_lines
                result.metadata["context_lines_note"] = f"context_lines 上限为 {MAX_SEARCH_CONTEXT_LINES}，已按上限返回"
            if result.metadata.get("match_count") == 0 and result.metadata["case_effective"] == "sensitive" and case == "smart":
                result.metadata["search_hint"] = "smart 模式下查询含大写字母，按大小写敏感匹配；可用 case=insensitive 重试"
            return result
        except RepositorySearchError as exc:
            return _failure(exc.code, str(exc))
        except (TypeError, ValueError, PathGuardError) as exc:
            return _failure("invalid_arguments", str(exc))

    def find_files(args: dict[str, Any]) -> ToolResult:
        try:
            outcome = search.find_files(
                path=_relative_path_argument(args.get("path", ".")),
                include=_patterns(args.get("include", []), "include"),
                exclude=_patterns(args.get("exclude", []), "exclude"),
                include_hidden=_boolean(args.get("include_hidden", False), "include_hidden"),
                max_results=_bounded_int(args.get("max_results", 200), "max_results", 1, 1000),
            )
            return _outcome_result(outcome)
        except RepositorySearchError as exc:
            return _failure(exc.code, str(exc))
        except (TypeError, ValueError, PathGuardError) as exc:
            return _failure("invalid_arguments", str(exc))

    return dict(list_dir=list_dir, show_tree=show_tree, read_file=read_file,
                search_text=search_text, find_files=find_files)


def build_fs_tools(context: WorkspaceContext, search_service=None) -> list:
    from codeagent.tools.base import PermissionLevel, ToolSpec
    from codeagent.tools.inputs import DirectoryInput, TreeInput, ReadFileInput, SearchTextInput, FindFilesInput
    handlers = build_fs_handlers(context, search_service)
    list_dir, show_tree, read_file, search_text, find_files = (handlers[n] for n in
        ("list_dir", "show_tree", "read_file", "search_text", "find_files"))
    return [
        ToolSpec.from_model("list_dir", "列出当前项目内显式目录的直接子项，包含隐藏项，不应用repository ignore，不跟随链接读取内容。最多1000项，过长输出截断；用子目录或find_files缩小范围。", PermissionLevel.READ, DirectoryInput, list_dir),
        ToolSpec.from_model("show_tree", "显示当前项目内目录树，默认深度2，不跟随符号链接，最多1000个条目。受禁止路径和输出大小限制；截断时用子目录继续调查。", PermissionLevel.READ, TreeInput, show_tree),
        ToolSpec.from_model("read_file", "读取当前项目内UTF-8普通文本，不读取符号链接或二进制。默认从第1行开始，每页最多1000行/64KiB；较大范围返回有界页。metadata.file_total_lines是文件总行数，returned_range是本次实际可见行，无可见内容时为null。按has_more_after和next_start_line续读；超长行返回片段，使用next_start_byte和sha256续读，不混用行范围。字节模式必须提供expected_sha256；文件变化时拒绝旧游标。", PermissionLevel.READ, ReadFileInput, read_file),
        ToolSpec.from_model("search_text", "使用ripgrep搜索当前项目可见文本，返回匹配行及路径、行号、列号，可带上下文。默认mode=literal，query视为完整普通文本；使用正则必须显式选择regex。默认遵守repository ignore；显式include按ripgrep规则可覆盖ignore/隐藏筛选，但不能绕过禁止路径。max_results限制匹配/上下文记录总行数；过多结果用query/path/include缩小范围。metadata回显query_mode和实际解释。", PermissionLevel.READ, SearchTextInput, search_text),
        ToolSpec.from_model("find_files", "按glob查找当前项目文件；include是模式数组，例如 **/*.py（置于数组中），省略则查找可见文件。基于ripgrep repository visibility，默认遵守ignore，不跟随链接；显式include可覆盖ignore/隐藏筛选，禁止路径始终生效。返回相对路径，按路径排序；默认200项，最多1000项，截断时收紧include/path。", PermissionLevel.READ, FindFilesInput, find_files),
    ]


def _outcome_result(outcome: Any) -> ToolResult:
    content, output_truncated = _trim("\n".join(outcome.rows))
    metadata = dict(outcome.metadata)
    if output_truncated:
        metadata["limit_reason"] = "output_bytes"
    return ToolResult(ok=True, content=content, truncated=outcome.truncated or output_truncated, metadata=metadata)


def _failure(code: str, message: str) -> ToolResult:
    return ToolResult(ok=False, error=message, error_code=code)


def _trim(content: str, limit: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    encoded = content.encode("utf-8")
    if len(encoded) <= limit:
        return content, False
    return encoded[:limit].decode("utf-8", errors="ignore") + "\n...[truncated]", True


def _directory(guard: PathGuard, value: Any) -> tuple[Path, str]:
    relative = _relative_path_argument(value)
    if _has_symlink_component(guard.workspace_root, Path(relative)):
        raise PathGuardError("目录路径不能经过 symlink")
    path = guard.resolve(relative)
    if not path.exists():
        raise FileNotFoundError(f"path 不存在: {relative}")
    if not path.is_dir():
        raise ValueError("path is not a directory")
    return path, path.relative_to(guard.workspace_root).as_posix() or "."


def _relative_path_argument(value: Any, *, required: bool = False) -> str:
    if value is None and required:
        raise KeyError("path")
    if value is None:
        return "."
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("path 必须是非空字符串")
    raw = Path(value)
    if raw.is_absolute() or ".." in raw.parts:
        raise PathGuardError("path 必须是 workspace-relative 且不能包含父目录跳转")
    return raw.as_posix()


def _safe_listing_item(guard: PathGuard, path: Path) -> str | None:
    try:
        guard.resolve(path)
    except (OSError, ValueError):
        return None
    marker = "@" if path.is_symlink() else "/" if path.is_dir() else ""
    return f"{path.name}{marker}"


def _has_nul(path: Path) -> bool:
    with path.open("rb") as stream:
        return b"\0" in stream.read(8192)


def _has_symlink_component(root: Path, relative: Path) -> bool:
    current = root
    for part in relative.parts:
        if part in {"", "."}:
            continue
        current = current / part
        if current.is_symlink():
            return True
    return False


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} 必须是整数")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须在 {minimum}..{maximum} 范围内")
    return value


def _choice(value: Any, name: str, allowed: set[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{name} 必须是 {sorted(allowed)} 之一")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} 必须是 boolean")
    return value


def _patterns(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 20 or not all(isinstance(item, str) for item in value):
        raise TypeError(f'{name} 必须是最多 20 项的字符串列表，例如 ["*.py"]；不要传单个字符串')
    for pattern in value:
        raw = Path(pattern)
        if not pattern or len(pattern) > 256 or "\x00" in pattern or raw.is_absolute() or ".." in raw.parts:
            raise ValueError(f"{name} 包含非法 glob")
    return value
