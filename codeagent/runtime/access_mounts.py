"""为未复制的 ignored 禁止路径建立临时空挂载点，不把它们纳入候选修改。"""
from __future__ import annotations

import os
import uuid
from contextlib import contextmanager


@contextmanager
def denied_mountpoints(context, store):
    if not context.denied_paths:
        yield
        return
    operation = uuid.uuid4().hex
    created = []
    store.append_event('access_mounts_preparing', {'operation': operation, 'paths': list(context.denied_paths)})
    try:
        for relative in context.denied_paths:
            target = context.active_root / relative
            chain = []
            cursor = target
            while cursor != context.active_root:
                if cursor.is_symlink():
                    raise ValueError('禁止路径的挂载点不能经过符号链接')
                if not cursor.exists():
                    chain.append(cursor)
                cursor = cursor.parent
            for path in reversed(chain):
                directory = path != target or (context.source_root / relative).is_dir()
                if directory:
                    path.mkdir()
                else:
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    os.close(fd)
                info = path.stat()
                created.append((path, directory, info.st_dev, info.st_ino, info.st_mtime_ns))
        yield
    finally:
        try:
            for path, directory, device, inode, mtime in reversed(created):
                info = path.lstat()
                if (info.st_dev, info.st_ino) != (device, inode) or path.is_symlink():
                    raise ValueError('临时挂载点已被外部替换')
                if directory:
                    path.rmdir()  # 非空则保留，不能递归删除用户或外部程序写入的数据。
                elif info.st_size == 0 and info.st_mtime_ns == mtime:
                    path.unlink()
                else:
                    raise ValueError('临时挂载点已被外部修改')
            store.append_event('access_mounts_cleaned', {'operation': operation})
        except (OSError, ValueError) as exc:
            store.mark_recovery_required('禁止路径挂载点清理未确认；保留现场，请人工检查')
            raise ValueError('禁止路径挂载点清理未确认；命令结果不能作为验证证据') from exc


def recover_access_mounts(store):
    pending = set()
    for event in store.read_events():
        if event.type == 'access_mounts_preparing':
            pending.add(event.payload['operation'])
        elif event.type == 'access_mounts_cleaned':
            pending.discard(event.payload['operation'])
    if pending:
        store.mark_recovery_required('检测到未完成的禁止路径挂载；保留空挂载点与候选现场，请人工检查')
