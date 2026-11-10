import pytest


@pytest.fixture(autouse=True)
def isolated_session_root(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEAGENT_SESSION_ROOT", str(tmp_path / "codeagent-sessions"))


def runtime_view(messages):
    """合并前缀中的工作区/环境合同与尾部“当前 Runtime 状态”，供夹具按模型实际可见内容选择动作。"""
    import json
    contract = next(json.loads(m["content"].split("\n", 1)[1])["runtime_context"] for m in messages
                    if m["role"] == "system" and m.get("content", "").startswith("以下是 Runtime 当前工作目录与可用环境"))
    state = next(json.loads(m["content"].split("\n", 1)[1]) for m in reversed(messages)
                 if m["role"] == "system" and m.get("content", "").startswith("当前 Runtime 状态"))
    return {**contract, **state}
