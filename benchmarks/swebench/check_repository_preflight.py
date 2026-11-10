"""零模型预检：直接验证正式单题容器的工具、依赖与编辑/undo，不加载gold。"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from codeagent.benchmark.swebench_preflight import SWEbenchTaskRepository
from codeagent.benchmark.swebench_source import SWEbenchSourcePreparer, SWEbenchTaskSource
from codeagent.benchmark.swebench_executor import repository_package
from codeagent.benchmark.swebench_network import SWEbenchNetworkConfig, authorize_network
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.command_service import CommandService
from codeagent.session.store import SessionStore


def environment_probe(instance):
    package = repository_package(instance)
    if instance.startswith('pylint-dev__pylint-'):
        return 'python -c "from importlib.metadata import distribution; assert distribution(\'pylint\').version" && pyreverse --help >/tmp/codeagent-pyreverse-help'
    return f'python -c "import {package}"' if package else 'python --version'


def check_task(instance, tasks, output, network_config=None):
    # 这里只加载公开metadata；gold与隐藏测试由另一评分入口读取。
    meta = tasks.load_metadata(instance)
    output.mkdir(parents=True, exist_ok=True)
    row = {'instance_id': instance, 'passed': False, 'workspace_backend': 'swebench-container-workspace-v1'}
    workspace = None
    try:
        network_config = network_config or SWEbenchNetworkConfig()
        workspace = SWEbenchSourcePreparer(output / 'unused-cache', allow_pull=False, network_config=network_config).open_workspace(
            SWEbenchTaskSource(instance, str(meta['image']), str(meta['base_commit'])), output / 'container')
        row['source_identity'] = dict(workspace.identity)
        store = SessionStore(workspace.context.active_root, session_root=output / 'sessions').create()
        authorize_network(store, network_config)
        tools = {t.name: t for t in workspace.build_read_tools(workspace.context)}
        command = CommandService(workspace.context, AutoApprovalGate(False), workspace.command_executor,
                                 store, CommandArtifactStore(store))
        found = tools['find_files'].handler({'include': ['*.py'], 'max_results': 1})
        if not found.ok or not found.content:
            raise RuntimeError('镜像中未找到可读取的项目Python文件')
        first = found.content.splitlines()[0]
        read = tools['read_file'].handler({'path': first, 'start_line': 1, 'end_line': 2})
        if not read.ok:
            raise RuntimeError('实际容器文件读取失败：' + str(read.error))
        editor = workspace.edit_service(workspace.context, store)
        # 使用新文件验证跨工具可见及undo，完成后保持原始项目干净。
        filename = '__codeagent_environment_probe__.txt'
        edited = editor.apply_workspace_edit({'operations': [
            {'op': 'create_file', 'path': filename, 'content': 'container-preflight\n'}]})
        if not edited.ok:
            raise RuntimeError('实际容器编辑失败：' + str(edited.error))
        probe = command.run_command({'command': environment_probe(instance) + ' && grep -q container-preflight ' + filename})
        if not probe.ok:
            raise RuntimeError('实际项目环境探测失败：' + probe.content)
        if network_config.mode == 'filtered':
            network_probe = command.run_command({'command': "python - <<'PY'\nimport socket, urllib.request, urllib.error\ntry:\n urllib.request.urlopen('https://github.com/',timeout=5)\nexcept urllib.error.URLError as e:\n assert '403' in str(e), str(e)\nelse:\n raise AssertionError('answer domain not blocked')\ntry:\n socket.create_connection(('1.1.1.1',443),timeout=1)\nexcept OSError:\n pass\nelse:\n raise AssertionError('direct route exists')\nPY", 'timeout_seconds': 15})
            if not network_probe.ok:
                raise RuntimeError('实际网络隔离探测失败：' + network_probe.content)
            row['network_probe_passed'] = True
        undone = editor.undo()
        if not undone.ok or workspace.prediction_patch():
            raise RuntimeError('预检编辑没有完整撤销，原始项目发生变化')
        row.update(passed=True, source_unchanged=True, gold_objects_isolated=True,
                   environment_probe_passed=True, probe_command=environment_probe(instance),
                   file_read={'path': first, 'sha256': read.metadata.get('sha256')})
    except Exception as exc:
        row.update(passed=False, error_type=type(exc).__name__, error=str(exc))
    finally:
        if workspace is not None:
            try:
                workspace.close()
                row['container_removed'] = workspace.container_id is None
            except Exception as exc:
                row.update(passed=False, container_removed=False, cleanup_error=str(exc))
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selection', type=Path, default=ROOT/'benchmarks/swebench/selections/hal-verified-mini-50.json')
    parser.add_argument('--task-repo', type=Path, required=True)
    parser.add_argument('--source-cache', type=Path, help='旧参数，仅保留命令兼容，不读取或创建源码缓存')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=1, choices=range(1,5))
    parser.add_argument('--swe-network', choices=('off', 'filtered'), default='filtered')
    args = parser.parse_args()
    selection = json.loads(args.selection.read_text())
    def git(root, *argv):
        return subprocess.check_output(['git', *argv], cwd=root, text=True).strip()
    runtime_commit = git(ROOT, 'rev-parse', 'HEAD')
    if git(ROOT, 'status', '--porcelain'):
        raise ValueError('正式题集预检必须从干净冻结checkout运行')
    if git(args.task_repo, 'rev-parse', 'HEAD') != selection['pinned_sources']['task_repository_commit']:
        raise ValueError('Task repository pin mismatch')
    if git(args.task_repo, 'status', '--porcelain'):
        raise ValueError('Task repository dirty')
    args.output.mkdir(parents=True, exist_ok=False)
    rows = []
    order = {task: i for i, task in enumerate(selection['official_instance_ids'])}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = {pool.submit(check_task, instance, SWEbenchTaskRepository(args.task_repo), args.output/instance,
                               SWEbenchNetworkConfig(args.swe_network)): instance for instance in order}
        for future in as_completed(pending):
            instance = pending[future]
            try:
                row = future.result()
            except Exception as exc:
                row = {'instance_id': instance, 'passed': False, 'error': str(exc)}
            rows.append(row)
            rows.sort(key=lambda r: order[r['instance_id']])
            (args.output/'repository-results.json').write_text(json.dumps({
                'workspace_backend': 'swebench-container-workspace-v1', 'runtime_commit': runtime_commit,
                'network_policy': SWEbenchNetworkConfig(args.swe_network).identity(),
                'workers': args.workers, 'selection_sha256': hashlib.sha256(args.selection.read_bytes()).hexdigest(),
                'tasks': rows}, ensure_ascii=False, indent=2)+'\n')
            print(instance, 'PASS' if row['passed'] else row.get('error', row), flush=True)
    return int(not all(r['passed'] for r in rows))


if __name__ == '__main__':
    sys.exit(main())
