import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from codeagent.benchmark.swebench_readiness import verify_readiness
from runtime_fixtures import git, make_bug_repo


def fixture(tmp_path):
    repo=make_bug_repo(tmp_path)
    selection=tmp_path/'selection.json';selection.write_text('{}')
    spec=SimpleNamespace(path=selection,instance_ids=('one','two'),pinned_sources={'swebench_commit':'grader','task_repository_commit':'tasks'})
    value={'runtime_commit':git(repo,'rev-parse','HEAD'),'selection_sha256':hashlib.sha256(selection.read_bytes()).hexdigest(),'pinned_sources':spec.pinned_sources}
    for kind in ('gold','repository'):
        report=tmp_path/(kind+'.json')
        rows=[{'instance_id':i,'resolved':True,'status':'gold_passed'} if kind=='gold' else {
            'instance_id':i,'passed':True,'source_unchanged':True,'gold_objects_isolated':True,
            'container_removed':True,'environment_probe_passed':True,
            'source_identity':{'image_digest':'sha256:image','dataset_base_commit':'base','prepared_head':'head','prepared_tree':'tree'}} for i in spec.instance_ids]
        report.write_text(json.dumps({'workspace_backend':'swebench-container-workspace-v1','grader_command':['official-grader','--frozen-env'],'runtime_commit':value['runtime_commit'],'tasks':rows,'selection_sha256':value['selection_sha256']}))
        value[kind]={'path':report.name,'sha256':hashlib.sha256(report.read_bytes()).hexdigest()}
    gate=tmp_path/'ready.json';gate.write_text(json.dumps(value))
    return repo,spec,gate,value


def test_full_preflight_accepted(tmp_path):
    repo,spec,gate,_=fixture(tmp_path)
    assert verify_readiness(gate,spec,repo)['all_tasks_preflight_passed']


@pytest.mark.parametrize('failure', [None, 'missing_probe', 'policy_mismatch'])
def test_filtered_preflight_requires_matching_per_task_policy_and_probe(tmp_path, failure):
    from codeagent.benchmark.swebench_network import SWEbenchNetworkConfig
    repo, spec, gate, value = fixture(tmp_path)
    path = tmp_path / 'repository.json'
    report = json.loads(path.read_text())
    policy = SWEbenchNetworkConfig('filtered').identity()
    report['network_policy'] = policy
    for row in report['tasks']:
        row['source_identity']['network_policy'] = policy
        row['network_probe_passed'] = True
    if failure == 'missing_probe': report['tasks'][0].pop('network_probe_passed')
    if failure == 'policy_mismatch': report['tasks'][0]['source_identity']['network_policy'] = SWEbenchNetworkConfig().identity()
    path.write_text(json.dumps(report))
    value['repository']['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    gate.write_text(json.dumps(value))
    if failure:
        with pytest.raises(ValueError): verify_readiness(gate, spec, repo)
    else:
        assert verify_readiness(gate, spec, repo)['network_policy'] == policy


@pytest.mark.parametrize('failure',['missing','runtime','selection','pins','changed_artifact','gold_failure','missing_task','duplicate_task','repository_failure','source_changed','gold_not_isolated','dirty_runtime','legacy_cache','container_not_removed','probe_failed','image_missing'])
def test_incomplete_or_stale_preflight_refuses_before_generation(tmp_path,failure):
    repo,spec,gate,v=fixture(tmp_path)
    if failure=='missing': gate=None
    elif failure in ('runtime','selection','pins'):
        v[{'runtime':'runtime_commit','selection':'selection_sha256','pins':'pinned_sources'}[failure]]='different'
    elif failure=='dirty_runtime': (repo/'extra').write_text('dirty')
    else:
        kind='repository' if failure in ('repository_failure','source_changed','gold_not_isolated','legacy_cache','container_not_removed','probe_failed','image_missing') else 'gold'
        p=tmp_path/(kind+'.json'); r=json.loads(p.read_text())
        if failure=='missing_task':r['tasks'].pop()
        elif failure=='duplicate_task':r['tasks'][1]=r['tasks'][0]
        elif failure=='repository_failure':r['tasks'][0]['passed']=False
        elif failure=='gold_not_isolated':r['tasks'][0].pop('gold_objects_isolated')
        elif failure=='source_changed':r['tasks'][0]['source_unchanged']=False
        elif failure=='legacy_cache':r.pop('workspace_backend')
        elif failure=='container_not_removed':r['tasks'][0]['container_removed']=False
        elif failure=='probe_failed':r['tasks'][0]['environment_probe_passed']=False
        elif failure=='image_missing':r['tasks'][0]['source_identity'].pop('image_digest')
        else:r['tasks'][0]['resolved']=False
        p.write_text(json.dumps(r))
        if failure!='changed_artifact':v[kind]['sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
    if gate:gate.write_text(json.dumps(v))
    with pytest.raises(ValueError):verify_readiness(gate,spec,repo)
