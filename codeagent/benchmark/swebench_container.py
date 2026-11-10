"""SWE单题容器工作区：源码与依赖始终留在官方镜像内。"""
from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import threading
import time
from types import SimpleNamespace

from codeagent.benchmark.swebench_executor import (
    DockerCLIExecutionBackend, _CONTAINER_INIT,
    repository_test_entrypoint, repository_package,
)
from codeagent.benchmark.swebench_source import _replace_tag_with_digest
from codeagent.benchmark.swebench_network import SWEbenchEgress, SWEbenchNetworkConfig, EVALUATION_RULES, write_network_identity
from codeagent.benchmark.swebench_compat import compatibility_arguments, activate_compatibility
from codeagent.runtime.artifacts import output_preview
from codeagent.runtime.atomic_edit import AtomicEditError, AtomicEditService
from codeagent.runtime.command import CommandExecutionStatus, CommandResult, command_cancellation
from codeagent.runtime.permissions import NetworkMode
from codeagent.safety.path_guard import PathGuardError
from codeagent.tools.base import ToolResult, ToolSpec
from codeagent.tools.fs_read import build_fs_tools
from codeagent.tools.git_read import build_git_tools
from codeagent.workspace.workspace import WorkspaceContext


TOOL_ROOT = "/opt/codeagent-tools"
_WORKER = ("import sys; sys.path.insert(0, '" + TOOL_ROOT + "'); "
           "from codeagent.workspace.docker_worker import main; main()")
_BUNDLE_MODULES = (
    "__init__", "config", "tools/__init__", "tools/output", "tools/fs_read", "tools/git_read",
    "runtime/__init__", "runtime/repository_search", "safety/__init__", "safety/path_guard",
    "safety/repository_access", "workspace/__init__", "workspace/workspace", "workspace/docker_worker",
    "workspace/egress_proxy", "workspace/egress_relay",
)


def prepare_tool_bundle(destination):
    """仅部署无模型依赖的工具代码及rg；不改变testbed的Python环境。"""
    package = Path(__file__).resolve().parents[1]
    for name in _BUNDLE_MODULES:
        target = destination / "codeagent" / (name + ".py")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(package / (name + ".py"), target)
    executable = shutil.which("rg")
    if not executable:
        raise RuntimeError("容器搜索工具需要宿主已安装的ripgrep，未下载任何依赖")
    shutil.copy2(executable, destination / "rg-bin")
    # 镜像不一定安装rg/pcre2；只随工具携带其非libc依赖，不修改项目依赖。
    linked = subprocess.run(["ldd", executable], capture_output=True, text=True, timeout=10)
    libraries = destination / "lib"
    libraries.mkdir(exist_ok=True)
    for line in linked.stdout.splitlines():
        if "=> /" not in line:
            continue
        name, rest = line.split("=>", 1)
        if name.strip() in {"libc.so.6", "libm.so.6", "libpthread.so.0", "libdl.so.2", "librt.so.1"}:
            continue
        source = Path(rest.strip().split()[0])
        shutil.copy2(source, libraries / source.name)
    launcher = destination / "rg"
    launcher.write_text('#!/bin/sh\nLD_LIBRARY_PATH=' + TOOL_ROOT + '/lib exec ' + TOOL_ROOT + '/rg-bin "$@"\n')
    launcher.chmod(0o755)


class SWEbenchContainerWorkspace:
    """所有项目工具共享一个容器；宿主目录只保存会话、日志和局部undo。"""
    model_root = "/testbed"
    evaluation_rules = EVALUATION_RULES

    def __init__(self, image, root: Path, *, backend=None, network_config=None):
        self.image = image
        self.root = root
        self.context = WorkspaceContext.source(root / "workspace-identity")
        self.context.active_root.mkdir(parents=True)
        self.backend = backend or DockerCLIExecutionBackend()
        self.lock = threading.RLock()
        self.container_id = None
        self.closed = False
        self.network_config = network_config or SWEbenchNetworkConfig()
        self.egress = None
        self.environment_compatibility = None
        self.tools_dir = root / "container-tools"
        self.command_executor = ContainerCommandExecutor(self)
        try:
            prepare_tool_bundle(self.tools_dir)
            immutable = _replace_tag_with_digest(image.image_ref, image.image_digest)
            write_network_identity(root, self.network_config)
            extra = []
            init = _CONTAINER_INIT
            if self.network_config.mode == 'filtered':
                self.egress = SWEbenchEgress(self.backend, immutable, self.tools_dir, root)
                extra = self.egress.project_arguments()
                init = ("import sys; sys.path.insert(0, '" + TOOL_ROOT + "'); "
                        "from codeagent.workspace.egress_relay import start_relay; relay=start_relay();\n" + init)
            extra += compatibility_arguments(image.instance_id, self.network_config.mode)
            # 匿名只读volume由Docker从镜像填充.git，保护元数据而不导出/克隆源码。
            argv = ["--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--workdir", "/testbed", "--env", "GIT_OPTIONAL_LOCKS=0",
                    "--label", "codeagent.swebench=true", "--label", "codeagent.swebench.instance=" + image.instance_id,
                    "--mount", "type=volume,dst=/testbed/.git,readonly",
                    "--mount", "type=bind,src=" + str(self.tools_dir) + ",dst=" + TOOL_ROOT + ",readonly",
                    *extra, immutable, "python", "-I", "-S", "-c", init]
            self.container_id = self.backend.create(argv)
            self.backend.start(self.container_id)
            self.environment_compatibility = activate_compatibility(
                self.backend, self.container_id, self.tools_dir, image.instance_id, self.network_config.mode)
            self.identity = {"image_ref": image.image_ref, "image_digest": image.image_digest,
                             "dataset_base_commit": image.dataset_base_commit,
                             **self.rpc("identity", {"base_commit": image.dataset_base_commit})}
            self.identity['network_policy'] = self.network_config.identity()
            if self.environment_compatibility:
                self.identity['environment_compatibility'] = self.environment_compatibility
                (root / 'environment-compatibility.json').write_text(
                    json.dumps(self.environment_compatibility, ensure_ascii=False, indent=2) + '\n')
            self.backend._call(["exec", self.container_id, TOOL_ROOT + "/rg", "--version"], 15)
            package = repository_package(image.instance_id)
            resolved = self.backend.probe_import(self.container_id, package) if package else None
            self.package_import = ({"package": package, "resolves_to": resolved,
                "workspace_edits_visible_to_tests": resolved.startswith("/testbed/"),
                "note": ("测试导入当前项目，源码修改直接可见。" if resolved.startswith("/testbed/") else
                         "测试脚本导入的是安装副本；运行项目测试时设置PYTHONPATH=/testbed以使用当前源码。")}
                if resolved else None)
        except BaseException:
            self.close()
            raise

    def rpc(self, operation, arguments=None, *, name=None, denied_paths=()):
        with self.lock:
            if self.closed or self.container_id is None:
                raise RuntimeError("本题容器已关闭，不能自动重建或重放文件操作")
            request = {"operation": operation, "arguments": arguments or {}, "denied_paths": list(denied_paths),
                       "rg": TOOL_ROOT + "/rg", **({"name": name} if name else {})}
            try:
                done = subprocess.run(["docker", "exec", "-i", self.container_id, "/opt/miniconda3/bin/python",
                                       "-I", "-S", "-c", _WORKER],
                                      input=json.dumps(request).encode(), capture_output=True, timeout=150)
                if done.returncode:
                    raise RuntimeError("容器工具传输失败：" + done.stderr.decode("utf-8", "replace")[-4000:])
                response = json.loads(done.stdout)
                if not isinstance(response, dict) or type(response.get('ok')) is not bool or (
                        response['ok'] and 'value' not in response) or (
                        not response['ok'] and not {'error', 'error_type'} <= response.keys()):
                    raise ValueError('容器工具返回协议无效')
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                # docker exec客户端被终止不保证容器内worker已停；结果未知时关闭整题，禁止重放。
                self.close()
                raise RuntimeError('容器工具结果未确认，执行环境已关闭，未自动重试：' + str(exc)) from exc
            if not response["ok"]:
                error = response["error"]
                kind = response["error_type"]
                errors = {"PathGuardError": PathGuardError, "FileNotFoundError": FileNotFoundError,
                          "FileExistsError": FileExistsError, "NotADirectoryError": NotADirectoryError,
                          "IsADirectoryError": IsADirectoryError, "PermissionError": PermissionError,
                          "ValueError": ValueError}
                raise errors.get(kind, RuntimeError)(error)
            return response["value"]

    def build_read_tools(self, context):
        # 定义、schema及校验完全沿用本地接口，只有handler替换为容器传输。
        result = []
        for tool in build_fs_tools(context) + build_git_tools(context):
            def invoke(args, name=tool.name):
                return ToolResult.model_validate(self.rpc("tool", args, name=name, denied_paths=context.denied_paths))
            result.append(ToolSpec.from_model(tool.name, tool.description, tool.permission_level, tool.input_model, invoke))
        return result

    def edit_service(self, context, store):
        return ContainerAtomicEditService(context, store, self)

    def prediction_patch(self):
        with self.lock:
            patch = base64.b64decode(self.rpc("prediction"), validate=True)
            (self.root / "patch.diff").write_bytes(patch)
            return patch

    def close(self):
        with self.lock:
            self.closed = True
            errors = []
            if self.container_id is not None:
                # -v回收本题匿名Git只读volume；源码在容器overlay内，无宿主源码副本。
                try:
                    self.backend._call(["rm", "--force", "--volumes", self.container_id], 30)
                    self.container_id = None
                except RuntimeError as exc:
                    errors.append(str(exc))
            if self.egress is not None:
                try:
                    self.egress.close()
                except RuntimeError as exc:
                    errors.append(str(exc))
            if errors:
                raise RuntimeError('SWE容器资源回收未确认：' + '; '.join(errors))


class ContainerPath:
    """编辑事务所需的文件I/O；每项实际操作都由同一容器执行。"""
    def __init__(self, workspace, relative="."):
        self.workspace, self.relative = workspace, PurePosixPath(relative)

    def __truediv__(self, part):
        return ContainerPath(self.workspace, self.relative / part)

    @property
    def parent(self):
        return ContainerPath(self.workspace, self.relative.parent)

    def invoke(self, operation, **kwargs):
        return self.workspace.rpc(operation, {"path": self.relative.as_posix(), **kwargs})

    def stat(self):
        info = self.invoke("stat")
        if info is None:
            raise FileNotFoundError(str(self.relative))
        return SimpleNamespace(**info)

    def exists(self):
        return self.invoke("stat") is not None

    def is_symlink(self):
        # worker已逐级拒绝symlink，不将指向其他路径的内容误作本文件。
        self.invoke("check")
        return False

    def is_file(self):
        info = self.invoke("stat")
        return info is not None and stat.S_ISREG(info["st_mode"])

    def is_dir(self):
        info = self.invoke("stat")
        return info is not None and stat.S_ISDIR(info["st_mode"])

    def read_bytes(self):
        return base64.b64decode(self.invoke("read"), validate=True)

    def mkdir(self, *, parents=False, exist_ok=False):
        self.invoke("mkdir", parents=parents, exist_ok=exist_ok)

    def unlink(self):
        self.invoke("unlink")

    def rmdir(self):
        self.invoke("rmdir")

    def iterdir(self):
        return (self / name for name in self.invoke("list"))


class ContainerAtomicEditService(AtomicEditService):
    """共用编辑计划、冲突检查、备份及undo；仅替换项目文件I/O。"""
    def __init__(self, context, store, workspace):
        super().__init__(context, store)
        self.workspace = workspace

    def apply_workspace_edit(self, arguments):
        with self.workspace.lock:
            return super().apply_workspace_edit(arguments)

    def undo(self, turn_id=None):
        with self.workspace.lock:
            return super().undo(turn_id)

    def _validate_workspace(self):
        if not ContainerPath(self.workspace).is_dir():
            raise AtomicEditError("path_rejected", "容器项目目录不存在")

    def _checked_path(self, relative):
        if any(p in {".git", ".codeagent"} for p in PurePosixPath(relative).parts):
            raise AtomicEditError("path_rejected", "不能编辑Git或Runtime内部记录")
        try:
            self.workspace.rpc("check", {"path": relative}, denied_paths=self.context.denied_paths)
        except (PathGuardError, ValueError) as exc:
            raise AtomicEditError("path_rejected", str(exc)) from exc
        return ContainerPath(self.workspace, relative)

    def _write_path(self, target, content):
        target.invoke("write", content=base64.b64encode(content).decode("ascii"))

    def _read_text_file(self, target, relative):
        if target.stat().st_size > self.MAX_FILE_BYTES:
            raise AtomicEditError("limit_exceeded", "文件超过大小限制：" + relative)
        return super()._read_text_file(target, relative)

    @staticmethod
    def _chmod(target, mode):
        target.invoke("chmod", mode=mode)


class ContainerCommandExecutor:
    """共用CommandService及现有Docker超时/输出/进程回收实现。"""
    supports_denied_paths = False  # 测评不开放额外仓库deny配置；不能静默忽略shell隔离。
    manages_workspace_paths = True
    OUTPUT_LIMIT_BYTES = 2 * 1024 * 1024

    def __init__(self, workspace):
        self.workspace = workspace
        self.cancelled = lambda: False

    def resolve_cwd(self, context, value):
        raw = PurePosixPath(value)
        if raw.is_absolute() or '..' in raw.parts or '\0' in value:
            raise PathGuardError('cwd必须是项目内相对路径')
        # 排队准备不等待另一条长命令；execute取得锁后会再次实查cwd。
        if not self.workspace.lock.acquire(blocking=False):
            return raw.as_posix()
        try:
            info = self.workspace.rpc("check", {"path": value}, denied_paths=context.denied_paths)
            if info is None or not stat.S_ISDIR(info["st_mode"]):
                raise ValueError("cwd不存在或不是容器项目内目录")
            return raw.as_posix()
        finally:
            self.workspace.lock.release()

    def environment_snapshot(self, context, artifacts, effective):
        return {"contract_revision": "swebench-container-workspace-v1", "backend": "swebench_docker",
                "backend_availability": "available" if not self.workspace.closed else "closed",
                "default_cwd": ".", "default_cwd_resolves_to": "/testbed",
                "project_file_operations": "same_container", "network_mode": self.network_config.mode,
                "network_policy": self.network_config.identity(), "evaluation_rules": EVALUATION_RULES,
                **({"environment_compatibility": self.workspace.environment_compatibility}
                   if getattr(self.workspace, 'environment_compatibility', None) else {}),
                "home_kind": "container_home", "tilde_is_host_home": False, "tmp_kind": "container_tmp",
                "commands_are_fresh_processes": True, "shell_state_persists": False,
                "container_state_persists": True, "container_lifetime": "one_task",
                "workspace_files_persist": True, "scratch_dir": "/tmp",
                "git_metadata_access": "read_only", "extra_host_mounts_supported": False,
                **({"package_import": self.workspace.package_import} if self.workspace.package_import else {}),
                "file_lifetime_guidance": "本题所有项目工具操作同一容器/testbed。/tmp和安装依赖在题内保留；每条命令使用新shell，cd/export不跨调用保留，后台进程在命令结束时回收。复现脚本放/tmp，项目内调试文件会进入补丁。",
                **({"test_entrypoint": entry} if (entry := repository_test_entrypoint(self.workspace.image.instance_id)) else {})}

    def execute(self, request, context, artifacts):
        started_at = datetime.now(timezone.utc).isoformat()
        started = time.monotonic()
        command = request.command
        failure = self._failure
        if artifacts is None:
            return failure("Docker命令需要持久输出目录", started_at, started, request)
        required = NetworkMode.HOST if self.network_config.mode == 'filtered' else NetworkMode.OFF
        if request.policy.network_mode != required or request.policy.read_grants or request.policy.write_grants:
            return failure("SWE容器授权与选定网络条件不匹配，或包含不支持的额外宿主授权，命令未启动", started_at, started, request)
        cancelled = command_cancellation(self.cancelled)
        # 主子与文件事务共用同一锁；kill(-1)不会清理另一工具的进程。
        while not self.workspace.lock.acquire(timeout=.05):
            if cancelled():
                return replace(failure("命令启动前已取消", started_at, started, request), status=CommandExecutionStatus.CANCELLED)
        payload_dispatched = False
        try:
            if self.workspace.closed or cancelled():
                return replace(failure("容器已关闭或命令已取消", started_at, started, request), status=CommandExecutionStatus.CANCELLED)
            self.resolve_cwd(context, command.cwd)
            wrapper = ('source /opt/miniconda3/etc/profile.d/conda.sh && '
                       'conda activate testbed && exec /bin/bash --noprofile --norc -c "$1"')
            argv = ["--workdir", str(PurePosixPath("/testbed") / command.cwd), self.workspace.container_id,
                    "/bin/bash", "--noprofile", "--norc", "-c", wrapper,
                    "codeagent-docker-command", command.command]
            payload_dispatched = True
            outcome = self.workspace.backend.execute(self.workspace.container_id, argv,
                artifacts.stdout_path, artifacts.stderr_path, command.timeout_seconds,
                self.OUTPUT_LIMIT_BYTES, cancelled)
            if not outcome.process_tree_stopped:
                self.workspace.close()
            stdout, out_preview = output_preview(artifacts.stdout_path)
            stderr, err_preview = output_preview(artifacts.stderr_path)
            status = (CommandExecutionStatus.CANCELLED if outcome.cancelled else
                      CommandExecutionStatus.TIMED_OUT if outcome.timed_out else CommandExecutionStatus.COMPLETED)
            if not outcome.process_tree_stopped:
                status = CommandExecutionStatus.SANDBOX_SETUP_FAILED
            return CommandResult(exit_code=outcome.exit_code, stdout=stdout, stderr=stderr,
                stdout_preview_truncated=out_preview, stderr_preview_truncated=err_preview,
                stdout_truncated=outcome.stdout_truncated, stderr_truncated=outcome.stderr_truncated,
                stdout_artifact=str(artifacts.stdout_path), stderr_artifact=str(artifacts.stderr_path),
                status=status, started_at=started_at, finished_at=datetime.now(timezone.utc).isoformat(),
                duration_ms=round((time.monotonic()-started)*1000), timed_out=outcome.timed_out,
                payload_started=True, process_tree_stopped=outcome.process_tree_stopped or self.workspace.closed,
                spawn_error=None if outcome.process_tree_stopped else '命令后代回收未确认，整题容器已关闭',
                effective_policy=request.policy.summary(), environment_contract_revision="swebench-container-workspace-v1",
                effective_filesystem_policy={"workspace": "/testbed", "git_metadata": "read_only",
                                             "host_project_mounts": False},
                backend="swebench_docker", backend_availability="available", effective_network_policy=self.network_config.mode,
                launch_cwd=str(PurePosixPath("/testbed") / command.cwd))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            if payload_dispatched:
                # transport异常可能发生在payload启动后；先关闭容器，不能留下晚到副作用。
                try:
                    self.workspace.close()
                except RuntimeError as cleanup:
                    return failure(str(exc) + '; ' + str(cleanup), started_at, started, request,
                                   stopped=False, payload_started=True)
                return failure(str(exc), started_at, started, request, payload_started=True)
            return failure(str(exc), started_at, started, request)
        finally:
            self.workspace.lock.release()

    def close(self):
        self.workspace.close()

    @property
    def network_config(self):
        return getattr(self.workspace, 'network_config', SWEbenchNetworkConfig())

    def _failure(self, detail, started_at, started, request, *, stopped=True, payload_started=False):
        return CommandResult(exit_code=None, status=CommandExecutionStatus.SANDBOX_SETUP_FAILED,
            spawn_error=detail, started_at=started_at, finished_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=round((time.monotonic()-started)*1000), process_tree_stopped=stopped,
            payload_started=payload_started, effective_policy=request.policy.summary(),
            environment_contract_revision='swebench-container-workspace-v1',
            backend='swebench_docker', backend_availability='unavailable',
            launch_cwd=str(PurePosixPath('/testbed') / request.command.cwd), effective_network_policy=self.network_config.mode)
