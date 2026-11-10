"""清单只维护可选进度，不维护代码版本、验证资格或最终回复门槛。"""
import json
import pytest
from codeagent.runtime.task_plan import TaskPlanService
from codeagent.session.store import SessionStore
from codeagent.tools.task_plan import build_task_plan_tools


@pytest.fixture
def active_plan_store(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    store = SessionStore(root).create()
    store.append_event('user_message', {'message': 'finish work'})
    return store


def test_plan_can_be_created_completed_changed_and_removed_without_pre_actions(active_plan_store):
    service = TaskPlanService(active_plan_store)
    assert service.update({'tasks': [{'title': '检查入口', 'status': 'done'}]}).ok
    assert service.view()['tasks'] == [{'title': '检查入口', 'status': 'done', 'notes': ''}]
    assert service.update({'tasks': [{'title': '补充说明', 'status': 'todo', 'notes': '环境缺失'}]}).ok
    assert service.update({'tasks': []}).ok
    assert service.view()['tasks'] == []
    assert not any(e.type in {'validation_completed', 'task_plan_final_deferred'} for e in active_plan_store.read_events())


def test_plan_survives_restart_without_reading_or_restoring_code(active_plan_store, monkeypatch):
    assert TaskPlanService(active_plan_store).update({'tasks': [{'title': '保留结果'}]}).ok
    restored = SessionStore(active_plan_store.workspace_root, active_plan_store.session_id).load()
    monkeypatch.setattr(restored, 'workspace_context', lambda: pytest.fail('进度读取不应读取或恢复代码版本'))
    assert TaskPlanService(restored).read({}).metadata['plan']['tasks'][0]['title'] == '保留结果'
    restored.append_event('assistant_message', {'message': 'done'})
    restored.append_event('user_message', {'message': 'new task'})
    assert TaskPlanService(restored).view() is None


def test_historical_plan_keeps_progress_without_restoring_proof_fields(active_plan_store):
    active_plan_store.append_event('task_plan_updated', {'revision': 4, 'tasks': [{
        'id': 'old', 'title': '历史验证', 'status': 'completed', 'note': '原始记录',
        'kind': 'verification', 'validation_execution_ids': ['old-check'],
    }], 'completion_review_subject': {'subject_tree': 'old-tree'}})
    data = TaskPlanService(active_plan_store).read({}).metadata
    assert data['plan']['tasks'] == [{'title': '历史验证', 'status': 'done', 'notes': '原始记录'}]
    assert 'old-tree' not in json.dumps(data)
    assert active_plan_store.read_events()[-1].payload['completion_review_subject']['subject_tree'] == 'old-tree'


def test_plan_schema_has_no_eligibility_protocol(active_plan_store):
    tools = {t.name: t for t in build_task_plan_tools(TaskPlanService(active_plan_store))}
    schema = tools['update_task_plan'].schema
    text = json.dumps(schema)
    assert not any(name in text for name in ['expected_revision', 'request_key', 'completion_review', 'validation_execution_ids'])
    before = active_plan_store.events_path.read_bytes()
    assert not TaskPlanService(active_plan_store).update({'tasks': [{'title': ' '}]}).ok
    assert active_plan_store.events_path.read_bytes() == before
