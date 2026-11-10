"""大仓库子快照：真实 Git、模型工具循环、资源失败回收；不调用付费模型。"""
import errno
import json
from pathlib import Path
import subprocess
import tracemalloc

import pytest

from codeagent.model_gateway.base import BaseModelClient, LLMResponse, LLMToolCall
from codeagent.session.store import SessionStore
from runtime_fixtures import git, make_bug_repo
from test_orchestration import service_for, spawn_args, report


class TailReader(BaseModelClient):
    def complete(self, request):
        observations = [m for m in request.messages if m['role'] == 'tool']
        if not observations:
            return LLMResponse(tool_calls=[LLMToolCall(call_id='tail', name='read_file',
                arguments={'path': 'many/06000.py'})], finish_reason='tool_calls')
        assert 'LAST_FILE_MARKER' in str(observations[-1]['content'])
        return LLMResponse(text='Read LAST_FILE_MARKER from many/06000.py', finish_reason='stop')


def tail_factory(config):
    return TailReader()


def test_graph_child_reads_beyond_old_5000_file_limit(tmp_path):
    repo = make_bug_repo(tmp_path)
    folder = repo / 'many'; folder.mkdir()
    for index in range(6001):
        (folder / f'{index:05}.py').write_text('LAST_FILE_MARKER\n' if index == 6000 else 'VALUE = 1\n')
    git(repo, 'add', '.'); git(repo, 'commit', '-m', 'many small files')
    head = git(repo, 'rev-parse', 'HEAD')
    store = SessionStore(repo, session_root=tmp_path/'sessions').create()
    service = service_for(store, factory=tail_factory)
    result = report(service.spawn(spawn_args()))
    assert 'LAST_FILE_MARKER' in str(result)
    assert len([e for e in store.read_events() if e.type == 'model_request_reserved']) == 2
    assert git(repo, 'rev-parse', 'HEAD') == head
    assert not git(repo, 'status', '--porcelain')
    created = [Path(e.payload['path']) for e in store.read_events() if e.type == 'agent_snapshot_created']
    assert not created
