"""把完整零模型预检报告绑定到干净冻结版本；任一检查失败不输出准入证明。"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from codeagent.benchmark.batch import SWEbenchSelection
from codeagent.benchmark.swebench_readiness import verify_readiness


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selection',type=Path,required=True)
    parser.add_argument('--gold-report',type=Path,required=True)
    parser.add_argument('--repository-report',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists(): raise ValueError('Use a new output path; do not overwrite prior evidence')
    selection=SWEbenchSelection(args.selection)
    value={'runtime_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
           'selection_sha256':hashlib.sha256(selection.path.read_bytes()).hexdigest(),
           'pinned_sources':selection.pinned_sources}
    for key,path in [('gold',args.gold_report),('repository',args.repository_report)]:
        value[key]={'path':str(path.resolve()),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    temporary=args.output.with_suffix('.pending.json')
    temporary.write_text(json.dumps(value,indent=2)+'\n')
    try:
        verify_readiness(temporary,selection,ROOT)
        temporary.rename(args.output)
    finally:
        temporary.unlink(missing_ok=True)
    print('All tasks passed preflight:',args.output)

if __name__=='__main__':main()
