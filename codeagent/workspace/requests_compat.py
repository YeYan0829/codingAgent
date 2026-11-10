"""Requests 2.0.1的评测环境适配；由单题Python的.pth按需安装，不预加载题目包。

旧urllib3依赖HTTPConnection._tunnel改变host/port的旧语义。仅修正代理
TCP端点与TLS/Host目标的区分，不替换认证、请求或证书验证逻辑。
本文件单独部署，必须可在题目Python 3.9中运行，不依赖CodeAgent。
"""
import importlib.abc
import importlib.machinery
import sys


REVISION = "requests-2.0.1-proxy-v1"
_MARKER = "_codeagent_proxy_compat_revision"
_POOL = "requests.packages.urllib3.connectionpool"


def patch_connection_class(connection_class):
    """保持TLS/Host使用目标地址，重新连接时仍从原代理建立TCP连接。"""
    if getattr(connection_class, _MARKER, None) == REVISION:
        return
    original_tunnel = connection_class._tunnel
    original_connect = connection_class.connect

    def tunnel(self):
        self._codeagent_proxy_endpoint = (self.host, self.port)
        original_tunnel(self)  # CONNECT拒绝或失败原样传播，不发送TLS。
        self.host, self.port = self._tunnel_host, self._tunnel_port

    def connect(self):
        if self._tunnel_host and hasattr(self, "_codeagent_proxy_endpoint"):
            self.host, self.port = self._codeagent_proxy_endpoint
        return original_connect(self)

    connection_class._tunnel = tunnel
    connection_class.connect = connect
    setattr(connection_class, _MARKER, REVISION)


def _after_import():
    requests = sys.modules.get("requests")
    if getattr(requests, "__version__", None) != "2.0.1":
        return
    pool = sys.modules.get(_POOL)
    connection = getattr(pool, "VerifiedHTTPSConnection", None)
    if connection is not None:
        patch_connection_class(connection)


class _Loader:
    def __init__(self, original):
        self.original = original

    def create_module(self, spec):
        create = getattr(self.original, "create_module", None)
        return create(spec) if create else None

    def exec_module(self, module):
        self.original.exec_module(module)
        _after_import()

    def __getattr__(self, name):
        return getattr(self.original, name)


class _Finder(importlib.abc.MetaPathFinder):
    _codeagent_requests_compat = REVISION

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in ("requests", _POOL):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is not None and spec.loader is not None and hasattr(spec.loader, "exec_module"):
            spec.loader = _Loader(spec.loader)
        return spec


def install():
    if any(getattr(finder, "_codeagent_requests_compat", None) == REVISION for finder in sys.meta_path):
        return
    sys.meta_path.insert(0, _Finder())
    _after_import()


if __name__ == "_codeagent_requests_compat_bootstrap":
    install()
