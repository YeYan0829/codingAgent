from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from codeagent.runtime.repository_search import RepositorySearchError, RepositorySearchService
from codeagent.safety.path_guard import PathGuard, PathGuardError
from codeagent.safety.sensitive import SensitiveListingMode, sensitive_label
from codeagent.tools.base import PermissionLevel, ToolResult, ToolSpec
from codeagent.workspace.workspace import WorkspaceContext

MAX_OUTPUT_CHARS = 64 * 1024
MAX_READ_LINES = 1000
MAX_ENTRIES = 1000


def build_fs_tools(
    context: WorkspaceContext,
    sensitive_listing_mode: SensitiveListingMode = SensitiveListingMode.SHOW_MARKED,
    search_service: RepositorySearchService | None = None,
) -> list[ToolSpec]:
    guard = PathGuard(context.active_root)
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
                rendered = _safe_listing_item(guard, item, sensitive_listing_mode)
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
                        rendered = _safe_listing_item(guard, item, sensitive_listing_mode)
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
                    if entry.is_dir() and not entry.is_symlink() and sensitive_label(Path(entry.name)) is None:
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
            start = _bounded_int(args.get("start_line", 1), "start_line", 1, 10**9)
            end_value = args.get("end_line")
            end = _bounded_int(end_value, "end_line", start, 10**9) if end_value is not None else start + MAX_READ_LINES - 1
            if end - start + 1 > MAX_READ_LINES:
                raise ValueError(f"单次最多读取 {MAX_READ_LINES} 行")
            selected: list[str] = []
            total_lines = 0
            output_chars = 0
            output_limited = False
            with path.open("r", encoding="utf-8", errors="strict", newline=None) as stream:
                for number, line in enumerate(stream, 1):
                    total_lines = number
                    if number < start:
                        continue
                    if number > end:
                        continue
                    if output_limited:
                        continue
                    normalized = line.rstrip("\r\n")
                    if output_chars + len(normalized) + 1 > MAX_OUTPUT_CHARS:
                        output_limited = True
                        continue
                    selected.append(normalized)
                    output_chars += len(normalized) + 1
            content = "\n".join(selected)
            actual_end = start + len(selected) - 1 if selected else start - 1
            has_more_before = start > 1 and total_lines > 0
            has_more_after = actual_end < total_lines
            implicit_limit = end_value is None and has_more_after
            return ToolResult(ok=True, content=content, truncated=output_limited or implicit_limit, metadata={
                "path": path.relative_to(guard.workspace_root).as_posix(), "start_line": start,
                "end_line": actual_end, "total_lines": total_lines, "content_bytes": len(content.encode("utf-8")),
                "requested_range": {"start_line": start, "end_line": end_value},
                "returned_range": {"start_line": start, "end_line": actual_end},
                "file_total_lines": total_lines, "has_more_before": has_more_before,
                "has_more_after": has_more_after,
                "sha256": digest.hexdigest(),
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
            outcome = search.search_text(
                query=query, path=_relative_path_argument(args.get("path", ".")),
                mode=mode,
                case=_choice(args.get("case", "smart"), "case", {"sensitive", "insensitive", "smart"}),
                include=_patterns(args.get("include", []), "include"),
                exclude=_patterns(args.get("exclude", []), "exclude"),
                include_hidden=_boolean(args.get("include_hidden", False), "include_hidden"),
                context_lines=_bounded_int(args.get("context_lines", 0), "context_lines", 0, 3),
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
            })
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

    return [
        ToolSpec("list_dir", "列出显式目录内容，不应用 repository ignore。", PermissionLevel.READ, _object_schema({"path": _string("相对 workspace 的目录路径，默认 .。")}), list_dir),
        ToolSpec("show_tree", "显示显式目录的有界树形结构，不跟随 symlink。", PermissionLevel.READ, _object_schema({"path": _string("相对 workspace 的目录路径，默认 .。"), "max_depth": {"type": "integer", "minimum": 1, "maximum": 4}}), show_tree),
        ToolSpec("read_file", "严格按 UTF-8 读取文本文件。可指定最多 1000 行的范围；结果会区分请求范围、实际返回范围和文件总行数，并说明前后是否还有内容。省略范围时从第 1 行开始读取，受行数和输出大小上限约束。", PermissionLevel.READ, _object_schema({"path": _string("相对 workspace 的文本文件路径。"), "start_line": {"type": "integer", "minimum": 1, "description": "请求的起始行（包含），默认 1；它不是文件总行数。"}, "end_line": {"type": "integer", "minimum": 1, "description": "请求的结束行（包含）；省略时最多读取 1000 行。"}}, ["path"]), read_file),
        ToolSpec("search_text", "使用 ripgrep 搜索 repository 可见文件。默认 mode=literal，query 按完整普通文本匹配，`|`、括号、`.*` 等不会作为正则解释；需要正则语义时必须显式设置 mode=regex。结果 metadata 会回显实际 query_mode 和解释方式。", PermissionLevel.READ, _object_schema({"query": _string("查询内容。mode=literal 时是完整普通文本；只有 mode=regex 时才可使用 `|`、分组、字符类等正则语法。", 1, 4096), "path": _string("搜索起点，默认 .。"), "mode": {"type": "string", "enum": ["literal", "regex"], "description": "查询解释方式，默认 literal；使用任何正则语法时必须显式选择 regex。", "default": "literal"}, "case": {"type": "string", "enum": ["sensitive", "insensitive", "smart"], "description": "大小写策略，默认 smart。", "default": "smart"}, "include": _string_array(), "exclude": _string_array(), "include_hidden": {"type": "boolean"}, "context_lines": {"type": "integer", "minimum": 0, "maximum": 3}, "max_results": {"type": "integer", "minimum": 1, "maximum": 200}}, ["query"]), search_text),
        ToolSpec("find_files", "使用 ripgrep repository visibility 查找文件。", PermissionLevel.READ, _object_schema({"path": _string("搜索起点，默认 .。"), "include": _string_array(), "exclude": _string_array(), "include_hidden": {"type": "boolean"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 1000}}), find_files),
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
    path = guard.resolve(relative, reject_sensitive=False)
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


def _safe_listing_item(guard: PathGuard, path: Path, mode: SensitiveListingMode) -> str | None:
    try:
        path.resolve().relative_to(guard.workspace_root)
    except (OSError, ValueError):
        return None
    marker = "@" if path.is_symlink() else "/" if path.is_dir() else ""
    label = sensitive_label(Path(path.name))
    if label is None:
        return f"{path.name}{marker}"
    if mode == SensitiveListingMode.HIDE:
        return None
    if mode == SensitiveListingMode.REDACT_NAME:
        return f"<sensitive file hidden> [{label}, content blocked]"
    return f"{path.name}{marker} [{label}, content blocked]"


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
        raise TypeError(f"{name} 必须是最多 20 项的字符串列表")
    for pattern in value:
        raw = Path(pattern)
        if not pattern or len(pattern) > 256 or "\x00" in pattern or raw.is_absolute() or ".." in raw.parts:
            raise ValueError(f"{name} 包含非法 glob")
    return value


def _object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def _string(description: str, minimum: int | None = None, maximum: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string", "description": description}
    if minimum is not None:
        schema["minLength"] = minimum
    if maximum is not None:
        schema["maxLength"] = maximum
    return schema


def _enum(values: list[str]) -> dict[str, Any]:
    return {"type": "string", "enum": values}


def _string_array() -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string", "maxLength": 256}, "maxItems": 20}
