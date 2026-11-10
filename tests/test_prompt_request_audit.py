"""实际 Runtime → GLM SDK 的免费提示审校夹具；不模拟模型质量。

C4_PROMPT_AUDIT_OUTPUT 可导出最终 kwargs。SDK 没有网络实现；子进程各写一份，
消息、工具和现场均由实际 Runtime 装配。
"""
import json
import os
from pathlib import Path
from types import SimpleNamespace as Obj
import uuid

from codeagent.application.session_runtime import build_agent_runner
from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.model_gateway.glm_client import GLMClient
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.skills.builtin import render_behavior_contract_skill
from test_orchestration import repo_store, events, seed_main_history


class AuditSDK:
    def __init__(self):
        self.chat = Obj(completions=self)

    def with_options(self, **kwargs):
        assert kwargs == {"max_retries": 0}
        return self

    def create(self, **wire):
        messages = wire["messages"]
        systems = "\n".join(m["content"] for m in messages if m["role"] == "system")
        child = "独立只读" in messages[0]["content"]
        summary = "历史压缩器" in systems
        kind = "summary" if summary else "child" if child else "main"
        tools = {t["function"]["name"] for t in wire.get("tools", [])}
        assert wire["model"] == "glm-5.3"
        assert wire["extra_body"]["thinking"]["clear_thinking"] is True
        assert wire["reasoning_effort"] == ("low" if summary else "high")
        if child:
            assert render_behavior_contract_skill("readonly_child") in messages[0]["content"]
            assert "PARENT_PRIVATE" not in json.dumps(messages)
            assert tools <= {"read_file", "search_text", "list_dir", "show_tree", "find_files", "read_observation"}
            anchor = next(m for m in reversed(messages) if m["role"] == "user")
            goal = json.loads(anchor["content"])["goal"]
            observations = [m for m in messages[messages.index(anchor) + 1:] if m["role"] == "tool"]
            index = len(observations)
            # 故意越界，再使用允许的工具恢复；最后一次实际不再暴露任何工具。
            calls = [("run_command", {"command": "touch ESCAPED"}),
                     ("read_file", {"path": "missing.md"}),
                     ("read_file", {"path": "README.md"})]
            if index < 3:
                action = calls[index]
            else:
                assert tools and wire["tool_choice"] == "auto"
                assert json.loads(observations[0]["content"])["error_code"] == "policy_denied"
                assert json.loads(observations[1]["content"])["error_code"] == "not_found"
                assert json.loads(observations[2]["content"])["ok"]
                action = "findings: README.md:1 是缓存入口。limitations: 仅文件证据，未运行测试。unresolved: Git 历史不可查。" + goal
        elif summary:
            assert not tools and "tool_choice" not in wire
            action = "USER_REQUIREMENTS: 保留公开接口。IMPORTANT_EVIDENCE: README.md 缓存入口。PENDING: 缓存失效假设未验证；主负责复现，子查文件合同。"
        elif "compaction fixture" in json.dumps(messages):
            assert render_behavior_contract_skill("main") in messages[0]["content"]
            assert any(m["role"] == "user" and (m.get("content") or "").startswith("<historical_memory>")
                       for m in messages)
            action = "压缩后继续；未运行测试。"
        else:
            assert render_behavior_contract_skill("main") in messages[0]["content"]
            observations = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            if not observations:
                action = ("read_file", {"path": "README.md"})
            elif len(observations) == 1:
                action = ("task", {"subagent_type": "explore", "prompt": "查缓存配置的调用方，报告文件依据和无法核实项。原要求：修复缓存失效且保留公开接口；主负责复现与修改；只查配置调用方。"})
            elif len(observations) in (2, 3):
                report = json.loads(observations[-1]["content"])
                action = ("task", {"resume": report["task_id"], "prompt": "重新读取当前文件，核对缓存配置引用并报告证据与限制。"})
            else:
                assert any("findings:" in (m.get("content") or "") for m in messages)
                action = "整合子报告；只读证据不能作为测试通过。"
        output = os.environ.get("C4_PROMPT_AUDIT_OUTPUT")
        if output:
            directory = Path(output)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / (kind + "-" + uuid.uuid4().hex + ".json")).write_text(
                json.dumps({"kind": kind, "wire": wire}, ensure_ascii=False, indent=2) + "\n")
        if isinstance(action, tuple):
            name, arguments = action
            message = Obj(content="", tool_calls=[Obj(id="fixture-" + uuid.uuid4().hex,
                function=Obj(name=name, arguments=json.dumps(arguments, ensure_ascii=False)))])
            reason = "tool_calls"
        else:
            message = Obj(content=action, tool_calls=[])
            reason = "stop"
        return Obj(id="offline-audit", choices=[Obj(message=message, finish_reason=reason)],
                   usage=Obj(prompt_tokens=100, completion_tokens=30, total_tokens=130))


def audit_factory(config):
    return GLMClient(model="glm-5.3", client=AuditSDK(), reasoning_enabled=True, reasoning_effort="high")


def audit_runner(store, summary_max_tokens=None):
    return build_agent_runner(store, model_config=ModelConfig(provider="glm", model="glm-5.3",
        temperature=1, max_tokens=8192, summary_max_tokens=summary_max_tokens,
        reasoning_enabled=True, reasoning_effort="high"),
        runtime_config=RuntimeConfig(delegation_enabled=True), approval_gate=AutoApprovalGate(False), model_factory=audit_factory)


def test_actual_sdk_child_boundaries_followup_refresh_and_parent_integration(repo_store):
    root, store = repo_store
    result = audit_runner(store).run_turn("修复缓存失效，保留公开接口。PARENT_PRIVATE 不传子会话。")
    assert result.status == "completed", result
    reports = events(store, "agent_run_finished")
    assert len(reports) == 3 and all(e.payload["status"] == "completed" for e in reports)
    assert all("context_epoch" not in e.payload for e in reports)
    assert not (root / "ESCAPED").exists()
    assert len(events(store, "agent_run_ack")) == 3
    assert not events(store, "child_report_observation")  # 同步task结果已交付，不重复注入后台报告。
    requests = events(store, "model_request_reserved")
    assert {e.payload["purpose"] for e in requests} >= {"main_agent", "readonly_child"}
    assert all(e.payload["max_tokens"] == 8192 for e in requests)


def test_actual_sdk_main_and_child_share_model_default_output_limit(repo_store, tmp_path, monkeypatch):
    _, store = repo_store
    capture = tmp_path / "default-sdk"
    monkeypatch.setenv("C4_PROMPT_AUDIT_OUTPUT", str(capture))
    runner = build_agent_runner(store, model_config=ModelConfig(provider="glm", model="glm-5.3",
        temperature=1, reasoning_enabled=True), runtime_config=RuntimeConfig(delegation_enabled=True),
        approval_gate=AutoApprovalGate(False), model_factory=audit_factory)
    result = runner.run_turn("修复缓存失效，保留公开接口。")
    assert result.status == "completed", result
    requests = events(store, "model_request_reserved")
    assert {e.payload["purpose"] for e in requests} >= {"main_agent", "readonly_child"}
    assert all(e.payload["max_tokens"] == 65536 for e in requests)
    wire = [json.loads(path.read_text()) for path in capture.glob("*.json")]
    assert {row["kind"] for row in wire} == {"main", "child"}
    assert all(row["wire"]["max_tokens"] == 65536 for row in wire)


def test_actual_sdk_compaction_preserves_anchor_and_recent_protocol(repo_store, tmp_path, monkeypatch):
    _, store = repo_store
    capture = tmp_path / "summary-sdk"
    monkeypatch.setenv("C4_PROMPT_AUDIT_OUTPUT", str(capture))
    seed_main_history(store)
    result = audit_runner(store, summary_max_tokens=3072).run_turn("compaction fixture：保留公开接口，继续未完成验证。")
    assert result.status == "completed", result
    assert len(events(store, "context_condensed")) == 1
    assert [e.payload["purpose"] for e in events(store, "model_request_reserved")] == ["context_condenser", "main_agent"]
    wire = [json.loads(path.read_text()) for path in capture.glob("*.json")]
    assert {row["kind"]: row["wire"]["max_tokens"] for row in wire} == {"summary": 3072, "main": 8192}


def test_actual_sdk_summary_inherits_default_output_and_uses_low_effort(repo_store, tmp_path, monkeypatch):
    _, store = repo_store
    capture = tmp_path / "inherited-summary-sdk"
    monkeypatch.setenv("C4_PROMPT_AUDIT_OUTPUT", str(capture))
    seed_main_history(store)
    runner = build_agent_runner(store, model_config=ModelConfig(provider="glm", model="glm-5.3",
        reasoning_enabled=True), runtime_config=RuntimeConfig(delegation_enabled=True),
        approval_gate=AutoApprovalGate(False), model_factory=audit_factory)
    result = runner.run_turn("compaction fixture：保留当前要求，继续核实。")
    assert result.status == "completed"
    wire = [json.loads(path.read_text()) for path in capture.glob("*.json")]
    assert {row["kind"]: row["wire"]["max_tokens"] for row in wire} == {"summary": 65536, "main": 65536}
    assert {row["kind"]: row["wire"]["reasoning_effort"] for row in wire} == {"summary": "low", "main": "high"}


class RecoveryAuditSDK:
    def __init__(self):
        self.chat = Obj(completions=self)

    def with_options(self, **kwargs):
        assert kwargs == {"max_retries": 0}
        return self

    def create(self, **wire):
        messages = wire["messages"]
        assert any(m["role"] == "user" and m["content"] == "把 README 标题从 R1 改成 R2。" for m in messages)
        tools = {t["function"]["name"] for t in wire.get("tools", [])}
        assert {"apply_workspace_edit", "run_command"} <= tools
        assert wire["tool_choice"] == "auto"
        observations = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        recovering = any(m["role"] == "system" and "上一次响应达到输出长度上限" in m.get("content", "") for m in messages)
        index = 3 if recovering else {0: 1, 1: 2, 2: 4, 3: 5}[len(observations)]
        assert wire["reasoning_effort"] == ("low" if recovering else "high")
        output = os.environ.get("C4_PROMPT_AUDIT_OUTPUT")
        if output:
            directory = Path(output)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / ("recovery-" + str(index) + ".json")).write_text(
                json.dumps({"kind": "recovery", "wire": wire}, ensure_ascii=False, indent=2) + "\n")
        if index in (2, 3):
            assert len(observations) == 1 and observations[0]["ok"]
            action = ("apply_workspace_edit", {"operations": [{"op": "replace_text", "path": "README.md",
                "expected_sha256": observations[0]["metadata"]["sha256"], "old_text": "R1", "new_text": "BAD" if index == 2 else "R2"}]})
            reason = "length" if index == 2 else "tool_calls"
        elif index in (1, 4):
            action, reason = ("read_file", {"path": "README.md"}), "tool_calls"
        else:
            assert observations[-1]["ok"] and "# R2" in observations[-1]["content"]
            assert "BAD" not in observations[-1]["content"]
            action, reason = "已修改标题并读取确认；未运行测试。", "stop"
        if isinstance(action, tuple):
            name, arguments = action
            message = Obj(content="", tool_calls=[Obj(id="recover-" + str(index),
                function=Obj(name=name, arguments=json.dumps(arguments, ensure_ascii=False)))])
        else:
            message = Obj(content=action, tool_calls=[])
        return Obj(id="offline-recover", choices=[Obj(message=message, finish_reason=reason)],
                   usage=Obj(prompt_tokens=100, completion_tokens=30, total_tokens=130))


def recovery_audit_factory(config):
    return GLMClient(model="glm-5.3", client=RecoveryAuditSDK(), reasoning_enabled=True, reasoning_effort="high")


def test_actual_sdk_truncation_keeps_edit_capability_and_continues_same_task(repo_store):
    """SDK 截断后保持目标/工具/隔离，执行真实 hash 编辑；不证明模型语义。"""
    root, store = repo_store
    runner = build_agent_runner(store, model_config=ModelConfig(provider="glm", model="glm-5.3",
        max_tokens=8192, reasoning_enabled=True, reasoning_effort="high"),
        runtime_config=RuntimeConfig(delegation_enabled=True),
        approval_gate=AutoApprovalGate(True), model_factory=recovery_audit_factory)
    output = runner.run_turn("把 README 标题从 R1 改成 R2。")
    assert output.status == "completed", output
    assert "没有结果清单" not in output.final_text
    assert len(events(store, "model_request_reserved")) == 5
    assert not events(store, "task_plan_final_deferred")
    assert len(events(store, "model_output_truncated")) == 1
    assert (root / "README.md").read_text().startswith("# R2")
    assert (store.workspace_context().active_root / "README.md").read_text().startswith("# R2")


class EmptyFileAuditSDK:
    def __init__(self):
        self.chat = Obj(completions=self)

    def with_options(self, **kwargs):
        assert kwargs == {"max_retries": 0}
        return self

    def create(self, **wire):
        observations = [json.loads(m["content"]) for m in wire["messages"] if m["role"] == "tool"]
        index = len(observations)
        output = os.environ.get("C4_PROMPT_EMPTY_EDIT_OUTPUT")
        if output:
            directory = Path(output)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"empty-edit-{index}.json").write_text(json.dumps(wire, ensure_ascii=False, indent=2) + "\n")
        if index == 0:
            action = ("read_file", {"path": "README.md"})
        elif index == 1:
            assert observations[0]["ok"] and observations[0]["content"] == ""
            action = ("apply_workspace_edit", {"operations": [{"op": "replace_text", "path": "README.md",
                "expected_sha256": observations[0]["metadata"]["sha256"], "old_text": "", "new_text": "# 已填写\n"}]})
        else:
            assert observations[-1]["ok"], observations[-1]
            action = "已填写空文件，未运行测试。"
        if isinstance(action, tuple):
            name, arguments = action
            message = Obj(content="", tool_calls=[Obj(id=f"empty-{index}",
                function=Obj(name=name, arguments=json.dumps(arguments, ensure_ascii=False)))])
            reason = "tool_calls"
        else:
            message, reason = Obj(content=action, tool_calls=[]), "stop"
        return Obj(id="offline-empty-edit", choices=[Obj(message=message, finish_reason=reason)],
                   usage=Obj(prompt_tokens=100, completion_tokens=30, total_tokens=130))


def empty_file_audit_factory(config):
    return GLMClient(model="glm-5.3", client=EmptyFileAuditSDK(), reasoning_enabled=True, reasoning_effort="high")


def test_actual_sdk_fills_user_empty_file_in_candidate_without_overwriting_source(repo_store):
    root, store = repo_store
    (root / "README.md").write_bytes(b"")  # 用户未提交的空文件也须保全。
    runner = build_agent_runner(store, model_config=ModelConfig(provider="glm", model="glm-5.3",
        max_tokens=8192, reasoning_enabled=True, reasoning_effort="high"),
        runtime_config=RuntimeConfig(delegation_enabled=True),
        approval_gate=AutoApprovalGate(True), model_factory=empty_file_audit_factory)
    result = runner.run_turn("将空README填写为标题“已填写”。")
    assert result.status == "completed", result
    assert "没有结果清单" not in result.final_text
    assert len(events(store, "model_request_reserved")) == 3
    assert not events(store, "task_plan_final_deferred")
    assert (root / "README.md").read_text() == "# 已填写\n"
    assert (store.workspace_context().active_root / "README.md").read_text() == "# 已填写\n"
