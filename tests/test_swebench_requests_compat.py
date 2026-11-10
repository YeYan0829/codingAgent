"""旧Requests代理适配：连接语义、惰性加载、环境身份与已有镜像免费验收。"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from codeagent.benchmark import swebench_compat as compat
from codeagent.workspace.requests_compat import patch_connection_class, REVISION


def connection_class():
    class Connection:
        def __init__(self, tunnel=True):
            self.host, self.port = ('127.0.0.1', 18080) if tunnel else ('direct.example', 443)
            self._tunnel_host = 'target.example' if tunnel else None
            self._tunnel_port = 443
            self.tcp = []
            self.tls = []
            self.reject = False

        def _tunnel(self):
            if self.reject:
                raise OSError('403 blocked_domain')
            # 当前Python不再把host/port改为目标地址。

        def connect(self):
            self.tcp.append((self.host, self.port))
            if self._tunnel_host:
                self._tunnel()
            self.tls.append((self.host, self.port))
    return Connection


def test_proxy_tcp_endpoint_remains_separate_from_tls_and_host_on_reconnect():
    Connection = connection_class()
    patch_connection_class(Connection)
    patch_connection_class(Connection)
    connection = Connection()
    for _ in range(3):
        connection.connect()
        assert (connection.host, connection.port) == ('target.example', 443)
    assert connection.tcp == [('127.0.0.1', 18080)] * 3
    assert connection.tls == [('target.example', 443)] * 3


def test_direct_connections_and_connect_rejections_keep_original_behavior():
    Connection = connection_class()
    patch_connection_class(Connection)
    direct = Connection(tunnel=False)
    direct.connect()
    assert direct.tcp == direct.tls == [('direct.example', 443)]
    denied = Connection()
    denied.reject = True
    for _ in range(2):
        with pytest.raises(OSError, match='403 blocked_domain'):
            denied.connect()
    assert not denied.tls and denied.tcp == [('127.0.0.1', 18080)] * 2


@pytest.mark.parametrize('version,patched', [('2.0.1', True), ('2.1.0', False)])
def test_import_hook_is_lazy_version_scoped_and_handles_module_reload(tmp_path, version, patched):
    package = tmp_path / 'requests'
    (package / 'packages/urllib3').mkdir(parents=True)
    for path in [package / 'packages/__init__.py', package / 'packages/urllib3/__init__.py']:
        path.write_text('')
    (package / '__init__.py').write_text('from .packages.urllib3 import connectionpool\n__version__ = ' + repr(version))
    (package / 'packages/urllib3/connectionpool.py').write_text(
        'class VerifiedHTTPSConnection:\n def connect(self): pass\n def _tunnel(self): pass\n')
    script = ('import sys,runpy,importlib; runpy.run_path(' + repr(str(compat._BOOTSTRAP)) +
        ',run_name="_codeagent_requests_compat_bootstrap"); assert "requests" not in sys.modules; '
        'sys.path.insert(0,' + repr(str(tmp_path)) + '); import requests; '
        'from requests.packages.urllib3 import connectionpool as pool; '
        'assert hasattr(pool.VerifiedHTTPSConnection,"_codeagent_proxy_compat_revision") is ' + repr(patched) + '; '
        'importlib.reload(pool); assert hasattr(pool.VerifiedHTTPSConnection,"_codeagent_proxy_compat_revision") is ' + repr(patched))
    subprocess.run([sys.executable, '-I', '-S', '-c', script], capture_output=True, text=True, check=True)


def test_profile_only_selects_requests_task_in_filtered_mode():
    selection = json.loads((Path(__file__).parents[1] / 'benchmarks/swebench/selections/hal-verified-mini-50.json').read_text())
    assert [row['instance_id'] for row in selection['tasks'] if compat.compatibility_profile(row['instance_id'], 'filtered')] == [compat.INSTANCE]
    assert compat.compatibility_catalog('off') == {}
    assert compat.compatibility_arguments(compat.INSTANCE, 'off') == []


def test_bootstrap_change_invalidates_manifest_fingerprint(tmp_path, monkeypatch):
    from codeagent.benchmark.manifest import build_run_manifest
    from codeagent.benchmark.swebench_network import SWEbenchNetworkConfig
    from codeagent.config import ModelConfig, RuntimeConfig
    bootstrap = tmp_path / 'bootstrap.py'; bootstrap.write_text('first')
    selection = tmp_path / 'selection.json'; selection.write_text('{}')
    monkeypatch.setattr(compat, '_BOOTSTRAP', bootstrap)
    def fingerprint():
        return build_run_manifest(run_id='fixture', selection_path=selection, selection_id='fixed',
            model=ModelConfig(), runtime=RuntimeConfig(), endpoint_identity='fake', swebench_commit=None,
            runtime_root=tmp_path, network_policy=SWEbenchNetworkConfig('filtered').identity())['model']['config_fingerprint']
    before = fingerprint(); bootstrap.write_text('second')
    assert fingerprint() != before


@pytest.mark.parametrize('failure', [None, 'missing', 'revision', 'bootstrap_sha256', 'ca_sha256', 'ca_bundle'])
def test_formal_preflight_requires_current_requests_compatibility(tmp_path, failure):
    import hashlib
    from test_swebench_readiness import fixture
    from codeagent.benchmark.swebench_readiness import verify_readiness
    from codeagent.benchmark.swebench_network import SWEbenchNetworkConfig
    repo, spec, gate, value = fixture(tmp_path)
    spec.instance_ids = (compat.INSTANCE, 'two')
    for kind in ('gold', 'repository'):
        path = tmp_path / (kind + '.json'); report = json.loads(path.read_text())
        report['tasks'][0]['instance_id'] = compat.INSTANCE
        if kind == 'repository':
            policy = SWEbenchNetworkConfig('filtered').identity(); report['network_policy'] = policy
            for row in report['tasks']:
                row['source_identity']['network_policy'] = policy; row['network_probe_passed'] = True
            identity = {**compat.compatibility_profile(compat.INSTANCE, 'filtered'), 'ca_sha256': 'a' * 64}
            if failure not in (None, 'missing'): identity[failure] = 'stale'
            if failure != 'missing': report['tasks'][0]['source_identity']['environment_compatibility'] = identity
        path.write_text(json.dumps(report)); value[kind]['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    gate.write_text(json.dumps(value))
    if failure:
        with pytest.raises(ValueError): verify_readiness(gate, spec, repo)
    else:
        assert verify_readiness(gate, spec, repo)['all_tasks_preflight_passed']


@pytest.mark.skipif(os.environ.get('CODEAGENT_RUN_SWEBENCH_DOCKER') != '1', reason='显式启用已有Requests镜像；无模型/无pull')
@pytest.mark.parametrize('mode', ['off', 'filtered'])
def test_real_requests_environment_compatibility_and_security(tmp_path, mode):
    from codeagent.benchmark.swebench_source import SWEbenchSourcePreparer, SWEbenchTaskSource
    from codeagent.benchmark.swebench_preflight import SWEbenchTaskRepository
    from codeagent.benchmark.swebench_network import SWEbenchNetworkConfig, authorize_network
    from codeagent.runtime.approval import AutoApprovalGate
    from codeagent.runtime.artifacts import CommandArtifactStore
    from codeagent.runtime.command_service import CommandService
    from codeagent.session.store import SessionStore
    root = Path(__file__).parents[1]
    meta = SWEbenchTaskRepository(root / 'reference/swe-bench-tasks').load_metadata(compat.INSTANCE)
    config = SWEbenchNetworkConfig(mode)
    workspace = SWEbenchSourcePreparer(allow_pull=False, network_config=config).open_workspace(
        SWEbenchTaskSource(compat.INSTANCE, meta['image'], meta['base_commit']), tmp_path / 'workspace')
    try:
        store = SessionStore(workspace.context.active_root, session_root=tmp_path / 'sessions').create()
        authorize_network(store, config)
        command = CommandService(workspace.context, AutoApprovalGate(False), workspace.command_executor, store, CommandArtifactStore(store))
        if mode == 'off':
            code = "import os,requests; from requests.packages.urllib3.connectionpool import VerifiedHTTPSConnection as C; assert not hasattr(C,'_codeagent_proxy_compat_revision'); assert 'REQUESTS_CA_BUNDLE' not in os.environ"
            assert workspace.environment_compatibility is None
        else:
            identity = workspace.environment_compatibility
            assert compat.valid_compatibility_identity(compat.INSTANCE, mode, identity)
            assert workspace.command_executor.environment_snapshot(workspace.context, None, None)['environment_compatibility'] == identity
            code = """import sys,os,json,requests
from requests.packages.urllib3.connectionpool import VerifiedHTTPSConnection as C
assert C._codeagent_proxy_compat_revision == 'requests-2.0.1-proxy-v1'
assert requests.__version__ == '2.0.1'
with requests.Session() as session:
 for index in range(3):
  response=session.get('https://httpbin.org/get',timeout=15,headers={'Connection':'close'} if index==1 else {})
  assert response.status_code==200 and response.json()['headers']['Host']=='httpbin.org'
  response.close()
response=requests.get('https://pypi.org/simple/pip/',timeout=15)
assert response.status_code==200;response.close()
for url,kind,reason in [('https://github.com/',requests.exceptions.ProxyError,'403'),('https://wrong.host.badssl.com/',requests.exceptions.SSLError,'hostname')]:
 try: requests.get(url,timeout=15)
 except kind as error: assert reason in str(error),str(error)
 else: raise AssertionError('expected rejection: '+url)
print('verified-https-reuse-reconnect-and-security')
"""
        result = command.run_command({'command': "python - <<'PY'\n" + code + "\nPY", 'timeout_seconds': 120})
        assert result.ok, result.content
        assert not workspace.prediction_patch()
        if mode == 'filtered':
            # 改PYTHONPATH不丢兼容层，正常启动不提前导入题目源码。
            result = command.run_command({'command': "PYTHONPATH=/testbed python -c \"import sys; assert 'requests' not in sys.modules; import requests; from requests.packages.urllib3.connectionpool import VerifiedHTTPSConnection as C; assert C._codeagent_proxy_compat_revision\""})
            assert result.ok, result.content
    finally:
        workspace.close()
