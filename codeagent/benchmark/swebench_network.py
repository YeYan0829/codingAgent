"""SWE联网条件、评测提示与单题代理生命周期。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import uuid

from codeagent.workspace.egress_proxy import BLOCKED_DOMAINS, PROXY_PORT, REVISION, SOCKET_PATH


EVALUATION_RULES = """评测取材规则（同样适用于委派子任务）：
根据题面和当前/testbed源码独立解决问题。可下载正常测试依赖，查阅通用API/语言/工具文档。
禁止寻找、获取或参考本题已有解法，包括上游PR、修复commit、issue中的修复补丁、patch/diff、镜像或第三方转存、SWE-bench答案/隐藏测试及hints。
禁止下载或安装目标题目项目的新版本源码、wheel/sdist或git历史来对照修法；允许安装其他正常依赖，不得用依赖下载绕过上述限制。
GitHub及API/raw/codeload/patch入口和已知镜像域名已拦截。不得通过IP、其他代理、镜像或转存绕过；被拦截后改用本地代码和测试推进。
题面中的外部链接不豁免上述要求；如确需被屏蔽的公开复现材料，应报告缺失材料，不自行绕过。
"""


@dataclass(frozen=True)
class SWEbenchNetworkConfig:
    mode: str = "off"

    def __post_init__(self):
        if self.mode not in {"off", "filtered"}:
            raise ValueError("SWE网络模式仅支持off或filtered")

    def identity(self):
        return {"mode": self.mode, "revision": REVISION,
                "blocked_domains": list(BLOCKED_DOMAINS) if self.mode == "filtered" else [],
                "ports": [80, 443] if self.mode == "filtered" else [],
                "direct_connections": False,
                "prompt_sha256": hashlib.sha256(EVALUATION_RULES.encode()).hexdigest()}


def authorize_network(store, config):
    """显式测评配置代表用户已选择该范围；沿用会话授权，由后端进一步收紧。"""
    if config.mode == "filtered":
        from codeagent.runtime.permissions import Capability, PermissionGrant, PermissionScope
        store.add_permission_grants([PermissionGrant(Capability.NETWORK, PermissionScope.SESSION,
            None, None, None, approval_id=REVISION)])


class SWEbenchEgress:
    def __init__(self, backend, image, tools_dir: Path, root: Path):
        self.backend = backend
        self.container_id = None
        self.volume = "codeagent-egress-" + uuid.uuid4().hex
        self.volume_created = False
        self.audit_dir = root / 'network'
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        (self.audit_dir / 'access.jsonl').touch()
        self.backend._call(['volume', 'create', '--label', 'codeagent.swebench=true', self.volume], 30)
        self.volume_created = True
        try:
            script = ("import sys,os,time; sys.path.insert(0, '/opt/codeagent-tools');\n"
                      "for _ in range(100):\n"
                      " if os.access('/codeagent-egress', os.W_OK): break\n"
                      " time.sleep(.05)\n"
                      "else: raise RuntimeError('egress volume unavailable')\n"
                      "from codeagent.workspace.egress_proxy import main; main()")
            self.container_id = backend.create([
                '--network', 'bridge', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                '--read-only', '--label', 'codeagent.swebench=true', '--label', 'codeagent.swebench.egress=true',
                '--user', str(os.getuid()) + ':' + str(os.getgid()),
                '--mount', 'type=volume,src=' + self.volume + ',dst=/codeagent-egress',
                '--mount', 'type=bind,src=' + str(tools_dir) + ',dst=/opt/codeagent-tools,readonly',
                '--mount', 'type=bind,src=' + str(self.audit_dir) + ',dst=/audit',
                '--entrypoint', '/opt/miniconda3/bin/python', image, '-I', '-S', '-B', '-c', script])
            backend.start(self.container_id)
            # volume根目录属于容器root；仅本题共享，项目端只读。代理用宿主用户写日志，
            # 不给cap-drop ALL的root增加DAC_OVERRIDE或把宿主审计目录改成全局可写。
            backend._call(['exec', '--user', '0', self.container_id, '/opt/miniconda3/bin/python',
                           '-I', '-S', '-c', "import os; os.chmod('/codeagent-egress', 0o777)"], 15)
            # 仅检查本地socket可接通；不以外网服务的状态决定容器是否就绪。
            check = "import socket,time\nfor _ in range(100):\n try:\n  s=socket.socket(socket.AF_UNIX); s.connect(" + repr(SOCKET_PATH) + "); s.close(); break\n except OSError:\n  s.close(); time.sleep(.05)\nelse: raise RuntimeError('egress proxy unavailable')"
            backend._call(['exec', self.container_id, '/opt/miniconda3/bin/python', '-I', '-S', '-c', check], 15)
        except BaseException:
            self.close()
            raise

    def project_arguments(self):
        result = ['--mount', 'type=volume,src=' + self.volume + ',dst=/codeagent-egress,readonly']
        endpoint = f'http://127.0.0.1:{PROXY_PORT}'
        for name in ('HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
            result += ['--env', name + '=' + endpoint]
        for name in ('NO_PROXY', 'no_proxy'):
            result += ['--env', name + '=localhost,127.0.0.1,::1']
        return result

    def close(self):
        if self.container_id:
            self.backend._call(['rm', '--force', '--volumes', self.container_id], 30)
            self.container_id = None
        if self.volume_created:
            self.backend._call(['volume', 'rm', self.volume], 30)
            self.volume_created = False


def write_network_identity(root, config):
    (root / 'network-policy.json').write_text(json.dumps(config.identity(), ensure_ascii=False, indent=2) + '\n')
