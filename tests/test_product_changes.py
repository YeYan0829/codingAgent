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
    assert service.get_changes(store.session_id)["available"] is False


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
