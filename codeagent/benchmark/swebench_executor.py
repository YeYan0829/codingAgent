from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol, Sequence

from codeagent.benchmark.swebench_source import PreparedSWEbenchSource, _replace_tag_with_digest
from codeagent.runtime.artifacts import output_preview
from codeagent.runtime.command import (
    CommandArtifactPaths,
    CommandExecutionStatus,
    CommandResult,
    SandboxExecutionRequest,
    command_cancellation,
)
from codeagent.runtime.permissions import NetworkMode
from codeagent.workspace.workspace import WorkspaceContext


@dataclass(frozen=True)
class DockerExecutionOutcome:
    exit_code: int | None
    timed_out: bool
    process_tree_stopped: bool
    stdout_truncated: bool
    stderr_truncated: bool
    cancelled: bool = False


# PID1只负责等待及回收孤儿，不持有shell状态；命令在各自的docker exec中运行。
_CONTAINER_INIT = """import os, signal
def reap(*args):
    try:
        while os.waitpid(-1, os.WNOHANG)[0]: pass
    except ChildProcessError: pass
signal.signal(signal.SIGCHLD, reap)
signal.signal(signal.SIGTERM, lambda *args: exit(0))
while True: signal.pause()
"""

# 容器使用私有PID命名空间，所有payload与清理进程同UID且禁用提权。
# kill(-1)不影响PID1及调用者；两阶段回收也覆盖自行setsid的后台后代。
_REAP_COMMANDS = """import os, signal, time
for sig in (signal.SIGTERM, signal.SIGKILL):
    try: os.kill(-1, sig)
    except ProcessLookupError: pass
    time.sleep(0.05)
for pid in os.listdir('/proc'):
    if not pid.isdigit() or int(pid) in (1, os.getpid()): continue
    try:
        with open('/proc/' + pid + '/stat') as stream: state = stream.read().rsplit(') ', 1)[1][0]
    except FileNotFoundError: continue
    if state != 'Z': raise SystemExit('command processes remain: ' + pid)
"""


class DockerExecutionBackend(Protocol):
    def create(self, argv: Sequence[str]) -> str: ...
    def start(self, container_id: str) -> None: ...
    def execute(self, container_id: str, argv: Sequence[str], stdout_path: Path, stderr_path: Path,
                timeout_seconds: int, output_limit_bytes: int, cancelled: Callable[[], bool]) -> DockerExecutionOutcome: ...
    def remove(self, container_id: str) -> None: ...
    def probe_import(self, container_id: str, package: str) -> str | None: ...


class DockerCLIExecutionBackend:
    """单题容器的create/start/exec/remove；每条命令独立回收，文件系统在题内保留。"""

    @staticmethod
    def _call(arguments, timeout):
        try:
            result = subprocess.run(['docker', *arguments], capture_output=True, text=True,
                                    shell=False, check=False, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f'Docker {arguments[0]} 调用失败: {exc}') from exc
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout or 'Docker command failed').strip()[-4000:])
        return result.stdout.strip()

    def create(self, argv):
        ident = self._call(['create', *argv], 60)
        if not ident:
            raise RuntimeError('docker create 未返回container id')
        return ident

    def start(self, container_id):
        self._call(['start', container_id], 30)

    def execute(self, container_id, argv, stdout_path, stderr_path, timeout_seconds, output_limit_bytes, cancelled):
        if cancelled():
            return DockerExecutionOutcome(None, False, True, False, False, True)
        process = subprocess.Popen(['docker', 'exec', *argv], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False, start_new_session=True, close_fds=True)
        stdout = _CappedOutput(process.stdout, stdout_path, output_limit_bytes)
        stderr = _CappedOutput(process.stderr, stderr_path, output_limit_bytes)
        stdout.start(); stderr.start()
        timed_out = was_cancelled = False
        stopped = False
        deadline = time.monotonic() + timeout_seconds
        try:
            while process.poll() is None:
                if cancelled():
                    was_cancelled = True
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                try:
                    process.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            try:
                # 即便shell正常退出，也不让后台进程跨命令产生晚到写入。
                self._call(['exec', container_id, 'python', '-I', '-S', '-c', _REAP_COMMANDS], 10)
                stopped = True
            except RuntimeError:
                stopped = False
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
            stdout.join(timeout=3); stderr.join(timeout=3)
            stopped = stopped and not stdout.is_alive() and not stderr.is_alive()
        return DockerExecutionOutcome(process.returncode, timed_out, stopped,
                                      stdout.truncated, stderr.truncated, was_cancelled)

    def remove(self, container_id):
        self._call(['rm', '--force', container_id], 30)

    def probe_import(self, container_id, package):
        # 在本题已有容器中探测，不额外创建短命容器。
        value = self._call(['exec', container_id, 'bash', '-c', _import_probe_script(package)], 15)
        line = value.splitlines()[-1] if value else ''
        return line if line.startswith('/') else None


class SWEbenchDockerCommandExecutor:
    """历史宿主挂载适配器，仅用于兼容回归；正式Harness使用原生容器工作区。"""

    OUTPUT_LIMIT_BYTES = 2 * 1024 * 1024
    CONTAINER_WORKSPACE = Path("/testbed")
    RUNTIME_ROOT = Path("/codeagent-runtime")
    SCRATCH_DIR = Path("/scratch")
    ENVIRONMENT_CONTRACT_REVISION = "swebench-prepared-environment-v5"
    supports_denied_paths = True

    def __init__(
        self,
        prepared_source: PreparedSWEbenchSource,
        backend: DockerExecutionBackend | None = None,
        import_probe: "Callable[[str, str], str | None] | None" = None,
    ) -> None:
        self.prepared_source = prepared_source
        self.backend = backend or DockerCLIExecutionBackend()
        # 默认在首次命令后用本题容器探测；显式回调是离线测试接缝。
        self.import_probe = import_probe
        self._package_import: dict | None = None
        self.cancelled = lambda: False
        self.container_id: str | None = None
        self._binding = None
        self._task_root: Path | None = None
        self._closed = False
        self._lock = threading.Lock()

    def close(self):
        """题目结束/异常时回收唯一容器；失败保留id，调用者可重试清理。"""
        with self._lock:
            self._closed = True
            if self.container_id is not None:
                self.backend.remove(self.container_id)
                self.container_id = None
            if self._task_root is not None:
                # 只有已确认容器移除后才清理运行期HOME/TMP；命令日志在独立目录。
                shutil.rmtree(self._task_root, ignore_errors=True)

    @contextmanager
    def _command_lock(self, cancelled):
        # 共享单题容器串行执行命令；排队取消不必等待另一条长命令结束。
        while not self._lock.acquire(timeout=.05):
            if cancelled():
                yield False
                return
        try:
            yield True
        finally:
            self._lock.release()


    def execute(
        self,
        request: SandboxExecutionRequest,
        context: WorkspaceContext,
        artifacts: CommandArtifactPaths | None,
    ) -> CommandResult:
        started_at = datetime.now(timezone.utc).isoformat()
        started = time.monotonic()
        cancelled = command_cancellation(self.cancelled)
        if artifacts is None:
            return self._failure("Docker executor需要runtime目录", started_at, started, request)
        if request.policy.network_mode != NetworkMode.OFF:
            return self._failure("SWE-bench Docker executor尚不支持获批的host network语义", started_at, started, request)

        if request.policy.read_grants or request.policy.write_grants:
            return self._failure('SWE单题容器不支持额外宿主资源挂载；不能把once权限保留给后续命令', started_at, started, request)
        cleanup_error = None
        try:
            with self._command_lock(cancelled) as acquired:
                if not acquired or cancelled():
                    return replace(self._failure('命令启动前已取消', started_at, started, request),
                                   status=CommandExecutionStatus.CANCELLED)
                if self._closed:
                    raise RuntimeError('本题容器已关闭，不能隐式重建运行环境')
                info = context.active_root.stat()
                binding = (context.active_root.resolve(), info.st_dev, info.st_ino, artifacts.runtime_home.parent.parent.resolve(),
                           tuple(request.policy.hard_deny_paths), tuple(request.policy.protected_readonly_paths))
                if self._binding is not None and self._binding != binding:
                    raise RuntimeError('本题容器的挂载/访问权限发生变化；不能沿用旧容器权限')
                if self.container_id is None:
                    self._task_root = artifacts.runtime_home.parent.parent / 'docker-task'
                    for name in ('home', 'tmp', 'markers', 'masks'):
                        (self._task_root / name).mkdir(parents=True, exist_ok=True)
                    self.container_id = self.backend.create(self._create_argv(request, context, artifacts))
                    self._binding = binding
                    self.backend.start(self.container_id)
                marker = self._task_root / 'markers' / request.command.execution_id
                argv = self._exec_argv(request, marker)
                outcome = self.backend.execute(self.container_id, argv, artifacts.stdout_path, artifacts.stderr_path,
                    request.command.timeout_seconds, self.OUTPUT_LIMIT_BYTES, cancelled)
        except (OSError, RuntimeError, ValueError) as exc:
            try:
                self.close()
            except RuntimeError as cleanup:
                return self._failure(str(exc) + '; 容器清理失败: ' + str(cleanup), started_at, started, request, stopped=False,
                                     payload_started=bool(self._task_root and (self._task_root / 'markers' / request.command.execution_id).exists()))
            return self._failure(str(exc), started_at, started, request)
        except BaseException:
            self.close()
            raise

        payload_started = marker.exists()
        status = (CommandExecutionStatus.CANCELLED if outcome.cancelled else
                  CommandExecutionStatus.TIMED_OUT if outcome.timed_out else CommandExecutionStatus.COMPLETED)
        if not payload_started and not outcome.timed_out and not outcome.cancelled:
            status = CommandExecutionStatus.SANDBOX_SETUP_FAILED
        if not outcome.process_tree_stopped:
            cleanup_error = '命令后代回收失败，本题容器已关闭'
            try:
                self.close()
            except RuntimeError as exc:
                cleanup_error = str(exc)
            status = CommandExecutionStatus.SANDBOX_SETUP_FAILED
        stdout_text, stdout_preview_truncated = output_preview(artifacts.stdout_path)
        stderr_text, stderr_preview_truncated = output_preview(artifacts.stderr_path)
        return CommandResult(
            exit_code=outcome.exit_code,
            stdout=stdout_text, stderr=stderr_text,
            stdout_preview_truncated=stdout_preview_truncated, stderr_preview_truncated=stderr_preview_truncated,
            status=status,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=max(0, round((time.monotonic() - started) * 1000)),
            timed_out=outcome.timed_out,
            spawn_error=cleanup_error or (None if payload_started else "container在payload启动前失败"),
            stdout_truncated=outcome.stdout_truncated,
            stderr_truncated=outcome.stderr_truncated,
            stdout_artifact=str(artifacts.stdout_path),
            stderr_artifact=str(artifacts.stderr_path),
            environment_names=("HOME", "TMPDIR", "XDG_CACHE_HOME", "PYTHONIOENCODING", "PYTHONDONTWRITEBYTECODE"),
            payload_started=payload_started,
            process_tree_stopped=outcome.process_tree_stopped or (self._closed and self.container_id is None),
            effective_policy=request.policy.summary(),
            environment_contract_revision=self.ENVIRONMENT_CONTRACT_REVISION,
            backend="swebench_docker", backend_availability="available",
            launch_cwd=str(self.CONTAINER_WORKSPACE / request.command.cwd),
            effective_network_policy="off",
            effective_filesystem_policy=_filesystem_policy(request.policy.summary()),
        )

    def environment_snapshot(self, context, artifacts, policy) -> dict:
        snapshot = {
            "contract_revision": self.ENVIRONMENT_CONTRACT_REVISION,
            "backend": "swebench_docker", "backend_availability": "closed" if self._closed else "prepared",
            "shell": "/bin/bash --noprofile --norc", "default_cwd": ".",
            "default_cwd_resolves_to": str(self.CONTAINER_WORKSPACE),
            "path_source": "prepared_image_default",
            "environment_names": [
                "GIT_OPTIONAL_LOCKS", "HOME", "PYTHONDONTWRITEBYTECODE", "PYTHONIOENCODING",
                "TMPDIR", "XDG_CACHE_HOME",
            ],
            "home_kind": "private_task_home", "tilde_is_host_home": False,
            "tmp_kind": "private_task_tmp", "network_mode": "off",
            "commands_are_fresh_processes": True, "shell_state_persists": False,
            "container_state_persists": True,
            "container_lifetime": "one_task",
            "extra_host_mounts_supported": False,
            "workspace_files_persist": True,
            "file_lifetime_guidance": (
                "本题复用一个容器；/tmp、HOME、TMPDIR和安装的依赖在题内保留。每条命令使用新shell，cd/export不跨调用保留，后台进程在命令结束时回收。"
                f"跨命令使用的复现脚本放在 {self.SCRATCH_DIR}：它在本任务内保留、不进入补丁，可用 heredoc 写入后执行。"
                "/testbed 中新建的文件会进入补丁，交付前删除调试文件。"
            ),
            "scratch_dir": str(self.SCRATCH_DIR),
            "source_workspace_access": "prepared_backend_policy",
            "active_workspace_access": "read_write",
        }
        entrypoint = repository_test_entrypoint(self.prepared_source.instance_id)
        if entrypoint:
            snapshot["test_entrypoint"] = entrypoint
        package_import = self._package_import_fact()
        if package_import:
            snapshot["package_import"] = package_import
        return snapshot

    def _package_import_fact(self) -> dict | None:
        """镜像里 import 仓库包是否解析到 /testbed：旧镜像以 egg 副本安装时，测试脚本看不到工作区修改。"""
        if self._package_import is not None:
            return self._package_import or None
        package = repository_package(self.prepared_source.instance_id)
        if self.import_probe is None and self.container_id is None:
            return None
        resolved = None
        if package:
            try:
                resolved = (self.import_probe(_replace_tag_with_digest(self.prepared_source.image_ref, self.prepared_source.image_digest), package)
                            if self.import_probe is not None else self.backend.probe_import(self.container_id, package))
            except Exception:
                resolved = None
        if not package or not resolved:
            self._package_import = {}
            return None
        live = resolved.startswith(str(self.CONTAINER_WORKSPACE) + "/")
        self._package_import = {
            "package": package, "resolves_to": resolved, "workspace_edits_visible_to_tests": live,
            "note": ("从测试脚本导入该包解析到当前工作区，修改直接生效。" if live else
                     f"从 tests/ 目录运行脚本时 import {package} 解析到 site-packages 的安装副本，/testbed 的修改不会被测试到；"
                     "运行测试命令时在前面加 PYTHONPATH=/testbed（官方评分会重新安装，无需在此处安装）。"),
        }
        return self._package_import

    def _create_argv(
        self,
        request: SandboxExecutionRequest,
        context: WorkspaceContext,
        artifacts: CommandArtifactPaths,
    ) -> list[str]:
        common_git = _git_path(context.active_root, "--git-common-dir")
        active_git = context.active_root / ".git"
        if not active_git.exists() or not common_git.is_dir():
            raise ValueError("实验工作目录缺少Git元数据")
        image = _replace_tag_with_digest(
            self.prepared_source.image_ref, self.prepared_source.image_digest
        )
        container_home = self.RUNTIME_ROOT / "home"
        container_temp = self.RUNTIME_ROOT / "tmp"
        mounts: list[tuple[Path, Path, bool]] = [
            (context.active_root, self.CONTAINER_WORKSPACE, False),
            (active_git, self.CONTAINER_WORKSPACE / ".git", True),
            *(((common_git, common_git, True),) if active_git.is_file() else ()),
            (self._task_root, self.RUNTIME_ROOT, False),
            (_scratch_dir(artifacts), self.SCRATCH_DIR, False),
        ]
        for path in request.policy.protected_readonly_paths:
            path = Path(path)
            if path.is_relative_to(context.active_root) and path.exists():
                mounts.append((path, self.CONTAINER_WORKSPACE / path.relative_to(context.active_root), True))
        denied = [Path(p) for p in request.policy.hard_deny_paths if Path(p).is_relative_to(context.active_root) and Path(p).exists()]
        for index, path in enumerate(denied):
            if any(parent in denied for parent in path.parents):
                continue
            mask = self._task_root / 'masks' / str(index)
            mask.mkdir(exist_ok=True) if path.is_dir() else mask.touch()
            destination = self.CONTAINER_WORKSPACE / path.relative_to(context.active_root)
            mounts = [item for item in mounts if not item[1].is_relative_to(destination)]
            mounts.append((mask, destination, True))
        mounts = _deduplicate_mounts(mounts)
        argv = [
            "--network", "none",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--workdir", str(self.CONTAINER_WORKSPACE),
            "--label", "codeagent.swebench=true",
            "--label", "codeagent.swebench.instance=" + self.prepared_source.instance_id,
        ]
        for name, value in {
            "HOME": str(container_home),
            "TMPDIR": str(container_temp),
            "XDG_CACHE_HOME": str(container_home / "cache"),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        }.items():
            argv.extend(("--env", f"{name}={value}"))
        for source, destination, readonly in mounts:
            value = f"type=bind,src={source},dst={destination}"
            if readonly:
                value += ",readonly"
            argv.extend(("--mount", value))
        argv.extend((image, 'python', '-I', '-S', '-c', _CONTAINER_INIT))
        return argv

    def _exec_argv(self, request, marker):
        wrapper = (
            'printf started > "$1"; '
            'if [ -f /opt/miniconda3/etc/profile.d/conda.sh ]; then '
            'source /opt/miniconda3/etc/profile.d/conda.sh && conda activate testbed; fi; '
            'exec /bin/bash --noprofile --norc -c "$2"'
        )
        return ['--workdir', str(self.CONTAINER_WORKSPACE / request.command.cwd), self.container_id,
                '/bin/bash', '--noprofile', '--norc', '-c', wrapper,
                'codeagent-docker-launcher', str(self.RUNTIME_ROOT / 'markers' / marker.name), request.command.command]

    @staticmethod
    def _failure(detail: str, started_at: str, started: float, request: SandboxExecutionRequest, *, stopped=True, payload_started=False) -> CommandResult:
        return CommandResult(
            exit_code=None,
            status=CommandExecutionStatus.SANDBOX_SETUP_FAILED,
            spawn_error=detail,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=max(0, round((time.monotonic() - started) * 1000)),
            process_tree_stopped=stopped,
            payload_started=payload_started,
            effective_policy=request.policy.summary(),
            environment_contract_revision=SWEbenchDockerCommandExecutor.ENVIRONMENT_CONTRACT_REVISION,
            backend="swebench_docker", backend_availability="unavailable",
            launch_cwd=str(SWEbenchDockerCommandExecutor.CONTAINER_WORKSPACE / request.command.cwd),
            effective_network_policy=request.policy.network_mode.value,
            effective_filesystem_policy=_filesystem_policy(request.policy.summary()),
        )


# 各仓库官方评测使用的运行器前缀（来自固定 swe-bench-tasks 的 eval.sh）；只给运行方式，
# 不包含任何具体测试文件或隐藏测试选择。
_TEST_ENTRYPOINTS = {
    "django__django": {
        "command": "./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1 <app_label>[.<test_module>]",
        "notes": "在 /testbed 根目录运行；参数是 tests/ 下的测试模块点分标签，如 model_formsets.test_uuid。没有安装 pytest。",
    },
    "sympy__sympy": {
        "command": "PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' bin/test -C --verbose <path/to/test_file.py>",
        "notes": "没有安装 pytest；可附加 -k <name> 选择测试函数。doctest 用 bin/doctest <path>。",
    },
    "sphinx-doc__sphinx": {
        "command": "tox --current-env -epy39 -v -- <tests/test_file.py>",
        "notes": "使用当前环境的 pytest；可在 -- 后附加 -k <name>。",
    },
}
_DEFAULT_TEST_ENTRYPOINT = {"command": "pytest -rA <path/to/test_file.py>", "notes": "可附加 -k <name> 选择测试。"}


_REPOSITORY_PACKAGES = {
    "django__django": "django", "sympy__sympy": "sympy", "scikit-learn__scikit-learn": "sklearn",
    "matplotlib__matplotlib": "matplotlib", "psf__requests": "requests", "astropy__astropy": "astropy",
    "pydata__xarray": "xarray", "pytest-dev__pytest": "_pytest", "pylint-dev__pylint": "pylint",
    "pylint-dev__astroid": "astroid", "sphinx-doc__sphinx": "sphinx", "pallets__flask": "flask",
    "mwaskom__seaborn": "seaborn", "pydicom__pydicom": "pydicom", "pvlib__pvlib-python": "pvlib",
    "sqlfluff__sqlfluff": "sqlfluff", "pyvista__pyvista": "pyvista", "marshmallow-code__marshmallow": "marshmallow",
}


def repository_package(instance_id: str) -> str | None:
    return _REPOSITORY_PACKAGES.get(instance_id.rsplit("-", 1)[0]) if "__" in instance_id else None


def probe_package_import(image: str, package: str) -> str | None:
    """在镜像的 tests/ 目录下导入仓库包并返回其 __file__；复现测试脚本的 sys.path，而不是 /testbed 下的 `python -c`。"""
    script = _import_probe_script(package)
    done = subprocess.run(["docker", "run", "--rm", "--network", "none", "-w", "/testbed", image, "bash", "-c", script],
                          capture_output=True, text=True, timeout=180)
    if done.returncode != 0:
        return None
    line = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""
    return line if line.startswith("/") else None


def _import_probe_script(package):
    return ("source /opt/miniconda3/etc/profile.d/conda.sh 2>/dev/null && conda activate testbed 2>/dev/null; "
            "cd /testbed/tests 2>/dev/null || cd /testbed/testing 2>/dev/null || cd /testbed; "
            f"python -c 'import {package}; print({package}.__file__)'")


def repository_test_entrypoint(instance_id: str) -> dict | None:
    repo = instance_id.rsplit("-", 1)[0] if "__" in instance_id else ""
    if not repo:
        return None
    return {**_TEST_ENTRYPOINTS.get(repo, _DEFAULT_TEST_ENTRYPOINT), "source": "swebench_task_eval_runner"}


def _scratch_dir(artifacts: CommandArtifactPaths) -> Path:
    """Session级scratch：跨命令保留，位于题目项目之外，不进入预测补丁。"""
    path = artifacts.runtime_home.parent.parent / "scratch"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _filesystem_policy(policy: dict) -> dict:
    return {key: policy[key] for key in (
        "policy_revision", "read_grants", "write_grants", "hard_deny_paths", "protected_readonly_paths",
    )}


def _git_path(root: Path, flag: str) -> Path:
    path = Path(_git_text(root, "rev-parse", flag))
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _git_text(root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True,
            shell=False, check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"Git identity调用失败: {exc}") from exc
    if proc.returncode:
        raise ValueError((proc.stderr or proc.stdout or "Git identity command failed").strip())
    return proc.stdout.strip()


def _deduplicate_mounts(mounts: list[tuple[Path, Path, bool]]) -> list[tuple[Path, Path, bool]]:
    by_destination: dict[Path, tuple[Path, Path, bool]] = {}
    for source, destination, readonly in mounts:
        source = source.resolve(strict=True)
        destination = destination if destination.is_absolute() else destination.resolve()
        current = by_destination.get(destination)
        if current is not None and current[0] != source:
            raise ValueError(f"Docker mount destination冲突: {destination}")
        by_destination[destination] = (source, destination, readonly and (current[2] if current else True))
    return list(by_destination.values())


class _CappedOutput(threading.Thread):
    def __init__(self, source, target: Path, limit: int) -> None:
        super().__init__(daemon=True)
        self.source = source
        self.target = target
        self.limit = limit
        self.truncated = False

    def run(self) -> None:
        written = 0
        with self.target.open("wb") as output:
            while True:
                chunk = self.source.read(65536)
                if not chunk:
                    break
                remaining = max(0, self.limit - written)
                if remaining:
                    output.write(chunk[:remaining])
                    written += min(len(chunk), remaining)
                if len(chunk) > remaining:
                    self.truncated = True
