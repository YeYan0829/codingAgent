"""确定性跨进程复现半行事件：完整读取，不忽略或覆盖损坏数据。"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import fcntl
import multiprocessing
from pathlib import Path
import threading

import pytest

from codeagent.session.store import SessionStore, SessionStoreError


def paused_append(workspace, session_id, session_root, derived, ready, release):
    store = SessionStore(workspace, session_id, session_root)
    original = Path.open

    class SplitWriter:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def write(self, text):
            cut = len(text) // 2
            self.handle.write(text[:cut])
            self.handle.flush()
            ready.set()
            if not release.wait(10):
                raise TimeoutError('test writer was not released')
            self.handle.write(text[cut:])
            return len(text)

    def open_path(path, mode='r', *args, **kwargs):
        handle = original(path, mode, *args, **kwargs)
        return SplitWriter(handle) if path == store.events_path and mode == 'a' else handle

    Path.open = open_path
    try:
        if derived:
            store.append_derived_events([('agent_run_finished', {'findings': '完整调查原文🙂' * 100})], turn_id=None)
        else:
            store.append_event('assistant_message', {'message': '完整调查原文🙂' * 100})
    finally:
        Path.open = original


@pytest.mark.parametrize('derived', [False, True])
def test_read_waits_for_another_process_to_finish_append(tmp_path, derived):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    store = SessionStore(workspace, session_root=tmp_path / 'sessions').create()
    ctx = multiprocessing.get_context('spawn')
    ready, release = ctx.Event(), ctx.Event()
    writer = ctx.Process(target=paused_append, args=(
        store.workspace_root, store.session_id, store.session_root, derived, ready, release))
    writer.start()
    try:
        assert ready.wait(10)
        # 两条实际追加入口均须持有跨进程独占锁，不能仅靠线程 RLock。
        with store.events_path.open() as handle:
            with pytest.raises(BlockingIOError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        started = threading.Event()

        def read():
            started.set()
            return store.read_events()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(read)
            try:
                assert started.wait(5)
                with pytest.raises(TimeoutError):
                    future.result(timeout=.05)
            finally:
                release.set()
            events = future.result(timeout=10)
        assert len(events) == 2
        assert ('完整调查原文🙂' * 100) in events[-1].payload.values()
        writer.join(10)
        assert writer.exitcode == 0
    finally:
        release.set()
        if writer.is_alive():
            writer.terminate()
        writer.join(5)
        writer.close()


def test_unlocked_corrupt_tail_still_fails_without_discarding_bytes(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    store = SessionStore(workspace, session_root=tmp_path / 'sessions').create()
    with store.events_path.open('ab') as stream:
        stream.write(b'{"type":')
    before = store.events_path.read_bytes()
    with pytest.raises(SessionStoreError, match='corrupted'):
        store.read_events()
    assert store.events_path.read_bytes() == before
