"""执行固定官方 grader，仅给旧 Requests 镜像提供冻结的 CA 信任库。

不改变标准答案、test patch、评分逻辑或 TLS verify。原始镜像/失败另行保留。
"""
import argparse
import hashlib
from pathlib import Path
import runpy
import sys


def main():
    parser=argparse.ArgumentParser(add_help=False)
    parser.add_argument('--ca-bundle',type=Path,required=True)
    parser.add_argument('--ca-sha256',required=True)
    args,remaining=parser.parse_known_args()
    bundle=args.ca_bundle.resolve(strict=True)
    if hashlib.sha256(bundle.read_bytes()).hexdigest()!=args.ca_sha256:
        raise ValueError('Frozen CA bundle identity mismatch')
    from docker.models.containers import ContainerCollection
    original=ContainerCollection.create
    def create(self,image,*positional,**kwargs):
        if 'psf_1776_requests-' in str(image):
            volumes=dict(kwargs.get('volumes') or {})
            volumes[str(bundle)]={'bind':'/testbed/requests/cacert.pem','mode':'ro'}
            kwargs['volumes']=volumes
        return original(self,image,*positional,**kwargs)
    ContainerCollection.create=create
    sys.argv=['swebench.harness.run_evaluation',*remaining]
    try: runpy.run_module('swebench.harness.run_evaluation',run_name='__main__')
    finally: ContainerCollection.create=original

if __name__=='__main__': main()
