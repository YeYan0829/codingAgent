"""容器内的无依赖文件工具入口；不加载模型、凭据、评分数据或宿主Session。

使用镜像基础Python（>=3.11），项目命令继续使用镜像testbed环境。读取、搜索、
Git沿用本地实现，文件事务只提供受保护的I/O原语，计划/undo日志仍由Runtime维护。
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import uuid

from codeagent.safety.path_guard import PathGuardError
from codeagent.workspace.workspace import WorkspaceContext


ROOT = Path("/testbed")


def checked(context, relative, *, write=False):
    raw = Path(relative)
    if raw.is_absolute() or ".." in raw.parts or "\0" in relative:
        raise PathGuardError("path必须是项目内相对路径")
    cursor = context.active_root
    for part in raw.parts:
        cursor /= part
        if cursor.is_symlink():
            raise PathGuardError("文件操作路径不能经过symlink")
    if write and any(part in {".git", ".codeagent"} for part in raw.parts):
        raise PathGuardError("不能编辑Git或Runtime元数据")
    return context.path_guard().resolve(relative)


def git(root, *args, allowed=(0,)):
    done = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=root,
                          stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
    if done.returncode not in allowed:
        raise RuntimeError(done.stderr.decode("utf-8", "replace")[-4000:])
    return done.stdout


def prediction(root):
    # 不修改index；不经过模型预览/命令输出的截断路径。
    patch = git(root, "diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD")
    for raw in git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0"):
        if raw:
            patch += git(root, "diff", "--no-index", "--binary", "--no-ext-diff", "--no-textconv",
                         "--", "/dev/null", os.fsdecode(raw), allowed=(0, 1))
    return base64.b64encode(patch).decode("ascii")


def dispatch(request, root=ROOT):
    context = WorkspaceContext(root, root, tuple(request.get("denied_paths", ())))
    operation = request["operation"]
    args = request.get("arguments", {})
    if operation == "tool":
        from codeagent.tools.fs_read import build_fs_handlers
        from codeagent.tools.git_read import GitReadService
        from codeagent.runtime.repository_search import RepositorySearchService
        search = RepositorySearchService(context, executable=request.get("rg"))
        handlers = build_fs_handlers(context, search)
        reader = GitReadService(context)
        handlers.update(git_status=lambda _: reader.status(), git_diff=reader.diff,
                        git_diff_stat=lambda a: reader.diff(a, stat=True))
        return handlers[request["name"]](args).model_dump()
    if operation == "identity":
        base = args["base_commit"]
        if git(root, "rev-parse", "--show-toplevel").decode().strip() != str(root):
            raise ValueError("镜像/testbed不是Git根目录")
        git(root, "merge-base", "--is-ancestor", base, "HEAD")
        status = git(root, "status", "--porcelain=v1", "--untracked-files=all").decode()
        if status:
            raise ValueError("官方镜像初始源码不是干净工作区：" + status[:1000])
        return {"prepared_head": git(root, "rev-parse", "HEAD").decode().strip(),
                "prepared_tree": git(root, "rev-parse", "HEAD^{tree}").decode().strip()}
    if operation == "prediction":
        return prediction(root)
    write = operation in {"write", "mkdir", "unlink", "rmdir", "chmod"}
    target = checked(context, args.get("path", "."), write=write)
    if operation in {"check", "stat"}:
        if not target.exists():
            return None
        info = target.stat()
        return {"st_mode": info.st_mode, "st_size": info.st_size,
                "st_dev": info.st_dev, "st_ino": info.st_ino}
    if operation == "read":
        if not target.is_file() or target.stat().st_size > 512_000:
            raise ValueError("编辑目标不是普通文件或超过512000字节")
        return base64.b64encode(target.read_bytes()).decode("ascii")
    if operation == "write":
        content = base64.b64decode(args["content"], validate=True)
        if len(content) > 512_000:
            raise ValueError("编辑文件超过512000字节")
        temporary = target.parent / ("." + target.name + ".codeagent-" + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
            if target.exists():
                temporary.chmod(stat.S_IMODE(target.stat().st_mode))
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    elif operation == "mkdir":
        target.mkdir(parents=args.get("parents", False), exist_ok=args.get("exist_ok", False))
    elif operation == "unlink":
        target.unlink()
    elif operation == "rmdir":
        target.rmdir()
    elif operation == "chmod":
        target.chmod(args["mode"])
    elif operation == "list":
        return [p.name for p in target.iterdir()]
    else:
        raise ValueError("不支持的容器文件操作：" + operation)
    return None


def main():
    try:
        result = {"ok": True, "value": dispatch(json.load(sys.stdin))}
    except Exception as exc:
        result = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
