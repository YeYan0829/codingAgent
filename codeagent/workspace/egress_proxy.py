"""测评出站代理：只用标准库，在独立容器中校验域名与公网地址。"""
from __future__ import annotations

from datetime import datetime, timezone
import http.server
import ipaddress
import json
import re
import select
import socket
import socketserver
import threading
from urllib.parse import urlsplit


BLOCKED_DOMAINS = (
    "github.com", "githubusercontent.com", "github.io", "githubassets.com",
    "ghproxy.com", "ghproxy.net", "ghfast.top", "gitclone.com", "gitmirror.com",
    "github.moeyy.xyz", "gh-proxy.com", "ghproxy.cc", "gh.api.99988866.xyz",
)
REVISION = "swe-filtered-egress-v1"
SOCKET_PATH = "/codeagent-egress/proxy.sock"
PROXY_PORT = 18080


def normalize_host(value):
    host = value.rstrip(".").encode("idna").decode("ascii").lower()
    if len(host) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
        raise ValueError("invalid_hostname")
    if any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-') for label in host.split('.')):
        raise ValueError("invalid_hostname")
    # getaddrinfo还接受整数、十六进制、八进制及缩写IPv4，不能仅检查标准点分形式。
    if all(re.fullmatch(r'(?:0x[0-9a-f]+|[0-9]+)', label) for label in host.split('.')):
        raise ValueError("ip_literal_denied")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ValueError("ip_literal_denied")


def checked_addresses(host, port):
    host = normalize_host(host)
    if any(host == domain or host.endswith('.' + domain) for domain in BLOCKED_DOMAINS):
        raise ValueError("blocked_domain")
    if port not in (80, 443):
        raise ValueError("port_denied")
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    # 任一非公网解析结果都拒绝；连接使用已检查的数值地址，避免二次DNS解析。
    ips = [ipaddress.ip_address(item[4][0]) for item in addresses]
    if not ips or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in ips):
        raise ValueError("non_public_address")
    return addresses


def connect_checked(host, port):
    addresses = checked_addresses(host, port)
    last = None
    for family, kind, proto, _, address in addresses:
        connection = socket.socket(family, kind, proto)
        connection.settimeout(30)
        try:
            connection.connect(address)
            return connection
        except OSError as exc:
            last = exc
            connection.close()
    raise OSError("upstream_connect_failed") from last


def copy_streams(left, right):
    """无活动90秒关闭；不解密TLS、不保存正文/凭据。"""
    peers = {left: right, right: left}
    while True:
        ready, _, _ = select.select(list(peers), [], [], 90)
        if not ready:
            return
        for source in ready:
            data = source.recv(65536)
            if not data:
                return
            peers[source].sendall(data)


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    # 不预读CONNECT后的TLS字节，转发统一从socket读取。
    rbufsize = 0

    def setup(self):
        self.request.settimeout(30)
        super().setup()

    def log_message(self, *args):
        pass

    def do_CONNECT(self):
        self.forward(tunnel=True)

    def do_GET(self):
        self.forward(tunnel=False)

    do_HEAD = do_GET

    def forward(self, *, tunnel):
        host, port = "", None
        upstream = None
        try:
            parts = urlsplit(('https://' if tunnel else '') + self.path)
            if parts.username is not None or parts.password is not None or parts.fragment:
                raise ValueError("invalid_target")
            if tunnel and (parts.path or parts.query) or not tunnel and parts.scheme != 'http':
                raise ValueError("invalid_target")
            host = normalize_host(parts.hostname or '')
            port = parts.port or (443 if tunnel else 80)
            if not tunnel and (self.headers.get('Transfer-Encoding') or self.headers.get('Content-Length', '0') != '0'):
                raise ValueError("http_body_denied")
            upstream = connect_checked(host, port)
        except (ValueError, OSError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "upstream_unavailable"
            self.server.audit(self.command, host, port, "denied", reason)
            self.send_error(403 if isinstance(exc, ValueError) else 502, reason)
            return
        self.server.audit(self.command, host, port, "allowed", "connected")
        try:
            if tunnel:
                self.send_response(200, "Connection established")
                self.end_headers()
            else:
                path = parts.path or '/'
                if parts.query:
                    path += '?' + parts.query
                excluded = {'host', 'connection', 'proxy-connection', 'proxy-authorization', 'keep-alive',
                            'transfer-encoding', 'upgrade', 'te', 'trailer'}
                excluded.update(item.strip().lower() for item in self.headers.get('Connection', '').split(','))
                headers = ''.join(f'{key}: {value}\r\n' for key, value in self.headers.items() if key.lower() not in excluded)
                authority = host + (f':{port}' if port != 80 else '')
                upstream.sendall((f'{self.command} {path} HTTP/1.0\r\nHost: {authority}\r\n' + headers + 'Connection: close\r\n\r\n').encode('latin-1'))
            copy_streams(self.connection, upstream)
        except OSError:
            pass
        finally:
            upstream.close()
            self.close_connection = True


class ProxyServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, address, audit_path):
        self.audit_lock = threading.Lock()
        self.audit_path = audit_path
        super().__init__(address, ProxyHandler)

    def audit(self, method, host, port, verdict, reason):
        with self.audit_lock:
            with open(self.audit_path, 'a', encoding='utf-8') as stream:
                stream.write(json.dumps(dict(time=datetime.now(timezone.utc).isoformat(), method=method,
                    host=host, port=port, verdict=verdict, reason=reason), ensure_ascii=False) + '\n')
                stream.flush()


def main():
    import os
    with ProxyServer(SOCKET_PATH, '/audit/access.jsonl') as server:
        os.chmod(SOCKET_PATH, 0o666)
        server.serve_forever()


if __name__ == '__main__':
    main()
