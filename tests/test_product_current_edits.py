"""新版插件真实产品入口：当前编辑、并发确认守卫、公开子过程。"""
import json
import pytest
from codeagent.product.rpc import JsonRpcServer
from codeagent.product.service import ProductApplicationService, ProductServiceError
from codeagent.runtime.atomic_edit import AtomicEditService
from runtime_fixtures import execution_session, edit_sort


def request(server, method, **params):
    return server.dispatch({'jsonrpc':'2.0','id':1,'method':method,'params':params})


def test_current_diff_and_undo_use_real_transaction_without_touching_git(tmp_path):
    repo,store,context=execution_session(tmp_path)
    original_index=(repo/'.git/index').read_bytes()
    store.append_event('user_message',{'message':'fix'})
    edit_sort(store,context)
    server=JsonRpcServer(ProductApplicationService(store.session_root))
    sid=store.session_id
    detail=request(server,'session/get',sessionId=sid)['result']
    edit=detail['workspaceEdits']
    assert edit['available'] and detail['availableActions']['canUndo']
    diff=request(server,'edits/file',sessionId=sid,path='sort_utils.py')['result']
    assert 'reverse=True' in diff['before'] and 'reverse=True' not in diff['after']
    assert 'error' in request(server,'edits/file',sessionId=sid,path='../secrets')
    result=request(server,'edits/undo',sessionId=sid,turnId=edit['turnId'],expectedEditSeq=edit['lastEditSeq'])
    assert result['result']['undone']
    assert 'reverse=True' in (repo/'sort_utils.py').read_text()
    assert (repo/'.git/index').read_bytes()==original_index
    edits = request(server,'session/get',sessionId=sid)['result']['workspaceEdits']
    assert not edits['available']
    assert edits['lastUndo'] == {'turnId': edit['turnId'], 'fileCount': 1}
    # 提示来自持久事务，重建服务仍可读；后续编辑不再显示旧成功提示。
    reloaded = ProductApplicationService(store.session_root)
    assert reloaded.get_session(sid)['workspaceEdits']['lastUndo'] == edits['lastUndo']
    edit_sort(store,context)
    assert reloaded.get_session(sid)['workspaceEdits']['lastUndo'] is None


def test_undo_rejects_new_edits_after_confirmation_and_external_changes(tmp_path):
    repo,store,context=execution_session(tmp_path)
    edit_sort(store,context)
    service=ProductApplicationService(store.session_root)
    before=service.get_session(store.session_id)['workspaceEdits']
    edit_sort(store,context,old='sorted(values)',new='list(values)')
    with pytest.raises(ProductServiceError,match='变化'):
        service.undo_edits(store.session_id,before['turnId'],before['lastEditSeq'])
    after=service.get_session(store.session_id)['workspaceEdits']
    (repo/'sort_utils.py').write_text('external user changes\n')
    with pytest.raises(ProductServiceError,match='后续操作改变'):
        service.undo_edits(store.session_id,after['turnId'],after['lastEditSeq'])
    assert (repo/'sort_utils.py').read_text()=='external user changes\n'
    assert service.get_session(store.session_id)['workspaceEdits']['lastUndo'] is None


def test_process_endpoint_filters_private_fields_and_pages_public_items(tmp_path):
    _,store,_=execution_session(tmp_path)
    def emit(kind,payload):
        return store.append_derived_events([(kind,payload)],turn_id='one')[0]
    emit('user_message',{'message':'parent'})
    emit('agent_command_intent',{'action':'spawn','request_key':'x','command_id':'c','execution':'foreground',
        'agent':{'agent_id':'a','spec':{'name':'code-explorer'}},'run':{'run_id':'r','agent_id':'a','root_turn_id':'one','message':'goal','description':'查找入口','execution_epoch':1}})
    emit('agent_run_running',{'run_id':'r'})
    for key,message in [('assistant_tool_calls','公开说明'),('assistant_message','公开结果')]:
        emit('agent_history',{'run_id':'r','agent_id':'a','kind':key,'data':{'message':message,'reasoning_content':'PRIVATE','tool_calls':[]}})
    service=ProductApplicationService(store.session_root)
    page=service.get_process(store.session_id,'r',limit=1)
    assert page['hasMore'] and len(page['items'])==1
    rest=service.get_process(store.session_id,'r',after_seq=page['lastSeq'],limit=1)
    assert not rest['hasMore'] and '公开结果' in json.dumps(rest,ensure_ascii=False)
    assert 'PRIVATE' not in json.dumps(page)+json.dumps(rest)
    with pytest.raises(ProductServiceError,match='unknown runId'):
        service.get_process(store.session_id,'other')


def test_recovery_and_active_owner_disable_undo(tmp_path):
    _,store,context=execution_session(tmp_path)
    edit_sort(store,context)
    store.mark_recovery_required('unknown write')
    service=ProductApplicationService(store.session_root)
    detail=service.get_session(store.session_id)
    assert not detail['availableActions']['canUndo']
    with pytest.raises(ProductServiceError):
        service.undo_edits(store.session_id,detail['workspaceEdits']['turnId'],detail['workspaceEdits']['lastEditSeq'])


def test_approval_owner_is_derived_from_inflight_child_operation(tmp_path):
    import threading
    import time
    from codeagent.product.execution import ExecutionSupervisor, ExecutionRecord
    _,store,_=execution_session(tmp_path)
    store.append_event('agent_command_intent',{'run':{'run_id':'child-run','agent_id':'child'}})
    store.append_event('tool_execution_intent',{'operation_key':'child-run:0:tool:0','call':{'call_id':'child-call'}})
    supervisor=ExecutionSupervisor()
    record=ExecutionRecord('job','running',store=store)
    supervisor._records[store.session_id]=record
    result=[]
    thread=threading.Thread(target=lambda:result.append(supervisor.await_approval(store,record,'tool',{'tool':'run_command'})))
    thread.start()
    try:
        deadline=time.monotonic()+3
        while supervisor.pending_approval(store.session_id) is None and time.monotonic()<deadline:
            time.sleep(.01)
        pending=supervisor.pending_approval(store.session_id)
        assert pending['owner']=={'callId':'child-call','runId':'child-run','agentId':'child'}
        supervisor.resolve_approval(store.session_id,pending['approvalId'],False)
        thread.join(3)
        assert result==[False] and supervisor.pending_approval(store.session_id) is None
    finally:
        if thread.is_alive():
            record.stop_requested=True
            with supervisor._condition:
                supervisor._condition.notify_all()
            thread.join(3)
