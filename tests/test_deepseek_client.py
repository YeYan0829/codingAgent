from types import SimpleNamespace

from codeagent.model_gateway.base import ModelRequest, ModelTool
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


def response_with_message(message):
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def test_deepseek_text_response_to_llm_response():
    client = FakeClient(response_with_message(SimpleNamespace(content="hello", tool_calls=None)))
    deepseek = DeepSeekClient(api_key="key", client=client)

    response = deepseek.complete(ModelRequest(messages=[{"role": "user", "content": "hi"}], model="deepseek-v4-flash"))

    assert response.text == "hello"
    assert response.tool_calls == []
    assert client.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}


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


def test_deepseek_malformed_tool_arguments_return_clear_text():
    tool_call = SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="read_file", arguments="{bad json"),
    )
    client = FakeClient(response_with_message(SimpleNamespace(content=None, tool_calls=[tool_call])))
    deepseek = DeepSeekClient(api_key="key", client=client)

    response = deepseek.complete(ModelRequest(messages=[{"role": "user", "content": "hi"}]))

    assert "malformed tool arguments" in response.text
    assert response.tool_calls == []


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
