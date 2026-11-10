"""主子共用的普通执行循环；恢复只使用已保存的响应与工具结果。"""
from codeagent.model_gateway.base import LLMResponse, ModelRequest


class AgentLoop:
    def __init__(self, adapter):
        self.adapter = adapter

    def invoke(self):
        a = self.adapter
        iteration = 0
        while True:
            a.check()
            a.iteration = iteration
            key = a.run_id + ':model:' + str(iteration)
            cached = next((e for e in a.store.read_events()
                if e.type == 'model_response' and e.payload['operation_key'] == key), None)
            if cached is not None:
                response = LLMResponse.model_validate(cached.payload['response'])
            else:
                prepared = a.prepare()
                if isinstance(prepared, dict):
                    return a.finish(prepared)
                request = ModelRequest(messages=prepared, tools=a.registry.as_model_tools(),
                    model=a.executor.model_config.resolved_model,
                    temperature=a.executor.model_config.temperature,
                    max_tokens=a.max_tokens, purpose=a.purpose,
                    reasoning_effort=a.request_reasoning_effort)
                if hasattr(a, 'before_request'):
                    a.before_request(key, request)
                response = a.execution.request(key, request, a.agent_id)
            action = a.record_response(response, iteration)
            if action['route'] == 'finish':
                return a.finish(action.get('output', {}))
            if action['route'] == 'tools':
                for index, call in enumerate(response.tool_calls):
                    a.check()
                    a.execute(call, a.run_id + ':tool:' + str(iteration) + ':' + str(index))
            iteration += 1
