"""断网项目容器的回环转发；线程运行于PID1，不被每条命令回收。"""
import socket
import socketserver
import threading

from codeagent.workspace.egress_proxy import PROXY_PORT, SOCKET_PATH, copy_streams


class RelayHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
                upstream.settimeout(30)
                upstream.connect(SOCKET_PATH)
                self.request.settimeout(30)
                copy_streams(self.request, upstream)
        except OSError as exc:
            # 运维诊断留在容器日志，不将上游内容或凭据写入日志。
            import sys
            print('egress relay unavailable: ' + type(exc).__name__ + ':' + str(exc.errno), file=sys.stderr, flush=True)


class RelayServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_relay():
    server = RelayServer(('127.0.0.1', PROXY_PORT), RelayHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
