from __future__ import annotations

from pathlib import Path
from typing import Any

from codeagent.safety.path_guard import PathGuard, PathGuardError
from codeagent.safety.sensitive import SensitiveListingMode, sensitive_label
from codeagent.tools.base import PermissionLevel, ToolResult, ToolSpec

SKIP_DIRS = {".git", "codeagent", "__pycache__", ".pytest_cache", "node_modules", ".mypy_cache", ".ruff_cache"}
MAX_OUTPUT_CHARS = 12000
MAX_READ_CHARS = 20000


def _trim(content: str, limit: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    if len(content) <= limit:
        return content, False
    return content[:limit] + "\n...[truncated]", True


def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return b"\0" in fh.read(2048)
    except OSError:
        return True


def build_fs_tools(guard: PathGuard, sensitive_listing_mode: SensitiveListingMode = SensitiveListingMode.SHOW_MARKED) -> list[ToolSpec]:
    def list_dir(args: dict[str, Any]) -> ToolResult:
        try:
            path = guard.resolve(args.get("path", "."))
            if not path.is_dir():
                return ToolResult(ok=False, error="path is not a directory")
            rows = []
            for item in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                if not _is_safe_workspace_entry(guard, item):
                    continue
                rendered = _render_listing_item(item, sensitive_listing_mode)
                if rendered is not None:
                    rows.append(rendered)
            content, truncated = _trim("\n".join(rows))
            return ToolResult(ok=True, content=content, truncated=truncated, metadata={"path": str(path)})
        except (OSError, PathGuardError) as exc:
            return ToolResult(ok=False, error=str(exc))

    def show_tree(args: dict[str, Any]) -> ToolResult:
        try:
            root = guard.resolve(args.get("path", "."))
            max_depth = int(args.get("max_depth", 2))
            lines: list[str] = [root.name or "."]

            def walk(current: Path, depth: int, prefix: str = "") -> None:
                if depth >= max_depth:
                    return
                try:
                    entries = []
                    for item in sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                        if item.name in SKIP_DIRS:
                            continue
                        if _is_safe_workspace_entry(guard, item) and _render_listing_item(item, sensitive_listing_mode) is not None:
                            entries.append(item)
                except OSError:
                    return
                for index, entry in enumerate(entries):
                    rendered = _render_listing_item(entry, sensitive_listing_mode)
                    if rendered is None:
                        continue
                    connector = "`-- " if index == len(entries) - 1 else "|-- "
                    lines.append(f"{prefix}{connector}{rendered}")
                    if entry.is_dir() and sensitive_label(entry) is None:
                        extension = "    " if index == len(entries) - 1 else "|   "
                        walk(entry, depth + 1, prefix + extension)

            walk(root, 0)
            content, truncated = _trim("\n".join(lines))
            return ToolResult(ok=True, content=content, truncated=truncated)
        except (OSError, PathGuardError, ValueError) as exc:
            return ToolResult(ok=False, error=str(exc))

    def read_file(args: dict[str, Any]) -> ToolResult:
        try:
            path = guard.resolve(args["path"])
            if not path.is_file():
                return ToolResult(ok=False, error="path is not a file")
            if _is_binary(path):
                return ToolResult(ok=False, error="binary files are not readable")
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            start = args.get("start_line")
            end = args.get("end_line")
            if start is not None or end is not None:
                start_index = max(int(start or 1), 1) - 1
                end_index = int(end) if end is not None else len(lines)
                text = "\n".join(lines[start_index:end_index])
            truncated = len(text) > MAX_READ_CHARS
            if truncated:
                text = text[:MAX_READ_CHARS] + "\n...[truncated]"
            content, output_truncated = _trim(text)
            return ToolResult(ok=True, content=content, truncated=truncated or output_truncated, metadata={"path": str(path)})
        except (KeyError, OSError, UnicodeError, PathGuardError, ValueError) as exc:
            return ToolResult(ok=False, error=str(exc))

    def search_text(args: dict[str, Any]) -> ToolResult:
        try:
            query = str(args["query"])
            root = guard.resolve(args.get("path", "."))
            matches: list[str] = []
            for file in root.rglob("*"):
                if any(part in SKIP_DIRS for part in file.parts):
                    continue
                if not file.is_file() or _is_binary(file):
                    continue
                try:
                    rel = file.relative_to(guard.workspace_root)
                    if _blocked_by_guard(guard, rel):
                        continue
                    for number, line in enumerate(file.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                        if query in line:
                            matches.append(f"{rel}:{number}: {line.strip()[:200]}")
                            if len(matches) >= 100:
                                break
                except (OSError, ValueError):
                    continue
            content, truncated = _trim("\n".join(matches))
            return ToolResult(ok=True, content=content, truncated=truncated, metadata={"matches": len(matches)})
        except (KeyError, OSError, PathGuardError) as exc:
            return ToolResult(ok=False, error=str(exc))

    def find_files(args: dict[str, Any]) -> ToolResult:
        try:
            pattern = str(args.get("pattern", "*"))
            root = guard.resolve(args.get("path", "."))
            results = []
            for item in root.glob(pattern):
                if item.is_file():
                    rel = item.resolve().relative_to(guard.workspace_root)
                    if _blocked_by_guard(guard, rel, reject_sensitive=False):
                        continue
                    rendered = _render_listing_item(item, sensitive_listing_mode, display_name=str(rel))
                    if rendered is not None:
                        results.append(rendered)
            content, truncated = _trim("\n".join(sorted(results)))
            return ToolResult(ok=True, content=content, truncated=truncated, metadata={"matches": len(results)})
        except (OSError, PathGuardError, ValueError) as exc:
            return ToolResult(ok=False, error=str(exc))

    return [
        ToolSpec(name="list_dir", description="列出目录内容，不递归。", permission_level=PermissionLevel.READ, schema={"path": "str"}, handler=list_dir),
        ToolSpec(name="show_tree", description="显示简化目录树。", permission_level=PermissionLevel.READ, schema={"path": "str", "max_depth": "int"}, handler=show_tree),
        ToolSpec(name="read_file", description="读取文本文件，可指定行范围。", permission_level=PermissionLevel.READ, schema={"path": "str", "start_line": "int?", "end_line": "int?"}, handler=read_file),
        ToolSpec(name="search_text", description="在工作区内做简单文本搜索。", permission_level=PermissionLevel.READ, schema={"query": "str", "path": "str"}, handler=search_text),
        ToolSpec(name="find_files", description="使用 glob 查找文件。", permission_level=PermissionLevel.READ, schema={"pattern": "str", "path": "str"}, handler=find_files),
    ]


def _is_safe_workspace_entry(guard: PathGuard, path: Path) -> bool:
    try:
        rel = path.resolve().relative_to(guard.workspace_root)
    except ValueError:
        return False
    return not _blocked_by_guard(guard, rel, reject_sensitive=False)


def _render_listing_item(path: Path, mode: SensitiveListingMode, display_name: str | None = None) -> str | None:
    label = sensitive_label(path)
    name = display_name or f"{path.name}{'/' if path.is_dir() else ''}"
    if label is None:
        return name
    if mode == SensitiveListingMode.HIDE:
        return None
    if mode == SensitiveListingMode.REDACT_NAME:
        return f"<sensitive file hidden> [{label}, content blocked]"
    return f"{name} [{label}, content blocked]"


def _blocked_by_guard(guard: PathGuard, rel: Path, *, reject_sensitive: bool = True) -> bool:
    try:
        guard.resolve(rel, reject_sensitive=reject_sensitive)
        return False
    except PathGuardError:
        return True
