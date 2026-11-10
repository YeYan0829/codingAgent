"""技能实际加载与报告采纳的无网络协议检查，不证明模型自主选择。"""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace as Obj
import uuid

import pytest

from codeagent.application.session_runtime import build_agent_runner
from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.model_gateway.glm_client import GLMClient
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.skills.builtin import load_builtin_skill, render_behavior_contract_skill
from test_orchestration import repo_store, events


def test_builtin_skill_has_stable_content_identity_and_closed_lookup():
    skill = load_builtin_skill("behavior-contract")
    assert skill.identity == hashlib.sha256(f"{skill.name}\n{skill.version}\n{skill.content}".encode()).hexdigest()
    assert load_builtin_skill(skill.name) is skill
    with pytest.raises(ValueError, match="unknown builtin"):
        load_builtin_skill("../../README.md")
    with pytest.raises(ValueError, match="unsupported skill role"):
        render_behavior_contract_skill("writer_child")




class SkillAdoptionSDK:
    def __init__(self):
        self.chat = Obj(completions=self)

    def with_options(self, **kwargs):
        assert kwargs == {"max_retries": 0}
        return self

    def create(self, **wire):
        messages = wire["messages"]
        child = messages[0]["content"].startswith("你是独立只读")
        assert render_behavior_contract_skill("readonly_child" if child else "main") in messages[0]["content"]
        tools = {t["function"]["name"] for t in wire.get("tools", [])}
        observations = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        if child:
            assert "PARENT_PRIVATE_PLAN" not in json.dumps(messages)
            assert tools <= {"read_file", "search_text", "list_dir", "show_tree", "find_files", "read_observation"}
            if not observations:
                action = ("read_file", {"path": "CONVENTIONS.md"})
            else:
                assert observations[0]["ok"] and "保留标题标记" in observations[0]["content"]
                action = "findings: CONVENTIONS.md:1 要求保留标题标记 #。unresolved: 未核实其他文档。limitations: 只读；未执行验证。"
        elif not observations:
            action = ("task", {"subagent_type": "explore", "prompt": "查明文档标题的格式约束，返回文件依据和未确认项。原要求：将README标题R1改为R2；主负责编辑，只查CONVENTIONS.md。"})
        elif len(observations) == 1:
            report = json.loads(observations[0]["content"])
            assert report["status"] == "completed" and "保留标题标记" in report["text"]
            action = ("read_file", {"path": "README.md"})
        elif len(observations) == 2:
            assert observations[-1]["ok"]
            action = ("apply_workspace_edit", {"operations": [{"op": "replace_text", "path": "README.md", "expected_sha256": observations[-1]["metadata"]["sha256"], "old_text": "# R1", "new_text": "# R2"}]})
        else:
            assert observations[-1]["ok"]
            action = "采用子报告的标题格式约束，保留#并修改R1为R2；未运行测试。"
        directory = os.environ.get("C4_SKILL_AUDIT_OUTPUT")
        if directory:
            p = Path(directory);p.mkdir(parents=True, exist_ok=True)
            (p / (("child-" if child else "main-") + uuid.uuid4().hex + ".json")).write_text(json.dumps(wire, ensure_ascii=False, indent=2) + "\n")
        if isinstance(action, tuple):
            name, args = action
            message = Obj(content="", tool_calls=[Obj(id=uuid.uuid4().hex, function=Obj(name=name, arguments=json.dumps(args, ensure_ascii=False)))])
            reason = "tool_calls"
        else:
            message = Obj(content=action, tool_calls=[]);reason = "stop"
        return Obj(id="offline-skill", choices=[Obj(message=message, finish_reason=reason)], usage=Obj(prompt_tokens=100, completion_tokens=30, total_tokens=130))


def skill_adoption_factory(config):
    return GLMClient(model="glm-5.3", client=SkillAdoptionSDK(), reasoning_enabled=True, reasoning_effort="high")


def test_actual_sdk_skill_report_changes_parent_edit_and_preserves_source(repo_store):
    root, store = repo_store
    (root / "CONVENTIONS.md").write_text("保留标题标记 #，只修改标题文字。\n")
    runner = build_agent_runner(store, model_config=ModelConfig(provider="glm", model="glm-5.3", max_tokens=8192, reasoning_enabled=True, reasoning_effort="high"), runtime_config=RuntimeConfig(delegation_enabled=True), approval_gate=AutoApprovalGate(True), model_factory=skill_adoption_factory)
    result = runner.run_turn("将README标题R1改为R2。PARENT_PRIVATE_PLAN 主负责编辑。")
    # 本夹具只测子信息采纳，不填写结果核对；新交付检查应保留这项明确缺口。
    assert result.status == "completed", result
    assert (root / "README.md").read_text().startswith("# R2")
    assert (root / "CONVENTIONS.md").read_text().startswith("保留标题标记")
    assert (store.workspace_context().active_root / "README.md").read_text().startswith("# R2")
    command, = events(store, "agent_command_intent")
    assert len(events(store, "agent_run_ack")) == 1
