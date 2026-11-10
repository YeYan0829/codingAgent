"""工作台组合回归：覆盖任务、修改审查与恢复之间的交互。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from codeagent.benchmark.swebench_executor import SWEbenchDockerCommandExecutor, repository_test_entrypoint
from codeagent.benchmark.swebench_native_artifacts import copy_native_artifacts
from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.context.builder import ContextManager, RUNTIME_STATE_PREFIX
from codeagent.context.models import ModelCapabilities
from codeagent.context.projector import project_context_events
from codeagent.runtime.validation_output import failed_test_summary
from codeagent.session.store import SessionStore
from codeagent.tools.base import ToolResult, ToolSpec, PermissionLevel
from codeagent.tools.command import command_tool_schema
from codeagent.tools.fs_read import build_fs_tools
from codeagent.tools.registry import ToolRegistry
from codeagent.workspace.workspace import WorkspaceContext


def _git_workspace(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=root, check=True)
    (root / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
    return root


def _tool(name):
    return ToolSpec(name, name, PermissionLevel.READ, {"type": "object", "properties": {}}, lambda _: ToolResult(ok=True))


def _registry(*names):
    registry = ToolRegistry()
    for name in names:
        registry.register(_tool(name))
    return registry


def _tool_round(store, call_id, name, arguments, result, *, message="", reasoning=None):
    payload = {"message": message, "tool_calls": [{"call_id": call_id, "name": name, "arguments": arguments}]}
    if reasoning is not None:
        payload["reasoning_content"] = reasoning
    store.append_event("assistant_tool_calls", payload)
    store.append_event("tool_result", {"call_id": call_id, "name": name, "result": result})


def test_volatile_state_is_at_tail_and_prefix_survives_workspace_change(tmp_path):
    """易变运行状态放在消息尾部，项目编辑保持历史前缀可缓存。"""
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "fix it"})
    _tool_round(store, "c1", "read_file", {"path": "a.py"}, {"ok": True, "content": "VALUE = 1\n"})
    before = ContextManager(store).build().messages
    (root / "a.py").write_text("VALUE = 2\n", encoding="utf-8")
    after = ContextManager(store).build().messages

    assert after[-1]["content"].startswith(RUNTIME_STATE_PREFIX)
    assert after[-1] == before[-1]  # Context装配不扫描工作区推导业务状态。
    # 除尾部状态外，前缀（system、环境合同、用户原文、历史工具对）逐字不变。
    assert after[:-1] == before[:-1]
    assert "changed_paths" not in after[2]["content"] and "validation_state" not in after[2]["content"]


def test_system_prompt_matches_registered_tools(tmp_path):
    """提示不能要求使用本轮不存在的清单或子 Agent 工具。"""
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "x"})
    lean = ContextManager(store, _registry("read_file")).build().messages[0]["content"]
    full = ContextManager(store, _registry("read_file", "update_task_plan", "read_task_plan", "task")).build().messages[0]["content"]
    for marker in ("update_task_plan", "task", "code-explorer"):
        assert marker not in lean
        assert marker in full
    lean_state = ContextManager(store, _registry("read_file")).build().messages[-1]["content"]
    assert "task_plan" not in lean_state and "completion_check" not in lean_state


def test_progress_tracking_does_not_change_command_schema():
    assert command_tool_schema(True) == command_tool_schema(False)
    assert "verifies" not in command_tool_schema(False)["properties"]
    assert RuntimeConfig().task_tracking is True


@pytest.mark.parametrize("scope", ["main", "child"])
@pytest.mark.parametrize("message", ["", "已核对缓存入口"])
def test_old_reasoning_is_not_rewritten_as_visible_assistant_text(tmp_path, scope, message):
    """旧推理只留原始记录；主子和摘要输入均不把猜测尾部变成普通结论。"""
    from codeagent.context.models import ContextState
    from codeagent.orchestration.context import ScopedHistoryStore

    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "fix"})
    reasoning = "long analysis " * 200 + "UNVERIFIED_REASONING_TAIL"
    _tool_round(store, "c1", "read_file", {"path": "a.py"}, {"ok": True, "content": "x"},
                message=message, reasoning=reasoning)
    _tool_round(store, "c2", "read_file", {"path": "a.py"}, {"ok": True, "content": "x"}, reasoning="next")
    history = store
    if scope == "child":
        for event in store.read_events():
            store.append_event("agent_history", {"agent_id": "child-a", "run_id": "run-a",
                "step_id": event.model_step_id, "kind": event.type, "data": event.payload})
        history = ScopedHistoryStore(store, {"agent_id": "child-a", "run_id": "run-a"}, lambda *args, **kwargs: None)
    manager = ContextManager(history, state_provider=lambda: ContextState(str(root), {"readonly": scope == "child"}))
    messages = manager.build().messages
    first = next(m for m in messages if m.get("tool_calls") and m["tool_calls"][0]["id"] == "c1")
    latest = next(m for m in messages if m.get("tool_calls") and m["tool_calls"][0]["id"] == "c2")
    assert "reasoning_content" not in first
    assert first["content"] == (message or None)
    assert latest["reasoning_content"] == "next" and latest["content"] == ""
    projection = project_context_events(history.read_events())
    assert all("decision_note" not in item.payload for item in projection.items)
    condenser = manager._condenser_messages(list(projection.atoms), None, ModelConfig().resolved_max_tokens)
    rendered = json.dumps(messages + condenser, ensure_ascii=False)
    assert "推理结论摘录" not in rendered and "UNVERIFIED_REASONING_TAIL" not in rendered
    raw = next(e for e in history.read_events() if e.type == "assistant_tool_calls")
    assert raw.payload["reasoning_content"] == reasoning
    assert raw.payload["message"] == message


def test_command_projection_keeps_body_and_drops_duplicate_tails(tmp_path):
    """工具正文不被Context再次截短，重复的审计尾部不进入模型请求。"""
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "run"})
    noise = "\n".join(f"collected line {i}" for i in range(3000))
    content = f"status: completed\nexit_code: 1\nstdout:\n{noise}\nFAILED test_x.py::test_y - AssertionError: 2 != 3\n== 1 failed in 0.1s =="
    _tool_round(store, "c1", "run_command", {"command": "pytest"}, {
        "ok": False, "content": content, "error_code": "command_exit_nonzero",
        "metadata": {"status": "completed", "exit_code": 1, "stdout_tail": "dup", "stderr_tail": "dup"}})
    tool = next(m for m in ContextManager(store).build(
        model_capabilities=ModelCapabilities(context_limit=128_000)).messages if m["role"] == "tool")
    result = json.loads(tool["content"])
    assert "AssertionError: 2 != 3" in result["content"] and "1 failed" in result["content"]
    assert result["content"].startswith("status: completed")
    assert result["content"] == content
    assert "stdout_tail" not in result["metadata"] and "stderr_tail" not in result["metadata"]


def test_runner_reported_failures_are_not_validation_success():
    """测试汇总中的失败覆盖零退出码，避免误记验证成功。"""
    assert failed_test_summary("tests finished: 8 passed, 1 failed, in 0.20 seconds\nDO *NOT* COMMIT!", "")
    assert failed_test_summary("== 2 passed, 1 failed in 0.31s ==", "")
    assert failed_test_summary("FAILED (failures=2)", "")
    assert failed_test_summary("tests finished: 141 passed, 2 expected to fail, in 3.0 seconds", "") is None
    assert failed_test_summary("== 5 passed in 0.1s ==", "") is None


def test_search_context_lines_are_clamped_not_rejected(tmp_path):
    """搜索上下文超过支持范围时收敛到有效上限。"""
    root = _git_workspace(tmp_path)
    context = WorkspaceContext(root, root)
    search = next(tool for tool in build_fs_tools(context) if tool.name == "search_text")
    result = search.handler({"query": "VALUE", "context_lines": 25})
    assert result.ok, result.error
    assert result.metadata["context_lines_applied"] == 10
    assert result.metadata["case_effective"] == "sensitive"
    missing = search.handler({"query": "Value"})
    assert missing.metadata.get("search_hint")


def test_swebench_environment_declares_test_runner_without_test_selection():
    """环境提供项目测试入口，具体测试选择由Agent决定。"""
    django = repository_test_entrypoint("django__django-16950")
    assert "tests/runtests.py" in django["command"] and "pytest" in django["notes"]
    sympy = repository_test_entrypoint("sympy__sympy-18763")
    assert "bin/test" in sympy["command"]
    assert repository_test_entrypoint("psf__requests-1766")["command"].startswith("pytest -rA")
    # 只给运行方式，不包含题目对应的测试文件。
    assert "test_latex" not in json.dumps(sympy) and "test_uuid" not in django["command"]


def test_docker_commands_get_session_scratch_outside_candidate(tmp_path):
    from test_swebench_docker_executor import FakeDockerExecutionBackend, prepared
    from runtime_fixtures import execution_session
    from codeagent.runtime.artifacts import CommandArtifactStore
    from codeagent.runtime.command import CommandRequest, SandboxExecutionRequest
    from codeagent.runtime.permissions import EffectiveSandboxPolicy, NetworkMode
    _, store, context = execution_session(tmp_path)
    backend = FakeDockerExecutionBackend(context.active_root)
    executor = SWEbenchDockerCommandExecutor(prepared(context), backend)
    snapshot = executor.environment_snapshot(context, None, None)
    assert snapshot["scratch_dir"] == "/scratch" and "test_entrypoint" in snapshot
    request = CommandRequest(execution_id="e" * 32, command="true", cwd=".", purpose="utility", timeout_seconds=10)
    artifacts = CommandArtifactStore(store).prepare(request)
    policy = EffectiveSandboxPolicy(policy_revision="t", workspace_root=context.active_root, source_root=context.source_root,
                                    runtime_cache=tmp_path / "cache", read_grants=(), write_grants=(), hard_deny_paths=(),
                                    network_mode=NetworkMode.OFF)
    executor.execute(SandboxExecutionRequest(command=request, policy=policy), context, artifacts)
    mount = next(v for v in backend.created_argv if "dst=/scratch" in v)
    source = Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
    assert source.is_dir() and context.active_root not in source.parents
    assert "readonly" not in mount


def test_pristine_cache_rebuild_keeps_ignored_native_extensions(tmp_path):
    """Git clone 重建隔离缓存会丢掉被忽略的 .so，使 Candidate 无法导入原生扩展。"""
    source = _git_workspace(tmp_path)
    (source / ".gitignore").write_text("*.so\nbuild/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "ignore"], cwd=source, check=True)
    (source / "pkg").mkdir()
    (source / "pkg/_ext.cpython-311-x86_64-linux-gnu.so").write_bytes(b"\x7fELFext")
    (source / "build").mkdir()
    (source / "build/other.c").write_text("not copied\n")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--no-local", str(source), str(clone)], check=True)
    assert not (clone / "pkg").exists()
    result = copy_native_artifacts(source, clone)
    assert (clone / "pkg/_ext.cpython-311-x86_64-linux-gnu.so").read_bytes() == b"\x7fELFext"
    assert [item["path"] for item in result["copied"]] == ["pkg/_ext.cpython-311-x86_64-linux-gnu.so"]
    assert not (clone / "build").exists()
    assert subprocess.run(["git", "status", "--porcelain"], cwd=clone, capture_output=True, text=True).stdout == ""


def test_generated_version_modules_are_kept_but_build_scripts_and_secrets_are_not(tmp_path):
    """输入缓存保留生成的版本模块，同时排除构建脚本和密钥。"""
    source = _git_workspace(tmp_path)
    (source / ".gitignore").write_text("*.so\nbuild/\n_version.py\nversion.py\n.env\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "ignore"], cwd=source, check=True)
    (source / "lib/pkg").mkdir(parents=True)
    (source / "lib/pkg/_version.py").write_text("version = '1.0'\n")
    (source / "lib/pkg/_ext.so").write_bytes(b"\x7fELF")
    (source / "build/scripts").mkdir(parents=True)
    (source / "build/scripts/version.py").write_text("print('build helper')\n")
    (source / ".env").write_text("SECRET=1\n")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", "--no-local", str(source), str(clone)], check=True)
    copied = {item["path"] for item in copy_native_artifacts(source, clone)["copied"]}
    assert copied == {"lib/pkg/_version.py", "lib/pkg/_ext.so"}
    assert not (clone / "build").exists() and not (clone / ".env").exists()


def test_edit_file_flat_interface_and_missing_path_diagnostic(tmp_path):
    """编辑接口将文件路径作为顶层必填字段，并返回明确的缺项诊断。"""
    from test_atomic_edit import operation_service
    from codeagent.tools.atomic_edit import build_edit_file_tool
    _, _, context, service = operation_service(tmp_path)
    tool = build_edit_file_tool(service)
    assert tool.schema["required"] == ["path", "edits"]
    target = context.active_root / "sort_utils.py"
    assert service.edit_file({"path": "sort_utils.py", "edits": [
        {"old_text": "reverse=True", "new_text": "reverse=False"}]}).ok
    assert "reverse=False" in target.read_text(encoding="utf-8")
    # 单项的扁平写法同样接受。
    assert service.edit_file({"path": "sort_utils.py", "old_text": "reverse=False", "new_text": "reverse=True"}).ok
    missing = service.edit_file({"edits": [{"old_text": "a", "new_text": "b"}]})
    assert missing.error_code == "invalid_arguments" and "path" in missing.error
    # 任一处失败，整组不写入。
    before = target.read_bytes()
    partial = service.edit_file({"path": "sort_utils.py", "edits": [
        {"old_text": "reverse=True", "new_text": "reverse=False"}, {"old_text": "does not exist", "new_text": "x"}]})
    assert partial.error_code == "edit_conflict" and target.read_bytes() == before


def test_prompt_variant_is_appended_and_validated(tmp_path):
    """多候选生成的条件引导变体：只改方法段落，不改工具与状态。"""
    import pytest
    from codeagent.config import available_prompt_variants
    assert {"owner_layer", "contract_first"} <= available_prompt_variants()
    with pytest.raises(ValueError, match="prompt_variant"):
        RuntimeConfig(prompt_variant="nope")
    root = _git_workspace(tmp_path)
    store = SessionStore(root).create()
    store.append_event("user_message", {"message": "x"})
    plain = ContextManager(store, _registry("read_file")).build().messages
    varied = ContextManager(store, _registry("read_file"), prompt_variant="contract_first").build().messages
    assert "合同" in varied[0]["content"] and "合同优先" not in plain[0]["content"]
    assert varied[1:] == plain[1:]


def test_environment_declares_whether_tests_see_workspace_edits(tmp_path):
    """环境声明实际导入路径，以及测试是否会读取当前项目编辑。"""
    from dataclasses import replace
    from test_swebench_docker_executor import FakeDockerExecutionBackend, prepared
    from runtime_fixtures import execution_session
    _, store, context = execution_session(tmp_path)
    probes = []

    def probe(image, package):
        probes.append((image, package))
        return "/opt/miniconda3/envs/testbed/lib/python3.5/site-packages/Django-1.11-py3.5.egg/django/__init__.py"

    source = replace(prepared(context), instance_id="django__django-7530")
    executor = SWEbenchDockerCommandExecutor(source, FakeDockerExecutionBackend(context.active_root), import_probe=probe)
    first = executor.environment_snapshot(context, None, None)["package_import"]
    executor.environment_snapshot(context, None, None)
    assert probes == [(probes[0][0], "django")] and "sha256:" in probes[0][0]
    assert first["workspace_edits_visible_to_tests"] is False and "PYTHONPATH=/testbed" in first["note"]
    live = SWEbenchDockerCommandExecutor(source, FakeDockerExecutionBackend(context.active_root),
                                         import_probe=lambda image, package: "/testbed/django/__init__.py")
    assert live.environment_snapshot(context, None, None)["package_import"]["workspace_edits_visible_to_tests"] is True
    failing = SWEbenchDockerCommandExecutor(source, FakeDockerExecutionBackend(context.active_root), import_probe=lambda i, p: None)
    assert "package_import" not in failing.environment_snapshot(context, None, None)


def test_edit_arguments_with_misplaced_path_or_operation_key_are_accepted(tmp_path):
    """对路径位置和操作键的兼容写法执行无歧义规范化。"""
    from test_atomic_edit import operation_service
    _, _, context, service = operation_service(tmp_path)
    target = context.active_root / "sort_utils.py"
    assert service.edit_file({"edits": [{"path": "sort_utils.py", "old_text": "reverse=True", "new_text": "reverse=False"}]}).ok
    assert "reverse=False" in target.read_text(encoding="utf-8")
    assert service.apply_workspace_edit({"operations": [
        {"replace_text": True, "path": "sort_utils.py", "old_text": "reverse=False", "new_text": "reverse=True"}]}).ok
    assert "reverse=True" in target.read_text(encoding="utf-8")
    mixed = service.edit_file({"edits": [{"path": "a.py", "old_text": "x", "new_text": "y"}, {"path": "b.py", "old_text": "x", "new_text": "y"}]})
    assert mixed.error_code == "invalid_arguments" and "path" in mixed.error
