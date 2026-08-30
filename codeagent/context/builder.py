from __future__ import annotations

import json
import hashlib
import subprocess
from importlib.resources import files
from pathlib import Path
from typing import Any

from codeagent.context.models import ActiveCodeSlice, BudgetReport, ContextBudgetExceeded, ModelCapabilities, RuntimeSnapshot, ToolExchange, UserTurnView
from codeagent.context.projector import ProjectionError, project_events
from codeagent.safety.path_guard import PathGuard, PathGuardError
from codeagent.session.store import SessionStore
from codeagent.tools.registry import ToolRegistry
from codeagent.workspace.workspace import WorkspaceContext


class ConservativeTokenEstimator:
    def estimate(self, value: Any) -> int:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return max(1, (len(text.encode("utf-8")) + 2) // 3 + 8)


class ContextManager:
    def __init__(self, session_store: SessionStore, tool_registry: ToolRegistry | None = None) -> None:
        self.session_store = session_store
        self.tool_registry = tool_registry
        self.estimator = ConservativeTokenEstimator()
        self.last_budget_report: BudgetReport | None = None

    def build(self, *, model_capabilities: ModelCapabilities | None = None,
              current_turn_transient_messages: tuple[dict, ...] = ()) -> list[dict]:
        caps = model_capabilities or ModelCapabilities()
        turns = project_events(self.session_store.read_events())
        context = self.session_store.workspace_context()
        snapshot = self._runtime_snapshot(context)
        active_code = self._active_code(turns, context, caps.active_code_budget, snapshot)
        instructions = [
            {"role": "system", "content": self._read_prompt("system.md")},
            {"role": "system", "content": self._read_prompt("tool_policy.md")},
            {"role": "system", "content": self._render_snapshot(snapshot, active_code)},
        ]
        schemas = self.tool_registry.as_model_tools() if self.tool_registry else []
        retained, dropped = list(turns), []
        while True:
            messages = instructions + self._render_turns(retained) + list(current_turn_transient_messages)
            estimate = self.estimator.estimate(messages) + self.estimator.estimate([tool.model_dump() for tool in schemas])
            if estimate <= caps.usable_input_budget:
                self.last_budget_report = BudgetReport(estimate, caps.usable_input_budget, tuple(dropped), ("whole_turn_eviction",) if dropped else ())
                return messages
            removable = next((turn for turn in retained[:-1] if turn.completed), None)
            if removable is None:
                report = BudgetReport(estimate, caps.usable_input_budget, tuple(dropped), ("minimum_set",))
                self.last_budget_report = report
                raise ContextBudgetExceeded(report)
            retained.remove(removable)
            dropped.append(removable.turn_id)

    def _render_turns(self, turns: list[UserTurnView]) -> list[dict]:
        messages: list[dict] = []
        for index, turn in enumerate(turns):
            messages.append({"role": "user", "content": turn.user_message})
            if index == len(turns) - 1 or not turn.completed:
                execution_indexes = [step_index for step_index, item in enumerate(turn.model_steps) if item.exchanges]
                latest_execution_index = execution_indexes[-1] if execution_indexes else -1
                recent_preamble_indexes = set(execution_indexes[-4:])
                for step_index, step in enumerate(turn.model_steps):
                    if step.protocol_error:
                        messages.append({"role": "system", "content": self._protocol_error_residue(step.protocol_error)})
                        continue
                    if not step.exchanges:
                        continue
                    messages.append({"role": "assistant", "content": step.message if step_index in recent_preamble_indexes else None, "tool_calls": [
                        {"id": item.call_id, "type": "function", "function": {"name": item.tool_name,
                         "arguments": json.dumps(item.arguments, ensure_ascii=False)}} for item in step.exchanges
                    ]})
                    for exchange in step.exchanges:
                        if exchange.terminal_kind is None:
                            raise ProjectionError(f"incomplete tool exchange: {exchange.call_id}")
                        messages.append({"role": "tool", "tool_call_id": exchange.call_id,
                                         "content": json.dumps(
                                             self._bounded_result(exchange) if step_index == latest_execution_index
                                             else self._historical_residue(exchange), ensure_ascii=False)})
            else:
                residues = [self._historical_residue(exchange) for step in turn.model_steps for exchange in step.exchanges]
                if residues:
                    messages.append({"role": "system", "content": "该已完成轮次的历史执行记录（正文已省略）：\n" +
                                     json.dumps(residues, ensure_ascii=False)})
            if turn.final_message is not None:
                messages.append({"role": "assistant", "content": turn.final_message})
        return messages

    @staticmethod
    def _bounded_result(exchange: ToolExchange, limit: int = 16_000) -> dict[str, Any]:
        result = dict(exchange.result or {})
        content = str(result.get("content") or "")
        if len(content.encode("utf-8")) > limit:
            result["content"] = content.encode("utf-8")[:limit].decode("utf-8", "ignore") + "\n...[context truncated]"
            result["truncated"] = True
            result.setdefault("metadata", {})["context_limit_bytes"] = limit
        return result

    @staticmethod
    def _protocol_error_residue(payload: dict[str, Any]) -> str:
        return ("上一模型响应无法形成合法 ToolCall；工具未执行，workspace 未修改。"
                f" tool={payload.get('tool', '')} error={payload.get('message', '')}"
                f" line={payload.get('line')} column={payload.get('column')}")

    def _historical_residue(self, exchange: ToolExchange) -> dict[str, Any]:
        result = exchange.result or {}
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        kept_metadata = {key: metadata[key] for key in (
            "path", "start_line", "end_line", "total_lines", "file_total_lines", "requested_range",
            "returned_range", "has_more_before", "has_more_after", "sha256", "match_count", "file_count", "entry_count",
            "candidate_revision", "subject_tree", "before_tree", "after_tree", "changed_paths", "workspace_changed",
            "operation_errors", "status", "exit_code", "stdout_tail", "stderr_tail", "diagnostic",
        ) if key in metadata}
        residue = {"tool": exchange.tool_name, "call_id": exchange.call_id, "ok": result.get("ok"),
                   "error_code": result.get("error_code"), "error": result.get("error"), "metadata": kept_metadata}
        if exchange.tool_name in {"search_text", "find_files"}:
            residue["query"] = exchange.arguments.get("query")
        elif exchange.tool_name == "run_command":
            if not kept_metadata.get("stdout_tail") and not kept_metadata.get("stderr_tail"):
                kept_metadata.update(_legacy_command_tails(str(result.get("content") or "")))
            residue.update({key: exchange.arguments.get(key) for key in ("command", "cwd", "purpose")})
        elif exchange.tool_name in {"read_file", "list_dir", "show_tree", "git_diff", "git_status", "git_diff_stat"}:
            residue["path"] = exchange.arguments.get("path") or metadata.get("path")
            if exchange.tool_name == "read_file" and metadata.get("sha256") and residue["path"]:
                try:
                    current = PathGuard(self.session_store.workspace_context().active_root).resolve(str(residue["path"])).read_bytes()
                    residue["changed_since_read"] = hashlib.sha256(current).hexdigest() != metadata["sha256"]
                except (PathGuardError, OSError):
                    residue["changed_since_read"] = True
        elif exchange.tool_name == "apply_workspace_edit":
            residue["operations"] = [{"op": item.get("op"), "path": item.get("path") or item.get("source_path"),
                                      "destination_path": item.get("destination_path")}
                                     for item in exchange.arguments.get("operations", []) if isinstance(item, dict)]
        return residue

    def _runtime_snapshot(self, context: WorkspaceContext) -> RuntimeSnapshot:
        from codeagent.runtime.git_snapshot import GitSnapshotError, snapshot_tree
        from codeagent.runtime.validation import current_validation_evidence

        meta = self.session_store.read_meta()
        changed = _git_changed_paths(context.active_root)
        tree = None
        if context.base_commit or (context.active_root / ".git").exists():
            try:
                tree = snapshot_tree(context.active_root, context.base_commit or "HEAD")
            except GitSnapshotError:
                pass
        validation = "current" if current_validation_evidence(self.session_store, context) else "none_or_stale"
        return RuntimeSnapshot(str(meta.get("workspace_state", "unknown")), int(meta.get("workspace_revision", 0)),
            int(meta.get("candidate_revision", 0)), context.base_commit, tree, tuple(changed), validation,
            {"default_cwd": ".", "home_kind": "private_runtime_home", "tilde_is_host_home": False, "network_mode": "off"})

    def _active_code(self, turns: list[UserTurnView], context: WorkspaceContext, token_budget: int,
                     snapshot: RuntimeSnapshot) -> list[ActiveCodeSlice]:
        evidence: dict[str, tuple[int, int, str, str]] = {}
        for item in snapshot.changed_paths:
            if item.get("kind") != "delete":
                evidence[item["path"]] = (1, 200, "changed_file", "deterministic prefix; no range metadata")
        for turn in turns:
            for step in turn.model_steps:
                for exchange in step.exchanges:
                    result = exchange.result or {}
                    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
                    if exchange.tool_name == "apply_workspace_edit" and not result.get("ok"):
                        for operation in exchange.arguments.get("operations", []):
                            if not isinstance(operation, dict):
                                continue
                            target = operation.get("path") or operation.get("source") or operation.get("destination")
                            if isinstance(target, str):
                                start = int(operation.get("start_line", 1))
                                end = int(operation.get("end_line", start + 199))
                                provenance = "failed edit operation range" if "start_line" in operation else "deterministic prefix; no range metadata"
                                evidence[target] = (start, min(end, start + 199), "latest_failed_edit_target", provenance)
                    path = metadata.get("path") or exchange.arguments.get("path")
                    if not isinstance(path, str):
                        continue
                    if exchange.tool_name == "read_file" and result.get("ok"):
                        start = int(metadata.get("start_line", exchange.arguments.get("start_line", 1)))
                        end = int(metadata.get("end_line", exchange.arguments.get("end_line", start + 199)))
                        evidence[path] = (start, min(end, start + 199), "recent_read", "read_file requested range")
                    elif not result.get("ok") and exchange.tool_name == "apply_workspace_edit":
                        evidence[path] = (1, 200, "latest_failed_edit_target", "deterministic prefix; no range metadata")
        slices, remaining = [], max(0, token_budget)
        priority = {"latest_failed_edit_target": 0, "changed_file": 1, "recent_read": 2}
        guard = PathGuard(context.active_root)
        for path, (start, end, reason, provenance) in sorted(evidence.items(), key=lambda item: (priority[item[1][2]], item[0])):
            if remaining <= 0:
                break
            try:
                lines = guard.resolve(path).read_text(encoding="utf-8").splitlines()
            except (PathGuardError, OSError, UnicodeError):
                continue
            content = "\n".join(lines[start - 1:end])[:remaining * 3]
            slices.append(ActiveCodeSlice(path, start, start + max(0, content.count("\n")), content, reason, provenance))
            remaining -= self.estimator.estimate(content)
        return slices

    @staticmethod
    def _render_snapshot(snapshot: RuntimeSnapshot, active_code: list[ActiveCodeSlice]) -> str:
        data = {"runtime_snapshot": {"workspace_state": snapshot.workspace_state,
            "workspace_revision": snapshot.workspace_revision, "candidate_revision": snapshot.candidate_revision,
            "base_commit": snapshot.base_commit, "subject_tree": snapshot.subject_tree,
            "changed_paths": list(snapshot.changed_paths), "validation_state": snapshot.validation_state,
            "execution_environment": snapshot.environment}, "active_code": [item.__dict__ for item in active_code]}
        return "以下是当前 Runtime/Git 客观事实；它覆盖历史对话中的旧状态：\n" + json.dumps(data, ensure_ascii=False)

    @staticmethod
    def _read_prompt(name: str) -> str:
        return files("codeagent.prompts").joinpath(name).read_text(encoding="utf-8")


class ContextBuilder:
    """旧调用点兼容入口；容量由 token budget 保证，不再按 max_turns 截断。"""
    def __init__(self, session_store: SessionStore, max_turns: int | None = None) -> None:
        self.manager = ContextManager(session_store)

    def build(self) -> list[dict]:
        return self.manager.build()


def _git_changed_paths(root: Path) -> list[dict[str, str]]:
    try:
        proc = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=root,
                              capture_output=True, check=False, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode:
        return []
    rows, parts, index = [], proc.stdout.decode("utf-8", "surrogateescape").split("\0"), 0
    while index < len(parts) and parts[index]:
        row = parts[index]
        status, path = row[:2], row[3:]
        kind = "create" if "?" in status or "A" in status else "delete" if "D" in status else "rename" if "R" in status else "update"
        if "R" in status and index + 1 < len(parts):
            index += 1
            path = parts[index]
        rows.append({"path": path, "kind": kind})
        index += 1
    return rows


def _legacy_command_tails(content: str, *, max_lines: int = 20, max_chars: int = 1200) -> dict[str, str]:
    """兼容旧 Event：按 Runtime 自己的 result markers 提取有限 stdout/stderr 尾部。"""
    sections: dict[str, list[str]] = {"stdout": [], "stderr": []}
    current: str | None = None
    for line in content.splitlines():
        if line == "stdout:":
            current = "stdout"
        elif line == "stderr:":
            current = "stderr"
        elif line.startswith("effective_policy:") or line.startswith("diagnostic:"):
            current = None
        elif current:
            sections[current].append(line)
    result = {}
    for name, lines in sections.items():
        tail = "\n".join(lines[-max_lines:])
        if len(tail) > max_chars:
            tail = "...[tail truncated]\n" + tail[-max_chars:]
        if tail:
            result[f"{name}_tail"] = tail
    return result
