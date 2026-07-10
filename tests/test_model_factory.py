import pytest

from codeagent.config import ModelConfig
from codeagent.model_gateway.deepseek_client import DeepSeekClient
from codeagent.model_gateway.factory import build_model_client
from codeagent.model_gateway.fake import FakeLLM


def test_model_factory_builds_fake():
    client = build_model_client(ModelConfig(provider="fake"))

    assert isinstance(client, FakeLLM)


def test_model_factory_deepseek_requires_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        build_model_client(ModelConfig(provider="deepseek"))


def test_deepseek_default_model_with_injected_client():
    client = DeepSeekClient(api_key="test-key", client=object())

    assert client.model == "deepseek-v4-flash"
