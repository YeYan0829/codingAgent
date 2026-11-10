"""联网评测的规则、协议转发和恢复身份；无模型调用。"""
import json
import os
import socket
import threading

import pytest

from codeagent.benchmark.swebench_network import SWEbenchNetworkConfig, SWEbenchEgress, EVALUATION_RULES
from codeagent.workspace import egress_proxy as proxy


@pytest.mark.parametrize('host', [
    'github.com', 'API.GITHUB.COM.', 'raw.githubusercontent.com', 'patch-diff.githubusercontent.com',
    'codeload.github.com', 'gist.github.com', 'project.github.io', 'ghproxy.net', 'gitclone.com',
])
def test_blocked_domains_never_resolve(monkeypatch, host):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: pytest.fail('拦截应发生在DNS之前'))
    with pytest.raises(ValueError, match='blocked_domain'):
        proxy.checked_addresses(host, 443)


@pytest.mark.parametrize('host', ['127.0.0.1', '2130706433', '0x7f000001', 'localhost', 'allowed.example'])
def test_private_ip_and_alternative_ip_spellings_cannot_reach_host(monkeypatch, host):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))])
    with pytest.raises(ValueError):
        proxy.checked_addresses(host, 443)


@pytest.mark.parametrize('host', ['134744072', '0x08080808', '010.010.010.010', '8.8.2056'])
def test_encoded_public_ip_is_rejected_before_dns(monkeypatch, host):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: pytest.fail('不能把数值IP别名当域名解析'))
    with pytest.raises(ValueError, match='ip_literal_denied'):
        proxy.checked_addresses(host, 443)


def test_allowed_domain_uses_checked_public_address_and_no_second_lookup(monkeypatch):
    calls = []
    address = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('151.101.0.223', 443))
    def resolve(host, port, **kwargs):
        calls.append((host, port))
        return [address]
    class Connection:
        def settimeout(self, timeout): pass
        def connect(self, target): self.target = target
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    monkeypatch.setattr(socket, 'socket', lambda *args: Connection())
    connection = proxy.connect_checked('PyPI.org.', 443)
    assert calls == [('pypi.org', 443)] and connection.target == address[4]


@pytest.mark.parametrize('address', ['10.0.0.1', '169.254.169.254', '::1', 'fc00::1', '192.0.2.1'])
def test_mixed_public_private_dns_is_rejected(monkeypatch, address):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('151.101.0.223', 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))])
    with pytest.raises(ValueError, match='non_public_address'):
        proxy.checked_addresses('pypi.org', 443)


@pytest.fixture
def server(tmp_path):
    audit = tmp_path / 'audit.jsonl'
    sock = str(tmp_path / 'proxy.sock')
    with proxy.ProxyServer(sock, str(audit)) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield sock, audit
        finally:
            server.shutdown()
            thread.join(3)


def exchange(sock, payload):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(sock)
        client.sendall(payload)
        result = bytearray()
        while data := client.recv(65536):
            result.extend(data)
        return bytes(result)


def test_http_rewrites_authority_and_audit_omits_secrets(server, monkeypatch):
    captured = []
    def connect(host, port):
        assert (host, port) == ('pypi.org', 80)
        local, upstream = socket.socketpair()
        def respond():
            with upstream:
                data = b''
                while b'\r\n\r\n' not in data:
                    data += upstream.recv(4096)
                captured.append(data)
                upstream.sendall(b'HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nOK')
        threading.Thread(target=respond, daemon=True).start()
        return local
    monkeypatch.setattr(proxy, 'connect_checked', connect)
    sock, audit = server
    response = exchange(sock, b'GET http://pypi.org/simple/pip/?token=secret HTTP/1.1\r\nHost: github.com\r\nProxy-Authorization: secret\r\n\r\n')
    assert response.endswith(b'OK') and b'200 OK' in response
    assert b'Host: pypi.org\r\n' in captured[0] and b'github.com' not in captured[0]
    assert b'Proxy-Authorization' not in captured[0]
    assert 'secret' not in audit.read_text() and json.loads(audit.read_text())['verdict'] == 'allowed'


def test_connect_tunnel_forwards_bytes(server, monkeypatch):
    def connect(host, port):
        assert (host, port) == ('pypi.org', 443)
        local, upstream = socket.socketpair()
        def echo():
            with upstream:
                upstream.sendall(upstream.recv(4096))
        threading.Thread(target=echo, daemon=True).start()
        return local
    monkeypatch.setattr(proxy, 'connect_checked', connect)
    sock, _ = server
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(3)
        client.connect(sock)
        # 提前发送payload也不得被HTTP缓冲丢弃。
        client.sendall(b'CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org\r\n\r\nTLS-PROBE')
        result = b''
        while chunk := client.recv(4096): result += chunk
    assert b'200 Connection established' in result and result.endswith(b'TLS-PROBE')


@pytest.mark.parametrize('target', [b'github.com:443', b'raw.githubusercontent.com:443', b'127.0.0.1:443', b'pypi.org:22', b'user:password@pypi.org:443'])
def test_protocol_denials_are_logged(server, target):
    sock, audit = server
    response = exchange(sock, b'CONNECT ' + target + b' HTTP/1.1\r\n\r\n')
    assert b'403' in response and json.loads(audit.read_text())['verdict'] == 'denied'


def test_network_and_prompt_change_batch_fingerprint(tmp_path):
    from codeagent.benchmark.manifest import build_run_manifest
    from codeagent.config import ModelConfig, RuntimeConfig
    selection = tmp_path / 'selection.json'; selection.write_text('{}')
    def manifest(mode):
        return build_run_manifest(run_id='fixture', selection_path=selection, selection_id='fixed', model=ModelConfig(),
            runtime=RuntimeConfig(), endpoint_identity='fake', swebench_commit=None, runtime_root=tmp_path,
            network_policy=SWEbenchNetworkConfig(mode).identity())
    assert manifest('off')['model']['config_fingerprint'] != manifest('filtered')['model']['config_fingerprint']
    assert SWEbenchNetworkConfig('filtered').identity()['prompt_sha256']
    assert 'wheel/sdist' in EVALUATION_RULES


@pytest.mark.parametrize('options, mode', [([], 'filtered'), (['--swe-network', 'off'], 'off')])
def test_cli_passes_selected_network_to_actual_harness(tmp_path, monkeypatch, options, mode):
    import codeagent.benchmark as benchmark
    from codeagent.cli import app
    from typer.testing import CliRunner
    selection = tmp_path / 'selection.json'
    selection.write_text(json.dumps({'selection_id': 'network-fixture', 'tasks': ['owner__repo-1']}))
    class Repository:
        def __init__(self, root): pass
        def export_dataset(self, ids, path): pass
    captured = []
    class Runner:
        def __init__(self, **kwargs): captured.append(kwargs['harness'].source_preparer.network_config)
        def run(self, run_id): return {'offline_fixture': True}
    monkeypatch.setattr(benchmark, 'SWEbenchTaskRepository', Repository)
    monkeypatch.setattr(benchmark, 'SWEbenchBatchRunner', Runner)
    result = CliRunner().invoke(app, ['swebench-batch', str(selection), '--task-repo', str(tmp_path),
        '--output-root', str(tmp_path / 'output'), '--grader-command', 'unused', '--report-template', 'unused', *options])
    assert result.exit_code == 0, result.exception
    assert captured == [SWEbenchNetworkConfig(mode)]


def test_sidecar_failure_removes_container_and_named_volume(tmp_path):
    class Backend:
        def __init__(self): self.calls = []
        def _call(self, argv, timeout):
            self.calls.append(argv)
            if argv[0] == 'exec': raise RuntimeError('proxy unavailable')
        def create(self, argv): self.argv = argv; return 'proxy-fixture'
        def start(self, ident): pass
    backend = Backend()
    with pytest.raises(RuntimeError, match='proxy unavailable'):
        SWEbenchEgress(backend, 'existing@sha256:fixture', tmp_path, tmp_path / 'run')
    assert ['rm', '--force', '--volumes', 'proxy-fixture'] in backend.calls
    assert any(call[:2] == ['volume', 'rm'] for call in backend.calls)
    assert '--read-only' in backend.argv and 'bridge' in backend.argv


@pytest.mark.skipif(os.environ.get('CODEAGENT_RUN_SWEBENCH_DOCKER') != '1', reason='显式启用已有镜像联网验收，无模型/无pull')
def test_real_filtered_container_allows_dependencies_denies_answers_and_direct_connections(tmp_path):
    from codeagent.benchmark.swebench_source import SWEbenchSourcePreparer, SWEbenchTaskSource
    from codeagent.benchmark.swebench_network import authorize_network
    from codeagent.runtime.approval import AutoApprovalGate
    from codeagent.runtime.artifacts import CommandArtifactStore
    from codeagent.runtime.command_service import CommandService
    from codeagent.session.store import SessionStore
    from test_swebench_container_workspace import IMAGE, BASE
    import subprocess
    config = SWEbenchNetworkConfig('filtered')
    workspace = SWEbenchSourcePreparer(allow_pull=False, network_config=config).open_workspace(
        SWEbenchTaskSource('pylint-dev__pylint-4551', IMAGE, BASE), tmp_path / 'workspace')
    ident, sidecar, volume = workspace.container_id, workspace.egress.container_id, workspace.egress.volume
    try:
        info = json.loads(workspace.backend._call(['inspect', ident], 15))[0]
        assert info['HostConfig']['NetworkMode'] == 'none'
        assert 'ALL' in info['HostConfig']['CapDrop']
        assert next(m for m in info['Mounts'] if m['Destination'] == '/codeagent-egress')['RW'] is False
        store = SessionStore(workspace.context.active_root, session_root=tmp_path / 'sessions').create()
        command = CommandService(workspace.context, AutoApprovalGate(False), workspace.command_executor, store, CommandArtifactStore(store))
        assert not command.run_command({'command': 'echo must-not-start'}).ok
        authorize_network(store, config)
        positive = command.run_command({'command': "python - <<'PY'\nimport urllib.request\nwith urllib.request.urlopen('https://pypi.org/simple/pip/',timeout=15) as r:\n assert r.status==200 and r.read(64)\nprint('dependency-site-available')\nPY", 'timeout_seconds': 25})
        if not positive.ok:
            def logs(ident):
                result = subprocess.run(['docker', 'logs', ident], capture_output=True, text=True, timeout=10)
                return result.stdout + result.stderr
            pytest.fail(positive.content + '\nproject: ' + logs(ident) + '\nproxy: ' + logs(sidecar))
        assert 'dependency-site-available' in positive.content
        denied = command.run_command({'command': "python - <<'PY'\nimport urllib.request,urllib.error,socket\nfor host in ['github.com','api.github.com','raw.githubusercontent.com','ghproxy.net','1.1.1.1']:\n try: urllib.request.urlopen('https://'+host+'/',timeout=5)\n except urllib.error.HTTPError as e: assert e.code==403,(host,e)\n except urllib.error.URLError as e: assert '403' in str(e),(host,e)\n else: raise AssertionError('not blocked: '+host)\ntry: socket.create_connection(('1.1.1.1',443),timeout=1)\nexcept OSError: pass\nelse: raise AssertionError('direct route exists')\nopener=urllib.request.build_opener(urllib.request.ProxyHandler({}))\ntry: opener.open('http://1.1.1.1/',timeout=1)\nexcept OSError: pass\nelse: raise AssertionError('unset proxy bypass')\nprint('answers-and-direct-denied')\nPY", 'timeout_seconds': 35})
        assert denied.ok and 'answers-and-direct-denied' in denied.content, denied.model_dump()
        records = [json.loads(line) for line in (workspace.egress.audit_dir / 'access.jsonl').read_text().splitlines()]
        assert any(r['host'] == 'pypi.org' and r['verdict'] == 'allowed' for r in records)
        assert sum(r['verdict'] == 'denied' for r in records) == 5
        workspace.backend._call(['stop', '--time', '1', sidecar], 15)
        failed = command.run_command({'command': "python - <<'PY'\nimport urllib.request\ntry: urllib.request.urlopen('https://pypi.org/',timeout=2)\nexcept OSError: print('failed-closed')\nelse: raise AssertionError('proxy failure bypassed')\nPY", 'timeout_seconds': 10})
        assert failed.ok and 'failed-closed' in failed.content, failed.model_dump()
    finally:
        workspace.close()
    for kind, name in [('container', ident), ('container', sidecar), ('volume', volume)]:
        assert subprocess.run(['docker', kind, 'inspect', name], capture_output=True, timeout=10).returncode != 0


@pytest.mark.skipif(os.environ.get('CODEAGENT_RUN_SWEBENCH_DOCKER') != '1', reason='显式启用已有镜像预检，无模型/无pull')
def test_real_repository_preflight_checks_selected_filtered_network(tmp_path):
    import importlib.util
    from pathlib import Path
    from test_swebench_container_workspace import IMAGE, BASE
    path = Path(__file__).parents[1] / 'benchmarks/swebench/check_repository_preflight.py'
    spec = importlib.util.spec_from_file_location('filtered_preflight_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    class Tasks:
        def load_metadata(self, instance): return {'image': IMAGE, 'base_commit': BASE}
    policy = SWEbenchNetworkConfig('filtered')
    row = module.check_task('pylint-dev__pylint-4551', Tasks(), tmp_path / 'preflight', policy)
    assert row['passed'] and row['container_removed'] and row['network_probe_passed'], row
    assert row['source_identity']['network_policy'] == policy.identity()
