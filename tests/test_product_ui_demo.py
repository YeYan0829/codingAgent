"""可加载的UI验收包：真实Session/RPC/受控编辑与Bubblewrap，不注入Webview状态。"""
import json
import os
import subprocess

import pytest

from codeagent.config import ModelConfig
from codeagent.model_gateway.base import ModelRequest
from codeagent.model_gateway.factory import build_model_client
from codeagent.product.service import ProductApplicationService
from codeagent.product.ui_demo import create_ui_demo, _wait


def test_ui_demo_refuses_existing_content_before_any_write(tmp_path):
    root = tmp_path / "existing"
    root.mkdir()
    keep = root / "keep.txt"
    keep.write_text("user content")
    with pytest.raises(RuntimeError, match="must be empty"):
        create_ui_demo(root)
    assert list(root.iterdir()) == [keep]
    assert keep.read_text() == "user content"


def test_ui_fixture_requires_explicit_model_and_demo_marker():
    client = build_model_client(ModelConfig(provider="fake", model="ui-fixture"))
    request = ModelRequest(messages=[{"role": "user", "content": "ui:edit"}])
    first = client.complete(request)
    assert first.tool_calls[0].name == "read_file"
    assert first.tool_calls[0].arguments == {"path": "UI_DEMO.json"}
    request.messages.append({"role": "tool", "tool_call_id": first.tool_calls[0].call_id,
                             "content": json.dumps({"ok": False, "error_code": "not_found"})})
    refused = client.complete(request)
    assert not refused.tool_calls and "未启动" in refused.text
    assert type(build_model_client(ModelConfig(provider="fake"))) is not type(client)


@pytest.mark.skipif(not os.environ.get("CODEAGENT_TEST_BWRAP"), reason="需要显式启用真实Bubblewrap")
def test_ui_demo_loads_from_fresh_rpc_and_live_approval_uses_real_gate(tmp_path):
    root = tmp_path / "demo pack with spaces"
    result = create_ui_demo(root)
    assert result["sampleCount"] == result["discoveredCount"] == 8
    manifest = json.loads((root / "samples.json").read_text())
    samples = {s["number"]: s for s in manifest["samples"]}
    service = ProductApplicationService(root / "sessions")
    details = {number: service.get_session(sample["sessionId"]) for number, sample in samples.items()}
    assert len(details[2]["turns"]) == 2
    assert "| 场景 |" in details[3]["turns"][0]["items"][-1]["text"]
    assert any(run["status"] == "failed" for task in details[4]["orchestration"]["tasks"] for run in task["runs"])
    assert any(e.type == "command_completed" and e.payload.get("exit_code") == 2
               for e in service._load(samples[4]["sessionId"]).read_events())
    assert any(e.type == "execution_stop_acknowledged" for e in service._load(samples[5]["sessionId"]).read_events())
    assert details[6]["globalAttention"]["kind"] == "recovery" and not details[6]["availableActions"]["canUndo"]
    assert not details[7]["turns"] and not details[8]["turns"]
    # 跨进程验证真实持久格式及可执行入口，不复用生成时的监管器或内存状态。
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "session/list", "params": {"workspace": result["workspace"]}},
        {"jsonrpc": "2.0", "id": 2, "method": "session/get", "params": {"sessionId": samples[1]["sessionId"]}},
        {"jsonrpc": "2.0", "id": 3, "method": "edits/file", "params": {"sessionId": samples[1]["sessionId"], "path": "src/orders/totals.py"}},
    ]
    rpc = subprocess.run([str(root / "run-rpc"), "--session-root", result["sessionRoot"]],
                         input="".join(json.dumps(r) + "\n" for r in requests), text=True,
                         capture_output=True, timeout=30, check=True)
    responses = {r["id"]: r for r in map(json.loads, rpc.stdout.splitlines()) if "id" in r}
    assert len(responses[1]["result"]["sessions"]) == 8
    assert responses[2]["result"]["workspaceEdits"]["fileCount"] == 3
    assert "count == 0" in responses[3]["result"]["after"]
    assert "count == 0" not in responses[3]["result"]["before"]
    original_index = (root / "workspace/.git/index").read_bytes()
    try:
        for allowed in (False, True):
            sid = samples[8]["sessionId"]
            service.run_session(sid, "ui:approval")
            pending = _wait(lambda: service.supervisor.pending_approval(sid), timeout=40, description="approval")
            assert pending["kind"] == "command_permissions"
            detail = service.get_session(sid)
            assert detail["executionState"] == "awaiting_approval" and detail["availableActions"]["canResolveApproval"]
            run = next(r for task in detail["orchestration"]["tasks"] for r in task["runs"] if r["runId"] == pending["owner"]["runId"])
            assert run["role"] == "bash-runner" and run["awaitingApproval"]
            service.resolve_approval(sid, pending["approvalId"], allowed)
            _wait(lambda: service.supervisor.state(sid) == "idle", timeout=40)
            assert service.get_session(sid)["pendingApproval"] is None
        commands = [e.payload for e in service._load(sid).read_events() if e.type == "command_completed"]
        assert any(e.get("exit_code") == 0 for e in commands)
        assert (root / "workspace/.git/index").read_bytes() == original_index
        detail = service.get_session(samples[1]["sessionId"])
        edit = detail["workspaceEdits"]
        service.undo_edits(samples[1]["sessionId"], edit["turnId"], edit["lastEditSeq"])
        assert "count == 0" not in (root / "workspace/src/orders/totals.py").read_text()
        assert (root / "workspace/.git/index").read_bytes() == original_index
    finally:
        service.supervisor.shutdown()
