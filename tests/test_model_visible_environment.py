"""核对最终 SDK 请求和实际续读；替身验证协议，不声称真实模型质量。"""
import copy
import json
from pathlib import Path
from types import SimpleNamespace as Obj

import pytest

from codeagent.application.session_runtime import build_agent_runner
from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.context.builder import ContextManager
from codeagent.context.models import ContextHistoryItem
from codeagent.model_gateway.base import ModelRequest
from codeagent.model_gateway.glm_client import GLMClient
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.request_executor import RequestExecutor
from test_orchestration import repo_store, events


class EnvironmentSDK:
    def __init__(self):
        self.chat = Obj(completions=self)

    def with_options(self, **kwargs):
        assert kwargs == {"max_retries": 0}
        return self

    def create(self, **kwargs):
        messages = kwargs["messages"]
        folder = Path(json.loads(next(m["content"] for m in messages if m["role"] == "user"))["capture"])
        index = len(list(folder.glob("request-*.json")))
        (folder / f"request-{index}.json").write_text(json.dumps(kwargs, ensure_ascii=False))
        notices = [m["content"] for m in messages if m["role"] == "system" and m["content"].startswith("当前执行余量（")]
        assert not notices
        assert not any("预算收尾" in str(m.get("content")) for m in messages)
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        calls = []
        if not results:
            for name in ["a.py", "b.py"]:
                calls.append(Obj(id=name, function=Obj(name="read_file", arguments=json.dumps({"path": name}))))
        elif index == 1:
            for result in results:
                meta = result["metadata"]
                if not result["truncated"]:
                    assert not meta["has_more_after"]
                    continue
                assert result["truncated"] and meta["has_more_after"]
                assert "model_visible_range" not in meta and "end_line" not in meta
                assert meta["next_start_line"] == meta["returned_range"]["end_line"] + 1
                calls.append(Obj(id=meta["path"]+"-tail", function=Obj(name="read_file", arguments=json.dumps({
                    "path": meta["path"], "start_line": meta["next_start_line"]}))))
        else:
            assert all(not r["truncated"] and not r["metadata"]["has_more_after"] for r in results[-2:])
        return Obj(id=f"offline-{index}", choices=[Obj(message=Obj(content="done" if not calls else "", tool_calls=calls),
                   finish_reason="tool_calls" if calls else "stop")],
                   usage=Obj(prompt_tokens=100, completion_tokens=20, total_tokens=120))


def environment_factory(config):
    return GLMClient(model=config.model, client=EnvironmentSDK(), reasoning_enabled=True)


@pytest.mark.parametrize("lines,request_count", [(316, 2), (1100, 3)])
def test_final_sdk_preserves_batch_results_and_tool_continuations(repo_store, tmp_path, lines, request_count):
    root, store = repo_store
    text = "".join(f"{i:04d} " + "x" * 32 + "\n" for i in range(1, lines + 1))
    for name in ["a.py", "b.py"]:
        (root / name).write_text(text)
    folder = tmp_path / "sdk"
    folder.mkdir()
    runner = build_agent_runner(store, model_config=ModelConfig(provider="glm", model="glm-5.3", reasoning_enabled=True),
        runtime_config=RuntimeConfig(cost_per_token_microunits=1),
        approval_gate=AutoApprovalGate(False), model_factory=environment_factory)
    result = runner.run_turn(json.dumps({"capture": str(folder)}))
    assert result.status == "completed", result
    requests = [json.loads(p.read_text()) for p in sorted(folder.glob("request-*.json"))]
    assert len(requests) == request_count
    observed = [json.loads(m["content"]) for m in requests[-1]["messages"] if m["role"] == "tool"]
    for name in ["a.py", "b.py"]:
        pieces = [r["content"] for r in observed if r["metadata"]["path"] == name]
        assert "\n".join(pieces) == text.rstrip("\n")
    raw = events(store, "tool_result")[0].payload["result"]
    assert raw["metadata"]["returned_range"] == {"start_line": 1, "end_line": min(lines, 1000)}
    assert raw["truncated"] == (lines > 1000) and "context_truncated" not in raw
    schemas = {t["function"]["name"]: t["function"] for t in requests[0]["tools"]}
    include_schema = schemas["search_text"]["parameters"]["properties"]["include"]
    assert include_schema["type"] == "array" and include_schema["items"]["type"] == "string"
    bad = runner.tools.call("search_text", {"query": "x", "include": "*.py"})
    assert not bad.ok and "include" in bad.error


@pytest.mark.parametrize("content,expected", [("", None), ("one\ntwo", {"start_line": 1, "end_line": 2})])
def test_complete_and_empty_reads_have_one_range(repo_store, content, expected):
    root, store = repo_store
    (root / "small.py").write_text(content)
    from codeagent.tools.fs_read import build_fs_tools
    read = next(t for t in build_fs_tools(store.workspace_context()) if t.name == "read_file")
    raw = read.handler({"path": "small.py"}).model_dump()
    original = copy.deepcopy(raw)
    item = ContextHistoryItem(1, "event:1", "tool_result", "turn", "step", "atom", {"name": "read_file", "result": raw})
    result = ContextManager._model_item_payload(item)["result"]
    assert result["metadata"]["returned_range"] == expected
    assert result["metadata"]["next_start_line"] is None and not result["metadata"]["has_more_after"]
    assert not result["truncated"] and raw == original






def test_projected_utf8_fragments_resume_without_loss(repo_store):
    from codeagent.tools.fs_read import build_fs_tools
    root, store = repo_store
    content = "猫" * 25_000 + "\nend\n"
    (root / "wide.py").write_text(content)
    read = next(t for t in build_fs_tools(store.workspace_context()) if t.name == "read_file")
    initial = read.handler({"path": "wide.py"})
    args = {"path": "wide.py", "start_byte": 0, "expected_sha256": initial.metadata["sha256"]}
    pieces = []
    for _ in range(10):
        raw = read.handler(args).model_dump()
        item = ContextHistoryItem(1, "event:1", "tool_result", "turn", "step", "atom", {"name": "read_file", "result": raw})
        projected = ContextManager._model_item_payload(item)["result"]
        assert projected["content"] == raw["content"] and "context_truncated" not in projected
        pieces.append(projected["content"])
        meta = projected["metadata"]
        assert meta["line_fragment"] and "model_visible_range" not in meta
        if not meta["has_more_after"]:
            break
        assert projected["truncated"] and meta["next_start_byte"] > args["start_byte"]
        args["start_byte"] = meta["next_start_byte"]
    assert "".join(pieces) == content
