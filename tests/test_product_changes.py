from __future__ import annotations

from codeagent.product.rpc import JsonRpcServer
from codeagent.product.service import ProductApplicationService
from codeagent.runtime.atomic_edit import AtomicEditService
from test_candidate_loop import command_service, execution_session
from test_session_workspace_lifecycle import edit_sort, validate


def test_changes_file_returns_baseline_and_current_text(tmp_path):
    repo, store, context = execution_session(tmp_path)
    edit_sort(store, context)
    service = ProductApplicationService(tmp_path / "sessions")

    changes = service.get_changes(store.session_id)
    snapshot = service.get_change_file(store.session_id, "sort_utils.py")

    assert changes["available"] is True
    assert changes["files"] == ["sort_utils.py"]
    assert changes["additions"] == 1
    assert changes["deletions"] == 1
    assert changes["fileStats"] == [{"path": "sort_utils.py", "additions": 1, "deletions": 1}]
    assert "reverse=True" in snapshot["before"]
    assert "reverse=True" not in snapshot["after"]
    assert "reverse=True" in (repo / "sort_utils.py").read_text(encoding="utf-8")


def test_product_accept_requires_current_validation_and_preserves_identity(tmp_path):
    repo, store, context = execution_session(tmp_path)
    edit_sort(store, context)
    validate(store, context)
    service = ProductApplicationService(tmp_path / "sessions")

    receipt = service.accept_changes(store.session_id)

    assert receipt["status"] == "applied"
    assert "sorted(values)" in (repo / "sort_utils.py").read_text(encoding="utf-8")
    assert store.workspace_context().active_root == context.active_root
    applied = service.get_changes(store.session_id)
    assert applied["available"] is True
    assert applied["state"] == "applied"
    assert applied["files"] == ["sort_utils.py"]
    assert applied["fileStats"] == [{
        "path": "sort_utils.py", "additions": 1, "deletions": 1, "previewAvailable": True,
    }]
    assert applied["canAccept"] is False
    assert applied["canDiscard"] is False
    assert applied["validation"]["historical"] is True
    assert applied["validation"]["appliesToCurrentChanges"] is False

    accepted_diff = service.get_change_file(store.session_id, "sort_utils.py")
    assert "reverse=True" in accepted_diff["before"]
    assert "reverse=True" not in accepted_diff["after"]

    (repo / "sort_utils.py").write_text("user changed source after Accept\n", encoding="utf-8")
    reopened = ProductApplicationService(tmp_path / "sessions")
    reloaded_diff = reopened.get_change_file(store.session_id, "sort_utils.py")
    assert reloaded_diff == accepted_diff
    assert reopened.get_session(store.session_id)["changesSummary"]["state"] == "applied"


def test_product_discard_and_rpc_changes_methods(tmp_path):
    repo, store, context = execution_session(tmp_path)
    edit_sort(store, context)
    server = JsonRpcServer(ProductApplicationService(tmp_path / "sessions"))

    listed = server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "changes/get", "params": {
        "sessionId": store.session_id,
    }})
    discarded = server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "changes/discard", "params": {
        "sessionId": store.session_id,
    }})

    assert listed["result"]["files"] == ["sort_utils.py"]
    assert discarded["result"]["status"] == "discarded"
    assert "reverse=True" in (repo / "sort_utils.py").read_text(encoding="utf-8")
    assert "reverse=True" in (context.active_root / "sort_utils.py").read_text(encoding="utf-8")

    reopened = ProductApplicationService(tmp_path / "sessions")
    reloaded = reopened.get_changes(store.session_id)
    detail = reopened.get_session(store.session_id)
    assert reloaded["state"] == "discarded"
    assert reloaded["available"] is False
    assert detail["deliveryReceipt"]["kind"] == "discarded"
    assert detail["availableActions"]["canAcceptChanges"] is False
    assert detail["availableActions"]["canDiscardChanges"] is False
