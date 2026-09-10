from types import SimpleNamespace

import pytest

from codeagent.model_gateway.base import MalformedToolArgumentsError, ModelRequest, ModelTool
from codeagent.model_gateway.glm_client import DEFAULT_GLM_MODEL, GLMClient


class FakeCompletions:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _client(message, *, usage=None, finish_reason=None):
    completions = FakeCompletions(SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)], usage=usage,
    ))
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


def test_glm_defaults_and_keeps_reasoning_disabled_by_default():
    client, completions = _client(SimpleNamespace(content="ok", tool_calls=None))
    glm = GLMClient(api_key="key", client=client)

    response = glm.complete(ModelRequest(messages=[{"role": "user", "content": "hi"}]))

    assert glm.model == DEFAULT_GLM_MODEL == "glm-5.2"
    assert response.text == "ok"
    assert completions.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "reasoning_effort" not in completions.calls[0]
    assert completions.calls[0]["tool_choice"] == "auto"


def test_glm_parses_openai_compatible_finish_reason():
    client, _ = _client(
        SimpleNamespace(content="", reasoning_content="仍在分析", tool_calls=None),
        finish_reason="length",
    )

    response = GLMClient(api_key="key", client=client).complete(ModelRequest(messages=[]))

    assert response.finish_reason == "length"


def test_glm_keeps_length_reason_on_truncated_tool_arguments():
    tool_call = SimpleNamespace(
        id="call-1", function=SimpleNamespace(name="read_file", arguments='{"path":'),
    )
    client, _ = _client(
        SimpleNamespace(content="partial", reasoning_content="thinking", tool_calls=[tool_call]),
        finish_reason="length",
    )

    with pytest.raises(MalformedToolArgumentsError) as caught:
        GLMClient(api_key="key", client=client).complete(ModelRequest(messages=[]))

    assert caught.value.finish_reason == "length"
    assert caught.value.text == "partial"
    assert caught.value.reasoning_content == "thinking"


def test_glm_uses_openai_compatible_function_calls():
    tool_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(name="read_file", arguments='{"path":"README.md"}'),
    )
    client, completions = _client(SimpleNamespace(content="先读取。", tool_calls=[tool_call]))
    glm = GLMClient(api_key="key", client=client)
    tool = ModelTool(
        name="read_file",
        description="Read a file.",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    )

    response = glm.complete(ModelRequest(messages=[{"role": "user", "content": "inspect"}], tools=[tool]))

    assert response.text == "先读取。"
    assert response.tool_calls[0].arguments == {"path": "README.md"}
    assert completions.calls[0]["tools"][0]["function"]["name"] == "read_file"


def test_glm_reasoning_tool_round_trip_uses_clearable_thinking_contract():
    tool_call = SimpleNamespace(
        id="call-1", function=SimpleNamespace(name="read_file", arguments='{"path":"README.md"}'),
    )
    client, completions = _client(SimpleNamespace(
        content="先读取。", reasoning_content="需要查看文件。", tool_calls=[tool_call],
    ))
    glm = GLMClient(api_key="key", client=client, reasoning_enabled=True, reasoning_effort="max")
    tool = ModelTool(name="read_file", description="Read", parameters={"type": "object"})

    first = glm.complete(ModelRequest(messages=[{"role": "user", "content": "inspect"}], tools=[tool]))
    assert first.reasoning_content == "需要查看文件。"
    assert completions.calls[0]["extra_body"] == {
        "thinking": {"type": "enabled", "clear_thinking": True},
    }
    assert completions.calls[0]["reasoning_effort"] == "max"

    messages = [
        {"role": "user", "content": "inspect"},
        {"role": "assistant", "content": first.text, "reasoning_content": first.reasoning_content,
         "tool_calls": [{"id": "call-1", "type": "function", "function": {
             "name": "read_file", "arguments": '{"path":"README.md"}',
         }}]},
        {"role": "tool", "tool_call_id": "call-1", "content": "# Demo"},
    ]
    completions.response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content="完成。", reasoning_content="文件已读取。", tool_calls=None,
    ))])
    glm.complete(ModelRequest(messages=messages, tools=[tool]))
    assert completions.calls[1]["messages"][1]["reasoning_content"] == "需要查看文件。"


def test_glm_keeps_aggregate_usage_without_inventing_reasoning_tokens():
    usage = SimpleNamespace(prompt_tokens=90, completion_tokens=20, total_tokens=110)
    client, _ = _client(SimpleNamespace(content="完成。", tool_calls=None), usage=usage)

    response = GLMClient(
        api_key="key", client=client, reasoning_enabled=True, reasoning_effort="max",
    ).complete(ModelRequest(messages=[{"role": "user", "content": "inspect"}]))

    assert response.usage.input_tokens == 90
    assert response.usage.output_tokens == 20
    assert response.usage.total_tokens == 110
    assert response.usage.reasoning_tokens is None


def test_glm_53_is_always_reasoning_and_supports_low_high_max():
    client, completions = _client(SimpleNamespace(content="ok", reasoning_content="think", tool_calls=None))
    glm = GLMClient(model="glm-5.3", api_key="key", client=client,
                    reasoning_enabled=True, reasoning_effort="low")

    response = glm.complete(ModelRequest(messages=[{"role": "user", "content": "hi"}]))

    assert response.reasoning_content == "think"
    assert completions.calls[0]["reasoning_effort"] == "low"
    assert completions.calls[0]["extra_body"] == {
        "thinking": {"type": "enabled", "clear_thinking": True},
    }
    with pytest.raises(ValueError, match="does not support reasoning disabled"):
        GLMClient(model="glm-5.3", api_key="key", client=client)


def test_glm_reasoning_defaults_to_high():
    client, completions = _client(SimpleNamespace(
        content="ok", reasoning_content="think", tool_calls=None,
    ))

    GLMClient(
        model="glm-5.3", api_key="key", client=client, reasoning_enabled=True,
    ).complete(ModelRequest(messages=[{"role": "user", "content": "hi"}]))

    assert completions.calls[0]["reasoning_effort"] == "high"
