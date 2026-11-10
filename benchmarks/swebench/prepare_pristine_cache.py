"""从已有 prepared HEAD 的可达历史重建独立模型缓存；不复制其他refs或悬空gold对象。"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor,as_completed
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from codeagent.benchmark.source_dependencies import copy_submodules, git as _git
from codeagent.benchmark.swebench_native_artifacts import copy_native_artifacts


def prepare(instance,original,destination):
    manifest,= (original/'sources'/instance).glob('*/manifest.json')
    data=json.loads(manifest.read_text()); source=manifest.parent/'source'
    target=destination/'sources'/instance/manifest.parent.name
    target.mkdir(parents=True,exist_ok=False)
    clone=target/'source'
    subprocess.run(['git','clone','--no-local','--single-branch','--no-tags','--no-checkout',str(source),str(clone)],check=True,capture_output=True,timeout=180)
    subprocess.run(['git','checkout','--detach',data['prepared_head']],cwd=clone,check=True,capture_output=True,timeout=180)
    copy_submodules(source,clone,data['prepared_head'])
    assert _git(clone,['rev-parse','HEAD^{tree}']).stdout.decode().strip()==data['prepared_tree']
    assert not _git(clone,['status','--porcelain','--untracked-files=all']).stdout.strip()
    # Git clone不含被忽略的构建产物；同镜像扩展另行复制，供独立实验目录导入。
    native=copy_native_artifacts(source,clone)
    assert not _git(clone,['status','--porcelain','--untracked-files=all']).stdout.strip()
    (target/'manifest.json').write_text(json.dumps(data,indent=2)+'\n')
    return {'instance_id':instance,'prepared_head':data['prepared_head'],'prepared_tree':data['prepared_tree'],'image_digest':data['image_digest'],
            'native_artifacts':len(native['copied']),'native_bytes':native['copied_bytes'],
            'method':'Git upload-pack of single HEAD branch plus same-image ignored native extensions; no tags, other refs, alternates, hardlinks or unreachable objects'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-cache',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--selection',type=Path,default=ROOT/'benchmarks/swebench/selections/hal-verified-mini-50.json')
    args=p.parse_args();args.source_cache=args.source_cache.resolve();args.output=args.output.resolve()
    args.output.mkdir(exist_ok=False,parents=True)
    ids=json.loads(args.selection.read_text())['official_instance_ids'];rows=[]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures={pool.submit(prepare,i,args.source_cache,args.output):i for i in ids}
        for future in as_completed(futures):
            row=future.result();rows.append(row)
            (args.output/'pristine-sources.json').write_text(json.dumps({'tasks':rows},indent=2)+'\n')
            print(row['instance_id'],'prepared',flush=True)
if __name__=='__main__':main()
