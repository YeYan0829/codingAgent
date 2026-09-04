from types import SimpleNamespace

import pytest

from codeagent.model_gateway.base import ModelRequest, ModelTool
from codeagent.model_gateway.base import MalformedToolArgumentsError
from codeagent.model_gateway.deepseek_client import DeepSeekClient


class FakeCompletions:
    def __init__(self, response, calls):
        self.response = response
        self.calls = calls

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeClient:
    def __init__(self, response):
        self.calls = []
        self.chat = SimpleNamespace(completions=FakeCompletions(response, self.calls))


def response_with_message(message, *, usage=None, request_id=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage, id=request_id)


def test_deepseek_text_response_to_llm_response():
    client = FakeClient(response_with_message(SimpleNamespace(content="hello", tool_calls=None)))
    deepseek = DeepSeekClient(api_key="key", client=client)

    response = deepseek.complete(ModelRequest(messages=[{"role": "user", "content": "hi"}], model="deepseek-v4-flash"))

    assert response.text == "hello"
    assert response.tool_calls == []
    assert client.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}


def test_deepseek_normalizes_provider_usage_and_request_id():
    usage = SimpleNamespace(
        prompt_tokens=120,
        completion_tokens=30,
        total_tokens=150,
        prompt_cache_hit_tokens=80,
        prompt_cache_miss_tokens=40,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=12),
    )
    client = FakeClient(response_with_message(
        SimpleNamespace(content="hello", tool_calls=None), usage=usage, request_id="req-1"
    ))

    response = DeepSeekClient(api_key="key", client=client).complete(
        ModelRequest(messages=[{"role": "user", "content": "hi"}])
    )

    assert response.provider_request_id == "req-1"
    assert response.usage.model_dump() == {
        "input_tokens": 120,
        "output_tokens": 30,
        "total_tokens": 150,
        "cached_input_tokens": 80,
        "cache_miss_input_tokens": 40,
        "reasoning_tokens": 12,
    }


def test_deepseek_converts_model_tools_to_chat_completion_tools():
    client = FakeClient(response_with_message(SimpleNamespace(content="hello", tool_calls=None)))
    deepseek = DeepSeekClient(api_key="key", client=client)
    tool = ModelTool(
        name="read_file",
        description="Read a file.",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    )

    deepseek.complete(ModelRequest(messages=[{"role": "user", "content": "hi"}], tools=[tool]))

    assert client.calls[0]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file.",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            },
        }
    ]


def test_deepseek_tool_calls_to_llm_response():
    tool_call = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="read_file", arguments='{"path": "README.md"}'),
    )
    client = FakeClient(response_with_message(SimpleNamespace(content=None, tool_calls=[tool_call])))
    deepseek = DeepSeekClient(api_key="key", client=client)

    response = deepseek.complete(ModelRequest(messages=[{"role": "user", "content": "hi"}]))

    assert response.text is None
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].call_id == "call_1"
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == {"path": "README.md"}


def test_deepseek_preserves_explanation_before_tool_calls():
    tool_call = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="read_file", arguments='{"path": "README.md"}'),
    )
    client = FakeClient(response_with_message(SimpleNamespace(content="我先阅读项目说明。", tool_calls=[tool_call])))

    response = DeepSeekClient(api_key="key", client=client).complete(
        ModelRequest(messages=[{"role": "user", "content": "inspect"}])
    )

    assert response.text == "我先阅读项目说明。"
    assert response.tool_calls[0].name == "read_file"


def test_deepseek_malformed_tool_arguments_return_clear_text():
    tool_call = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="read_file", arguments="{bad json"),
    )
    client = FakeClient(response_with_message(SimpleNamespace(content=None, tool_calls=[tool_call])))
    deepseek = DeepSeekClient(api_key="key", client=client)

    with pytest.raises(MalformedToolArgumentsError) as caught:
        deepseek.complete(ModelRequest(messages=[{"role": "user", "content": "hi"}]))
    assert caught.value.tool_name == "read_file"


def test_deepseek_keeps_usage_when_tool_arguments_are_malformed():
    tool_call = SimpleNamespace(
        id="call_1", function=SimpleNamespace(name="read_file", arguments="{bad json")
    )
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=3, total_tokens=13)
    client = FakeClient(response_with_message(
        SimpleNamespace(content=None, tool_calls=[tool_call]), usage=usage, request_id="req-bad"
    ))

    with pytest.raises(MalformedToolArgumentsError) as caught:
        DeepSeekClient(api_key="key", client=client).complete(ModelRequest(messages=[]))

    assert caught.value.provider_request_id == "req-bad"
    assert caught.value.usage.total_tokens == 13


def test_deepseek_retries_without_extra_body_when_sdk_rejects_it():
    class TypeErrorOnceCompletions:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise TypeError("unexpected extra_body")
            return response_with_message(SimpleNamespace(content="ok", tool_calls=None))

    completions = TypeErrorOnceCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    deepseek = DeepSeekClient(api_key="key", client=client)

    response = deepseek.complete(ModelRequest(messages=[{"role": "user", "content": "hi"}]))

    assert response.text == "ok"
    assert "extra_body" in completions.calls[0]
    assert "extra_body" not in completions.calls[1]
