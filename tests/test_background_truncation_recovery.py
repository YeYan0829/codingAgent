"""同步委派交付后的主模型截断保留报告，不重启已完成子任务。"""
import json
import os
from pathlib import Path
import time

import pytest

from codeagent.application.session_runtime import build_agent_runner
from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.model_gateway.base import LLMResponse, LLMToolCall
from codeagent.model_gateway.fake import FakeLLM
from codeagent.runtime.approval import AutoApprovalGate
from test_orchestration import repo_store, spawn_args, events


class HeldChildModel(FakeLLM):
    def complete(self, request):
        folder = Path(os.environ["BACKGROUND_TRUNCATION_TEST"])
        if request.purpose == "readonly_child":
            observations = [m for m in request.messages if m["role"] == "tool"]
            if observations:
                return LLMResponse(text="child evidence read", finish_reason="stop")
            (folder / "child-started").touch()
            return LLMResponse(tool_calls=[LLMToolCall(call_id="read", name="read_file",
                arguments={"path": "README.md", "start_line": 1, "end_line": 3})], finish_reason="tool_calls")
        observations = [m for m in request.messages if m["role"] == "tool"]
        if not observations:
            return LLMResponse(tool_calls=[LLMToolCall(call_id="task", name="task",
                arguments={"subagent_type": "review", "prompt": "read README"})], finish_reason="tool_calls")
        assert "child evidence read" in json.dumps(request.messages)
        if not (folder / "truncated").exists() or (folder / "terminal-case").exists():
            assert (folder / "child-started").exists()
            (folder / "truncated").touch()
            return LLMResponse(text="partial", finish_reason="length")
        if len(observations) == 1:
            assert request.reasoning_effort == "low"
            return LLMResponse(tool_calls=[LLMToolCall(call_id="recovery-read", name="read_file",
                arguments={"path": "README.md"})], finish_reason="tool_calls")
        return LLMResponse(text="integrated child evidence", finish_reason="stop")

def held_child_factory(config):
    return HeldChildModel()


@pytest.mark.parametrize("terminal", [False, True])
def test_main_truncation_keeps_completed_child_evidence_without_reopening(repo_store, tmp_path, monkeypatch, terminal):
    _, store = repo_store
    folder = tmp_path / "coordination"
    folder.mkdir()
    monkeypatch.setenv("BACKGROUND_TRUNCATION_TEST", str(folder))
    if terminal:
        (folder / "terminal-case").touch()
    runner = build_agent_runner(store, approval_gate=AutoApprovalGate(False),
        model_config=ModelConfig(provider="glm", model="glm-5.3", reasoning_enabled=True,
                                 reasoning_effort="high", max_tokens=8192),
        runtime_config=RuntimeConfig(delegation_enabled=True),
        model_factory=held_child_factory)
    output = runner.run_turn("调查 README 并整合报告")
    reports = events(store, "agent_run_finished")
    assert len(reports) == 1
    if terminal:
        assert output.status == "output_truncated"
        assert reports[0].payload["status"] == "completed"
        assert events(store, "agent_run_ack")
        assert not (folder / "release-child").exists()
    else:
        assert output.status == "completed", output.final_text
        assert reports[0].payload["status"] == "completed"
        assert output.final_text == "integrated child evidence"
        assert not any(e.payload.get("reason") == "output_truncation" for e in events(store, "agent_root_stopping"))
        assert len(events(store, "model_output_truncated")) == 1
        assert len(events(store, "model_request_reserved")) == 6  # 主4次，子2次，无重开或重试。
