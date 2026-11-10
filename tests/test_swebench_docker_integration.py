"""已有官方镜像的免费真实容器验收；不pull、不运行模型、不做官方评分。"""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import threading
import zipfile

import pytest

from codeagent.benchmark.swebench_executor import SWEbenchDockerCommandExecutor, DockerCLIExecutionBackend
from codeagent.runtime.approval import AutoApprovalGate
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.command_service import CommandService
from runtime_fixtures import execution_session
from test_swebench_docker_executor import prepared

pytestmark = pytest.mark.skipif(os.environ.get('CODEAGENT_RUN_SWEBENCH_DOCKER') != '1',
                              reason='显式设置CODEAGENT_RUN_SWEBENCH_DOCKER=1运行已有镜像，绝不下载')


class CountingDockerBackend(DockerCLIExecutionBackend):
    def __init__(self):
        self.created = []
        self.removed = []

    def create(self, argv):
        ident = super().create(argv)
        self.created.append(ident)
        return ident

    def remove(self, ident):
        super().remove(ident)
        self.removed.append(ident)


@pytest.fixture
def task(tmp_path):
    root, store, context = execution_session(tmp_path)
    image = 'swebench/sweb.eval.x86_64.sympy_1776_sympy-20590:latest'
    info = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image], timeout=15))[0]
    digest = info['RepoDigests'][0].rsplit('@', 1)[1]
    source = replace(prepared(context), image_ref=image, image_digest=digest)
    backend = CountingDockerBackend()
    executor = SWEbenchDockerCommandExecutor(source, backend, import_probe=lambda *_: None)
    service = CommandService(context, AutoApprovalGate(False), executor, store, CommandArtifactStore(store))
    try:
        yield root, store, context, executor, backend, service
    finally:
        executor.close()
        for ident in backend.created:
            assert subprocess.run(['docker', 'inspect', ident], capture_output=True, timeout=10).returncode != 0


def run(service, command, **kwargs):
    result = service.run_command({'command': command, **kwargs})
    assert result.ok, result.model_dump()
    return result


def test_one_container_retains_installed_package_tmp_home_and_fresh_shell(task):
    root, _, _, executor, backend, service = task
    wheel = root / 'persistent_fixture-0.0.0-py3-none-any.whl'
    with zipfile.ZipFile(wheel, 'w') as archive:
        archive.writestr('persistent_fixture.py', 'VALUE = 123\n')
        archive.writestr('persistent_fixture-0.0.0.dist-info/METADATA', 'Metadata-Version: 2.1\nName: persistent-fixture\nVersion: 0.0.0\n')
        archive.writestr('persistent_fixture-0.0.0.dist-info/WHEEL', 'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        archive.writestr('persistent_fixture-0.0.0.dist-info/RECORD', '')
    run(service, 'python -m pip install --no-index --no-deps --target "$HOME/site" /testbed/persistent_fixture-0.0.0-py3-none-any.whl && '
        'printf task > /tmp/codeagent-task && printf home > "$HOME/marker" && printf temp > "$TMPDIR/marker" && '
        'export ONLY_FIRST_COMMAND=present; cd /tmp', timeout_seconds=30)
    ident = executor.container_id
    run(service, 'test "$PWD" = /testbed && test -z "$ONLY_FIRST_COMMAND" && '
        'test "$(cat /tmp/codeagent-task)" = task && test "$(cat "$HOME/marker")" = home && '
        'test "$(cat "$TMPDIR/marker")" = temp && PYTHONPATH="$HOME/site" python -c "import persistent_fixture; assert persistent_fixture.VALUE == 123" && '
        'printf write > visible.txt && if printf bad >> .git/config 2>/dev/null; then exit 9; fi')
    assert (root / 'visible.txt').read_text() == 'write'
    assert len(backend.created) == 1 and not backend.removed and executor.container_id == ident
    info = json.loads(subprocess.check_output(['docker', 'inspect', ident], timeout=10))[0]
    assert info['State']['Running'] and info['HostConfig']['NetworkMode'] == 'none'


def test_normal_exit_reaps_detached_background_writes(task):
    root, _, _, _, backend, service = task
    run(service, 'setsid bash -c "sleep 2; echo late > /testbed/late.txt" >/dev/null 2>&1 & printf done')
    run(service, 'sleep 3; test ! -e late.txt')
    assert not (root / 'late.txt').exists() and len(backend.created) == 1


def test_timeout_reaps_tree_and_next_command_uses_same_environment(task):
    root, _, _, executor, backend, service = task
    run(service, 'printf persisted > /tmp/task-before-timeout')
    ident = executor.container_id
    result = service.run_command({'command': 'setsid bash -c "sleep 7; echo late > /testbed/late-timeout.txt" >/dev/null 2>&1 & sleep 30',
                                  'timeout_seconds': 3})
    assert result.error_code == 'execution_timed_out', result
    run(service, 'test "$(cat /tmp/task-before-timeout)" = persisted; sleep 5; test ! -e late-timeout.txt')
    assert not (root / 'late-timeout.txt').exists() and executor.container_id == ident
    assert len(backend.created) == 1


def test_stop_cancels_inflight_exec_and_close_removes_container(task):
    root, _, _, executor, backend, service = task
    run(service, 'printf keep > /tmp/task-before-stop')
    stop = threading.Event()
    executor.cancelled = stop.is_set
    timer = threading.Timer(2, stop.set)
    timer.start()
    try:
        result = service.run_command({'command': 'sleep 5; echo late > stopped-late.txt', 'timeout_seconds': 30})
    finally:
        timer.cancel(); timer.join()
    assert result.error_code == 'execution_cancelled', result
    executor.close()
    executor.close()
    assert len(backend.created) == len(backend.removed) == 1
    assert not (root / 'stopped-late.txt').exists()


def test_denied_file_is_masked_in_persistent_container(task):
    root, store, context, _, backend, _ = task
    (root / 'private.txt').write_text('secret fixture')
    executor = SWEbenchDockerCommandExecutor(prepared(context), backend, import_probe=lambda *_: None)
    # 使用已有镜像身份，不创建或下载另一个镜像。
    image = 'swebench/sweb.eval.x86_64.sympy_1776_sympy-20590:latest'
    details = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image]))[0]
    executor.prepared_source = replace(prepared(context), image_ref=image, image_digest=details['RepoDigests'][0].rsplit('@', 1)[1])
    service = CommandService(replace(context, denied_paths=('private.txt',)), AutoApprovalGate(False), executor, store, CommandArtifactStore(store))
    try:
        run(service, 'test ! -s private.txt')
        run(service, 'test ! -s private.txt')
    finally:
        executor.close()
    assert (root / 'private.txt').read_text() == 'secret fixture'
