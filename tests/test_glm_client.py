from types import SimpleNamespace

from codeagent.model_gateway.base import ModelRequest, ModelTool
from codeagent.model_gateway.glm_client import DEFAULT_GLM_MODEL, GLMClient


class FakeCompletions:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _client(message):
    completions = FakeCompletions(SimpleNamespace(choices=[SimpleNamespace(message=message)]))
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


def test_glm_defaults_and_disables_unpersisted_thinking():
    client, completions = _client(SimpleNamespace(content="ok", tool_calls=None))
    glm = GLMClient(api_key="key", client=client)

    response = glm.complete(ModelRequest(messages=[{"role": "user", "content": "hi"}]))

    assert glm.model == DEFAULT_GLM_MODEL == "glm-5.2"
    assert response.text == "ok"
    assert completions.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}


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
