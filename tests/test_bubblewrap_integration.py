import os
import shlex
import socket
import sys
import threading
import time
import uuid
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
    assert pytest_result.metadata["environment_contract_revision"] == "command-environment-v1"
    assert pytest_result.metadata["backend"] == "bubblewrap"
    assert pytest_result.metadata["launch_cwd"] == str(context.active_root)

    exported = service.run_command({"command": "export CODEAGENT_EPHEMERAL=1; cd /; ephemeral() { :; }"})
    fresh = service.run_command({
        "command": "test -z \"${CODEAGENT_EPHEMERAL-}\" && test \"$PWD\" = \"$(git rev-parse --show-toplevel)\" && ! type ephemeral",
    })
    assert exported.ok and fresh.ok, fresh.content

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
    assert host.metadata["effective_network_policy"] == "host"
    assert service.environment_snapshot()["network_mode"] == "off"
    assert accepted.wait(2)


def test_real_bubblewrap_host_root_is_readonly(tmp_path):
    _, store, context = execution_session(tmp_path)
    service = CommandService(
        context, AutoApprovalGate(True), SandboxedCommandExecutor(_backend()),
        store, CommandArtifactStore(store),
    )
    host_dir = Path("/var/tmp") / f"codeagent-bwrap-{uuid.uuid4().hex}"
    host_dir.mkdir()
    sentinel = host_dir / "sentinel.txt"
    sentinel.write_text("original\n")
    try:
        result = service.run_command({
            "command": f"printf hacked > {shlex.quote(str(sentinel))}",
        })
        assert not result.ok
        assert sentinel.read_text() == "original\n"
    finally:
        sentinel.unlink(missing_ok=True)
        host_dir.rmdir()


def test_real_bubblewrap_timeout_stops_children_and_cleans_runtime_home(tmp_path):
    _, store, context = execution_session(tmp_path)
    artifacts = CommandArtifactStore(store)
    service = CommandService(
        context, AutoApprovalGate(True), SandboxedCommandExecutor(_backend()),
        store, artifacts,
    )

    result = service.run_command({
        "command": "(sleep 2; printf leaked > timeout-leak.txt) & wait",
        "timeout_seconds": 1,
    })
    assert not result.ok and result.error_code == "execution_timed_out"
    assert result.metadata["workspace_state"] == "changes_active"
    time.sleep(2)
    assert not (context.active_root / "timeout-leak.txt").exists()
    assert not artifacts.runtime_root.exists() or not any(artifacts.runtime_root.iterdir())


def test_missing_bubblewrap_fails_closed_without_running_payload(tmp_path):
    _, store, context = execution_session(tmp_path)
    missing = tmp_path / "missing-bwrap"
    service = CommandService(
        context, AutoApprovalGate(True),
        SandboxedCommandExecutor(BubblewrapBackend(missing)),
        store, CommandArtifactStore(store),
    )

    result = service.run_command({"command": "printf launched > should-not-exist.txt"})

    assert not result.ok and result.error_code == "sandbox_unavailable"
    assert not (context.active_root / "should-not-exist.txt").exists()
