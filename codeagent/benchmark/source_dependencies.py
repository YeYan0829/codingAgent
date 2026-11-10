"""只用于重建实验输入缓存的本地子模块复制；正常Runtime不导入此模块。"""
import os
import subprocess
from pathlib import Path


def git(root, args):
    result = subprocess.run(['git', *args], cwd=root, env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'},
        capture_output=True, check=False, timeout=30)
    if result.returncode:
        raise ValueError(result.stderr.decode('utf-8', 'replace').strip())
    return result


def copy_submodules(source: Path, destination: Path, revision: str):
    for record in git(source, ['ls-tree', '-rz', revision]).stdout.split(b'\0'):
        if not record: continue
        info, raw = record.split(b'\t', 1)
        if info.split()[0] != b'160000': continue
        relative, oid = Path(os.fsdecode(raw)), info.split()[2].decode()
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('实验子模块路径越界')
        origin, target = source / relative, destination / relative
        if origin.is_symlink():
            raise ValueError('实验子模块不能是符号链接')
        if not (origin / '.git').exists():
            if origin.exists() and any(origin.iterdir()):
                raise ValueError('实验子模块有内容但缺少Git来源')
            continue
        if git(origin, ['rev-parse', 'HEAD']).stdout.decode().strip() != oid or git(origin, ['status', '--porcelain']).stdout.strip():
            raise ValueError('实验缓存子模块与数据集输入不一致')
        if not (target / '.git').exists():
            git(destination, ['-c', 'core.hooksPath=/dev/null', 'clone', '--no-local', '--single-branch', '--no-tags', '--no-checkout', '--', str(origin), str(target)])
            git(target, ['-c', 'core.hooksPath=/dev/null', 'checkout', '--detach', oid])
        copy_submodules(origin, target, oid)
        if git(target, ['rev-parse', 'HEAD']).stdout.decode().strip() != oid or git(target, ['status', '--porcelain']).stdout.strip():
            raise ValueError('实验子模块复制不完整')
