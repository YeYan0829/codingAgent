"""实验预测补丁：读取独立实验目录的当前差异，不修改index或认证测试版本。"""
import os
import subprocess
from pathlib import Path


def prepare_swebench_workspace(prepared, destination: Path) -> dict:
    """在独立题目目录检出prepared输入，并恢复同镜像依赖；不创建Runtime worktree。"""
    from codeagent.benchmark.source_dependencies import copy_submodules, git
    from codeagent.benchmark.swebench_native_artifacts import seed_prepared_native_artifacts
    from codeagent.workspace.workspace import WorkspaceContext

    source = prepared.source_root.resolve()
    destination = destination.resolve()
    # upload-pack只复制HEAD分支可达对象，避免本地clone带入悬空评分对象或其他refs。
    subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', 'clone', '--quiet', '--no-local',
                    '--single-branch', '--no-tags', '--no-checkout', '--', str(source), str(destination)],
                   capture_output=True, check=True, timeout=180)
    git(destination, ['-c', 'core.hooksPath=/dev/null', 'checkout', '--detach', prepared.prepared_head])
    if git(destination, ['rev-parse', 'HEAD^{tree}']).stdout.decode().strip() != prepared.prepared_tree:
        raise ValueError('实验目录与prepared输入树不一致')
    copy_submodules(source, destination, prepared.prepared_head)
    artifacts = seed_prepared_native_artifacts(WorkspaceContext.source(destination), source_root=source)
    if git(destination, ['status', '--porcelain', '--untracked-files=all']).stdout.strip():
        raise ValueError('实验目录初始化后存在额外源码修改')
    return artifacts


def prediction_patch(root: Path) -> bytes:
    environment = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}
    def git(*args):
        return subprocess.run(['git', '-c', 'core.quotePath=false', *args], cwd=root,
            env=environment, capture_output=True, check=False, timeout=30)
    tracked = git('diff', '--binary', '--no-ext-diff', '--no-textconv', 'HEAD')
    if tracked.returncode:
        raise ValueError('读取实验补丁失败')
    output = tracked.stdout
    untracked = git('ls-files', '--others', '--exclude-standard', '-z')
    if untracked.returncode:
        raise ValueError('读取实验新增文件失败')
    for raw in untracked.stdout.split(b'\0'):
        if not raw: continue
        relative = os.fsdecode(raw)
        patch = git('diff', '--no-index', '--binary', '--no-ext-diff', '--no-textconv', '--', '/dev/null', relative)
        if patch.returncode not in {0, 1}:
            raise ValueError('读取新增文件补丁失败')
        output += patch.stdout
    return output
