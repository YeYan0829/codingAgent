"""用户显式仓库路径限制。配置存储在 Session Root，模型没有修改入口。"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from codeagent.config import default_session_root


class RepositoryAccessError(ValueError):
    pass


def normalize_paths(value) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > 100:
        raise RepositoryAccessError('禁止路径必须是最多100项的列表')
    paths = set()
    for raw in value:
        if not isinstance(raw, str) or not raw or len(raw) > 1024 or any(c in raw for c in '\x00\r\n\\*?[]'):
            raise RepositoryAccessError('请填写相对文件或目录路径，不支持通配符、反斜线或空路径')
        path = Path(raw)
        if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] == '.git':
            raise RepositoryAccessError('禁止路径须位于仓库内，不能是根目录、.git或包含..')
        paths.add(path.as_posix())
    if sum(len(p) for p in paths) > 8192:
        raise RepositoryAccessError('禁止路径总长度不能超过8192字符')
    return tuple(sorted(p for p in paths if not any(parent.as_posix() in paths for parent in Path(p).parents)))


class RepositoryAccessStore:
    def __init__(self, workspace, session_root=None):
        self.workspace = Path(workspace).expanduser().resolve()
        root = Path(session_root or default_session_root()).expanduser().resolve()
        self.root = root / 'repository-access'
        self.key = hashlib.sha256(str(self.workspace).encode()).hexdigest()
        self.path = self.root / (self.key + '.json')

    @contextmanager
    def lock(self, *, exclusive=False):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / (self.key + '.lock')).open('a') as stream:
            try:
                fcntl.flock(stream, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RepositoryAccessError('仓库正在执行任务或保存访问设置；请停止任务后重试') from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def read(self):
        if not self.path.exists():
            return {'configured': False, 'revision': '0', 'deniedPaths': [], 'workspace': str(self.workspace)}
        try:
            result = json.loads(self.path.read_text())
            if result['workspace'] != str(self.workspace) or not isinstance(result['revision'], str):
                raise ValueError('identity')
            result['deniedPaths'] = list(normalize_paths(result['deniedPaths']))
            result['configured'] = True
            return result
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RepositoryAccessError(f'仓库访问配置损坏；未放行，请检查或从备份恢复：{self.path}') from exc

    def save(self, paths, expected_revision):
        paths = normalize_paths(paths)
        with self.lock(exclusive=True):
            old = self.read()
            if old['revision'] != expected_revision:
                raise RepositoryAccessError('仓库设置已被其他窗口修改，请刷新后重试')
            if self.root.is_relative_to(self.workspace):
                raise RepositoryAccessError('Session Root 必须位于仓库之外才能保存访问设置')
            for value in paths:
                path = self.workspace / value
                if not path.exists() or not (path.is_file() or path.is_dir()):
                    raise RepositoryAccessError(f'禁止路径不存在或不是普通文件/目录：{value}')
                if any(p.is_symlink() for p in (path, *path.parents) if p != self.workspace and p.is_relative_to(self.workspace)):
                    raise RepositoryAccessError(f'禁止路径不能经过符号链接：{value}')
            if old['configured'] and old['deniedPaths'] == list(paths):
                return old
            result = {'configured': True, 'workspace': str(self.workspace), 'deniedPaths': list(paths),
                      'revision': old['revision'] if not old['configured'] and not paths else uuid.uuid4().hex}
            temp = self.path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
            try:
                with temp.open('x') as stream:
                    os.chmod(temp, 0o600)
                    json.dump(result, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                temp.replace(self.path)
            finally:
                temp.unlink(missing_ok=True)
            return result


def assert_session_access(store):
    current = RepositoryAccessStore(store.workspace_root, store.session_root).read()
    if store.read_meta().get('repository_access_revision', '0') != current['revision']:
        raise RepositoryAccessError('仓库访问设置已变更；旧会话可能含已禁止内容，请新建任务。原修改仍可审查。')


@contextmanager
def session_access_lease(store):
    with RepositoryAccessStore(store.workspace_root, store.session_root).lock():
        assert_session_access(store)
        yield


def enforce_repository_access(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with session_access_lease(self.session_store):
            return method(self, *args, **kwargs)
    return wrapped
