"""完成由模型正常回复决定；缺清单、未测试、失败检查不会触发强制补表。"""
import pytest
from codeagent.application.session_runtime import build_agent_runner
from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.model_gateway.base import BaseModelClient, LLMResponse, LLMToolCall
from codeagent.runtime.approval import AutoApprovalGate
from test_orchestration import repo_store, events


class PlanThenFinish(BaseModelClient):
    def complete(self, request):
        if not any(m['role'] == 'tool' for m in request.messages):
            return LLMResponse(tool_calls=[LLMToolCall(call_id='plan', name='update_task_plan', arguments={
                'tasks': [{'title': '尚未执行的检查', 'status': 'todo'}],
            })])
        return LLMResponse(text='结果与未完成项由回复说明。')


def plan_factory(config):
    return PlanThenFinish()


def test_incomplete_plan_does_not_request_another_model_response(repo_store):
    _, store = repo_store
    runner = build_agent_runner(store, model_config=ModelConfig(), approval_gate=AutoApprovalGate(False),
        runtime_config=RuntimeConfig(), model_factory=plan_factory)
    result = runner.run_turn('整理工作')
    assert result.status == 'completed' and result.final_text == '结果与未完成项由回复说明。'
    assert len(events(store, 'model_usage')) == 2
    assert not events(store, 'task_plan_final_deferred') and not events(store, 'task_plan_final_checked')
    assert len(events(store, 'assistant_message')) == 1
