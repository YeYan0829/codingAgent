"""正式固定题集在真实生成前验证同版本的完整零模型预检。"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import subprocess


def verify_readiness(path: Path | None, selection, runtime_root: Path) -> dict:
    if path is None:
        raise ValueError('正式题集需要 --preflight-report：所有题的仓库接入及官方 gold 必须先通过')
    value=json.loads(Path(path).read_text())
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=runtime_root,text=True).strip()
    if subprocess.check_output(['git','status','--porcelain'],cwd=runtime_root,text=True).strip():
        raise ValueError('正式测评必须从干净的冻结 checkout 运行')
    if value.get('runtime_commit')!=commit or value.get('selection_sha256')!=hashlib.sha256(selection.path.read_bytes()).hexdigest():
        raise ValueError('预检版本或题集与本次运行不一致')
    if value.get('pinned_sources')!=selection.pinned_sources:
        raise ValueError('预检 task/grader pins 不一致')
    wanted=set(selection.instance_ids)
    grader_command = None
    workspace_backend = None
    image_identities = None
    network_policy = None
    for kind in ('gold','repository'):
        artifact=value[kind]
        target=Path(artifact['path'])
        if not target.is_absolute(): target=Path(path).parent/target
        raw=target.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=artifact['sha256']:
            raise ValueError(f'{kind} 预检文件已改变')
        report=json.loads(raw)
        rows=report['tasks']
        if len(rows)!=len(wanted) or {r['instance_id'] for r in rows}!=wanted:
            raise ValueError(f'{kind} 预检未覆盖完整且唯一的固定题集')
        if kind=='gold':
            grader_command = report.get('grader_command')
            if not isinstance(grader_command,list) or not grader_command or not all(isinstance(x,str) and x for x in grader_command):
                raise ValueError('gold 预检缺少可核对的 grader/environment command')
            passed=all(r.get('resolved') is True and r.get('status')=='gold_passed' for r in rows)
        else:
            from codeagent.benchmark.swebench_compat import valid_compatibility_identity
            workspace_backend = report.get('workspace_backend')
            network_policy = report.get('network_policy')
            if workspace_backend != 'swebench-container-workspace-v1':
                raise ValueError('repository预检必须使用当前原生单题容器；旧源码缓存预检不适用')
            image_identities = {r['instance_id']: r.get('source_identity') for r in rows}
            passed=(report.get('runtime_commit')==commit and report.get('selection_sha256')==value['selection_sha256'] and
                    all(r.get('passed') is True and r.get('source_unchanged') is True
                        and r.get('gold_objects_isolated') is True and r.get('container_removed') is True
                        and r.get('environment_probe_passed') is True
                        and isinstance(r.get('source_identity'), dict)
                        and all(isinstance(r['source_identity'].get(k), str) and r['source_identity'][k]
                                for k in ('image_digest', 'dataset_base_commit', 'prepared_head', 'prepared_tree'))
                        for r in rows))
            if network_policy is not None:
                passed = passed and all(r.get('source_identity', {}).get('network_policy') == network_policy for r in rows)
                passed = passed and all(valid_compatibility_identity(r['instance_id'], network_policy.get('mode'),
                    r.get('source_identity', {}).get('environment_compatibility')) for r in rows)
                if network_policy.get('mode') == 'filtered':
                    passed = passed and all(r.get('network_probe_passed') is True for r in rows)
        if not passed: raise ValueError(f'{kind} 预检仍有未通过项，禁止开始生成')
    return {'runtime_commit':commit,'report_sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            'all_tasks_preflight_passed':True,'grader_command':grader_command,
            'workspace_backend':workspace_backend,'image_identities':image_identities,
            'network_policy':network_policy}
