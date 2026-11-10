"""独立实验clone恢复同一prepared镜像的构建依赖；不进入普通Runtime。"""
from __future__ import annotations

import hashlib
import os
from pathlib import PurePosixPath
import stat
import subprocess

from codeagent.workspace.workspace import WorkspaceContext
from codeagent.safety.path_guard import PathGuard, PathGuardError


_BUILD_DIRS = frozenset({"build", ".tox", ".eggs", "__pycache__", ".git", "node_modules", ".venv", "venv"})


def is_native_artifact(name: str) -> bool:
    """镜像构建产生、导入时需要的被忽略文件：Linux 扩展，以及构建生成的 Python 模块。

    后者如 setuptools_scm 的 _version.py、astropy 的 version.py/cython_version.py；
    构建目录、缓存与其他被忽略内容（如 .env）不复制。
    """
    path = PurePosixPath(name)
    base = path.name
    if base.endswith(".so") or ".so." in base:
        return True
    return base.endswith(".py") and not _BUILD_DIRS & set(path.parts[:-1])


def ignored_native_artifacts(root) -> list[str]:
    """列出Git忽略的本地构建产物，供输入缓存重建与实验目录初始化共用。"""
    listed = subprocess.run(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
        cwd=root, capture_output=True, check=True, timeout=30,
    ).stdout.split(b"\0")
    return [os.fsdecode(raw) for raw in listed if raw and is_native_artifact(os.fsdecode(raw))]


def copy_native_artifacts(source, destination) -> dict:
    """重建隔离缓存时保留镜像导出的扩展；Git clone 不会带上被忽略的 .so。"""
    copied = []
    for name in ignored_native_artifacts(source):
        relative = PurePosixPath(name)
        if relative.is_absolute() or any(part in ("..", ".git") for part in relative.parts):
            raise ValueError("prepared artifact路径不在源码范围")
        src, dst = source / name, destination / name
        if src.is_symlink() or not stat.S_ISREG(src.lstat().st_mode) or os.path.lexists(dst):
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        dst.write_bytes(data)
        dst.chmod(stat.S_IMODE(src.stat().st_mode) & 0o777)
        copied.append({"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)})
    return {"copied": copied, "copied_bytes": sum(item["size_bytes"] for item in copied)}


def seed_prepared_native_artifacts(context: WorkspaceContext, *, source_root: Path | None = None) -> dict:
    """仅在SWE独立实验目录初始化时复制被Git忽略的扩展及构建生成的Python模块。

    整目录bind会遮住image原有扩展，而Git clone不含ignored构建产物。只从同一
    prepared source取缺失的构建依赖，不复制其他ignored内容、不覆盖实验目录，
    不跟随链接。返回内容hash供本地审计；补丁仍只来自Git源码差异。
    """
    source, active = source_root or context.source_root, context.active_root
    if source == active:
        raise ValueError("实验输入缓存与运行目录必须分开")
    source_guard = PathGuard(source, denied_paths=context.denied_paths)
    active_guard = PathGuard(active, denied_paths=context.denied_paths)
    listed = subprocess.run(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
        cwd=source, capture_output=True, check=True, timeout=30,
    ).stdout.split(b"\0")
    copied, skipped = [], []
    for raw in listed:
        if not raw:
            continue
        name = os.fsdecode(raw)
        relative = PurePosixPath(name)
        if not is_native_artifact(name):
            continue
        if relative.is_absolute() or any(part in ("..", ".git") for part in relative.parts):
            raise ValueError("prepared artifact路径不在源码范围")
        for root in (source, active):
            parent = root
            for part in relative.parts[:-1]:
                parent /= part
                if parent.is_symlink():
                    raise ValueError("prepared artifact父路径不能是符号链接")
        src, dst = source / name, active / name
        if not stat.S_ISREG(src.lstat().st_mode):
            skipped.append({"path": name, "reason": "not_regular_file"})
            continue
        try:
            source_guard.resolve(name)
            active_guard.resolve(name)
        except PathGuardError:
            skipped.append({"path": name, "reason": "repository_access"})
            continue
        if os.path.lexists(dst):
            skipped.append({"path": name, "reason": "workspace_already_has_path"})
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        digest, size = hashlib.sha256(), 0
        # xb不覆盖已有路径；输入缓存与运行目录在模型开始前由harness准备。
        with src.open("rb") as incoming, dst.open("xb") as outgoing:
            while chunk := incoming.read(1024 * 1024):
                outgoing.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        dst.chmod(stat.S_IMODE(src.stat().st_mode) & 0o777)
        copied.append({"path": name, "size_bytes": size, "sha256": digest.hexdigest()})
    return {"revision": "prepared-native-artifacts-v2", "source": "same_prepared_image_source",
            "copied": copied, "skipped": skipped, "copied_bytes": sum(x["size_bytes"] for x in copied)}
