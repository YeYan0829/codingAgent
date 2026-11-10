"""特定旧题目的显式环境兼容配置；不修改/testbed源码或普通Runtime。"""
import base64
import hashlib
from pathlib import Path
import re
import shutil


INSTANCE = "psf__requests-1766"
REVISION = "requests-2.0.1-proxy-v1"
CONTAINER_ROOT = "/opt/codeagent-tools/requests-compat"
CA_SOURCE = "/etc/ssl/certs/ca-certificates.crt"
_BOOTSTRAP = Path(__file__).resolve().parents[1] / "workspace/requests_compat.py"
_PTH = 'import runpy; runpy.run_path("' + CONTAINER_ROOT + '/bootstrap.py", run_name="_codeagent_requests_compat_bootstrap")\n'


def compatibility_profile(instance_id, network_mode):
    if instance_id != INSTANCE or network_mode != "filtered":
        return None
    return {"revision": REVISION, "requests_version": "2.0.1", "activation": "testbed-site-pth-lazy-import",
            "bootstrap_sha256": hashlib.sha256(_BOOTSTRAP.read_bytes()).hexdigest(),
            "pth_sha256": hashlib.sha256(_PTH.encode()).hexdigest(), "ca_source": CA_SOURCE,
            "ca_bundle": CONTAINER_ROOT + "/ca.pem", "tls_verification": "unchanged"}


def compatibility_catalog(network_mode):
    profile = compatibility_profile(INSTANCE, network_mode)
    return {INSTANCE: profile} if profile else {}


def compatibility_arguments(instance_id, network_mode):
    profile = compatibility_profile(instance_id, network_mode)
    return ["--env", "REQUESTS_CA_BUNDLE=" + profile["ca_bundle"]] if profile else []


def activate_compatibility(backend, container_id, tools_dir, instance_id, network_mode):
    profile = compatibility_profile(instance_id, network_mode)
    if profile is None:
        return None
    destination = tools_dir / "requests-compat"
    destination.mkdir()
    shutil.copyfile(_BOOTSTRAP, destination / "bootstrap.py")
    raw = backend._call(["exec", container_id, "/opt/miniconda3/bin/python", "-I", "-S", "-c",
                         "import base64; print(base64.b64encode(open(" + repr(CA_SOURCE) + ", 'rb').read()).decode())"], 15)
    ca = base64.b64decode(raw, validate=True)
    if b"-----BEGIN CERTIFICATE-----" not in ca:
        raise ValueError("Requests兼容环境缺少系统CA，未启动模型")
    (destination / "ca.pem").write_bytes(ca)
    # 只在新题目容器的testbed环境增加启动钩子；工具worker使用-S，不受影响。
    script = ("import pathlib,site; p=pathlib.Path(site.getsitepackages()[0])/'codeagent_requests_proxy_compat.pth'; "
              "assert not p.exists(), 'unexpected compatibility hook'; p.write_text(" + repr(_PTH) + ")")
    backend._call(["exec", container_id, "/opt/miniconda3/envs/testbed/bin/python", "-I", "-S", "-c", script], 15)
    probe = ("import sys; assert 'requests' not in sys.modules, 'compatibility must not preload target'; "
             "import requests; from requests.packages.urllib3.connectionpool import VerifiedHTTPSConnection as C; "
             "assert requests.__version__ == '2.0.1'; assert C._codeagent_proxy_compat_revision == " + repr(REVISION))
    backend._call(["exec", "--workdir", "/testbed", container_id,
                   "/opt/miniconda3/envs/testbed/bin/python", "-c", probe], 15)
    return {**profile, "ca_sha256": hashlib.sha256(ca).hexdigest()}


def valid_compatibility_identity(instance_id, network_mode, value):
    expected = compatibility_profile(instance_id, network_mode)
    if expected is None:
        return value is None
    return (isinstance(value, dict) and all(value.get(key) == item for key, item in expected.items())
            and isinstance(value.get("ca_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", value["ca_sha256"]) is not None)
