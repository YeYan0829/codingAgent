"""产品旧读取接口保留历史；当前编辑直接生效，撤销由核心undo处理。"""
import json
import shutil
import pytest
from codeagent.product.rpc import JsonRpcServer
from codeagent.product.service import ProductApplicationService, ProductServiceError
from codeagent.runtime.atomic_edit import AtomicEditService
from runtime_fixtures import execution_session, edit_sort


def test_direct_edits_do_not_require_product_accept(tmp_path):
    repo, store, context = execution_session(tmp_path)
    edit_sort(store, context)
    service = ProductApplicationService(store.session_root)
    assert "reverse=True" not in (repo / "sort_utils.py").read_text()
    detail = service.get_session(store.session_id)
    assert not detail['availableActions']['canAcceptChanges']
    assert not detail['availableActions']['canDiscardChanges']
    assert not service.get_changes(store.session_id)['available']
    assert AtomicEditService(context, store).undo().ok
    assert "reverse=True" in (repo / "sort_utils.py").read_text()


def test_retired_delivery_endpoints_cannot_modify_current_project(tmp_path):
    repo, store, context = execution_session(tmp_path)
    edit_sort(store, context)
    before = (repo / 'sort_utils.py').read_bytes()
    service = ProductApplicationService(store.session_root)
    for method in (service.accept_changes, service.discard_changes):
        with pytest.raises(ProductServiceError):
            method(store.session_id)
    server = JsonRpcServer(service)
    for method in ('changes/accept', 'changes/discard'):
        result = server.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': {'sessionId': store.session_id}})
        assert 'error' in result
    assert (repo / 'sort_utils.py').read_bytes() == before


def test_old_delivery_remains_readable_without_old_worktree(tmp_path):
    repo, store, _ = execution_session(tmp_path)
    delivery_id = 'a' * 32
    directory = store.session_dir / 'deliveries' / delivery_id
    directory.mkdir(parents=True)
    (directory / '0.before').write_text('old\n')
    (directory / '0.after').write_text('accepted\n')
    artifact = {'schemaVersion': 1, 'deliveryId': delivery_id, 'status': 'applied', 'files': [{
        'path': 'sort_utils.py', 'additions': 1, 'deletions': 1, 'previewAvailable': True,
        'beforeArtifact': '0.before', 'afterArtifact': '0.after',
    }]}
    (directory / 'delivery.json').write_text(json.dumps(artifact))
    store.append_event('changes_accepted', {'delivery_id': delivery_id})
    shutil.rmtree(repo)
    service = ProductApplicationService(store.session_root)
    assert service.get_changes(store.session_id)['state'] == 'applied'
    assert service.get_change_file(store.session_id, 'sort_utils.py') == {'path': 'sort_utils.py', 'before': 'old\n', 'after': 'accepted\n'}
