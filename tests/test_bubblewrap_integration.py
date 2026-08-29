import os
import socket
import sys
import threading
from pathlib import Path

import pytest

from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.bubblewrap import BubblewrapBackend
from codeagent.runtime.command_service import CommandService
from codeagent.runtime.sandbox_executor import SandboxedCommandExecutor
from test_candidate_loop import execution_session


def _backend() -> BubblewrapBackend:
    executable = os.environ.get("CODEAGENT_TEST_BWRAP")
    if not executable:
        pytest.skip("设置 CODEAGENT_TEST_BWRAP 后运行真实 Bubblewrap 集成测试")
    return BubblewrapBackend(Path(executable))


def test_real_bubblewrap_candidate_write_source_readonly_and_network_off(tmp_path):
    repo, store, context = execution_session(tmp_path)
    service = CommandService(
        context, AutoApprovalGate(True), SandboxedCommandExecutor(_backend()),
        store, CommandArtifactStore(store),
    )

    written = service.run_command({
        "command": "printf 'sandboxed\\n' > generated.txt; awk 'NR > 1 { exit 1 }' /proc/net/route",
        "purpose": "validation",
    })
    assert written.ok, written.content
    assert (context.active_root / "generated.txt").read_text() == "sandboxed\n"

    source = repo / "sort_utils.py"
    before = source.read_bytes()
    denied = service.run_command({"command": f"printf hacked > {source}"})
    assert not denied.ok
    assert source.read_bytes() == before

    git_write = service.run_command({"command": "printf hacked > \"$(git rev-parse --absolute-git-dir)/HEAD\""})
    assert not git_write.ok


def test_real_bubblewrap_existing_toolchains_and_approved_host_network(tmp_path):
    _, store, context = execution_session(tmp_path)
    service = CommandService(
        context, AutoApprovalGate(True), SandboxedCommandExecutor(_backend()),
        store, CommandArtifactStore(store),
    )
    pytest_result = service.run_command({
        "command": f"{sys.executable} -m pytest -p no:cacheprovider test_sort_utils.py",
        "purpose": "utility",
    })
    assert not pytest_result.ok and "1 failed" in pytest_result.content

    make_result = service.run_command({
        "command": "printf 'generated-by-make.txt:\\n\\tprintf made > generated-by-make.txt\\n' > Makefile && make generated-by-make.txt",
    })
    assert make_result.ok, make_result.content
    assert (context.active_root / "generated-by-make.txt").read_text() == "made"

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    accepted = threading.Event()

    def serve_once():
        connection, _ = listener.accept()
        connection.close()
        listener.close()
        accepted.set()

    threading.Thread(target=serve_once, daemon=True).start()
    connect = (
        f"{sys.executable} -c \"import socket; "
        f"socket.create_connection(('127.0.0.1',{port}),2).close()\""
    )
    off = service.run_command({"command": connect})
    assert not off.ok and not accepted.is_set()
    host = service.run_command({
        "command": connect,
        "permissions": [{"capability": "network", "reason": "integration localhost", "scope": "once"}],
    })
    assert host.ok, host.content
    assert accepted.wait(2)
