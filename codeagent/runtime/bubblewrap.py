from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from codeagent.runtime.permissions import EffectiveSandboxPolicy, NetworkMode


class SandboxUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class MountOperation:
    mode: str
    source: Path | None
    destination: Path
    size_bytes: int | None = None


@dataclass(frozen=True)
class MountPlan:
    operations: tuple[MountOperation, ...]

    @classmethod
    def build(cls, policy: EffectiveSandboxPolicy, mask_dir: Path, mask_file: Path) -> "MountPlan":
        writes = {path.resolve() for path in policy.write_grants}
        writes.update({policy.candidate_root.resolve(), policy.runtime_cache.resolve()})
        source = policy.source_root.resolve()
        protected = {path.resolve(strict=False) for path in policy.protected_readonly_paths if path.exists()}
        denied = {path.resolve(strict=False) for path in policy.hard_deny_paths if path.exists()}
        operations: list[MountOperation] = [
            MountOperation("ro_bind", Path("/"), Path("/")),
            # 先建立 private /tmp，再把位于 /tmp 下的测试/临时 Candidate 根叠加回来。
            MountOperation("tmpfs", None, Path("/tmp"), 256 * 1024 * 1024),
        ]
        tmp_destinations = {*writes, *protected, *denied}
        if source.exists():
            tmp_destinations.add(source)
        tmp_dirs: set[Path] = set()
        for path in tmp_destinations:
            try:
                relative = path.relative_to("/tmp")
            except ValueError:
                continue
            current = Path("/tmp")
            parts = relative.parts if path.is_dir() else relative.parts[:-1]
            for part in parts:
                current /= part
                tmp_dirs.add(current)
        operations.extend(MountOperation("dir", None, path) for path in sorted(tmp_dirs, key=lambda item: (len(item.parts), str(item))))
        for path in sorted(writes, key=lambda item: (len(item.parts), str(item))):
            operations.append(MountOperation("bind", path, path))
        if source.exists():
            operations.append(MountOperation("ro_bind", source, source))
        for path in sorted(protected, key=lambda item: (len(item.parts), str(item))):
            operations.append(MountOperation("ro_bind", path, path))
        for path in sorted(denied, key=lambda item: (len(item.parts), str(item))):
            mask = mask_dir if path.is_dir() else mask_file
            operations.append(MountOperation("mask", mask, path))
        return cls(tuple(operations))

    def to_bwrap_args(self) -> list[str]:
        args: list[str] = []
        for operation in self.operations:
            if operation.mode == "ro_bind":
                args.extend(("--ro-bind", str(operation.source), str(operation.destination)))
            elif operation.mode == "bind":
                args.extend(("--bind", str(operation.source), str(operation.destination)))
            elif operation.mode == "mask":
                args.extend(("--ro-bind", str(operation.source), str(operation.destination)))
            elif operation.mode == "tmpfs":
                args.extend(("--size", str(operation.size_bytes), "--tmpfs", str(operation.destination)))
            elif operation.mode == "dir":
                args.extend(("--dir", str(operation.destination)))
            else:
                raise ValueError(f"unknown mount operation: {operation.mode}")
        return args


@dataclass(frozen=True)
class BubblewrapFeatures:
    executable: Path
    version: str
    disable_userns: bool


class BubblewrapBackend:
    def __init__(self, executable: Path | None = None) -> None:
        self._configured = executable

    def discover(self, candidate_root: Path) -> Path:
        raw = self._configured or (Path(found) if (found := shutil.which("bwrap")) else None)
        if raw is None:
            raise SandboxUnavailable("未安装 Bubblewrap (bwrap)；任意命令保持关闭")
        try:
            path = raw.expanduser().resolve(strict=True)
            mode = path.stat().st_mode
        except OSError as exc:
            raise SandboxUnavailable(f"Bubblewrap executable 无法复检: {exc}") from exc
        if not stat.S_ISREG(mode) or not os.access(path, os.X_OK):
            raise SandboxUnavailable("Bubblewrap path 不是可执行普通文件")
        try:
            path.relative_to(candidate_root.resolve())
        except ValueError:
            return path
        raise SandboxUnavailable("拒绝使用 Candidate 内可被命令修改的 bwrap")

    def probe(self, candidate_root: Path) -> BubblewrapFeatures:
        executable = self.discover(candidate_root)
        try:
            version = subprocess.run(
                [str(executable), "--version"], capture_output=True, text=True,
                shell=False, check=True, timeout=5,
            ).stdout.strip()
            help_text = subprocess.run(
                [str(executable), "--help"], capture_output=True, text=True,
                shell=False, check=True, timeout=5,
            ).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise SandboxUnavailable(f"Bubblewrap feature probe 失败: {exc}") from exc
        required = ("--ro-bind", "--bind", "--tmpfs", "--unshare-user", "--unshare-pid", "--unshare-net", "--new-session", "--die-with-parent")
        missing = [item for item in required if item not in help_text]
        if missing:
            raise SandboxUnavailable(f"Bubblewrap 缺少所需 capability: {', '.join(missing)}")
        smoke = [
            str(executable), "--ro-bind", "/", "/", "--unshare-user", "--unshare-pid",
            "--unshare-ipc", "--unshare-uts", "--unshare-net", "--new-session",
            "--die-with-parent",
        ]
        if "--disable-userns" in help_text:
            smoke.append("--disable-userns")
        smoke.extend(("--proc", "/proc", "--dev", "/dev", "--", "/bin/true"))
        try:
            result = subprocess.run(smoke, capture_output=True, text=True, shell=False, check=False, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SandboxUnavailable(f"Bubblewrap namespace smoke test 失败: {exc}") from exc
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise SandboxUnavailable(f"Bubblewrap namespace smoke test 不可用: {detail}")
        return BubblewrapFeatures(executable, version, "--disable-userns" in help_text)

    @staticmethod
    def invocation(
        features: BubblewrapFeatures, plan: MountPlan, policy: EffectiveSandboxPolicy,
        cwd: Path, environment: dict[str, str], payload: list[str],
    ) -> list[str]:
        args = [str(features.executable), *plan.to_bwrap_args(), "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts"]
        if policy.network_mode == NetworkMode.OFF:
            args.append("--unshare-net")
        if features.disable_userns:
            args.append("--disable-userns")
        args.extend(("--new-session", "--die-with-parent", "--proc", "/proc", "--dev", "/dev", "--clearenv"))
        for name, value in sorted(environment.items()):
            args.extend(("--setenv", name, value))
        args.extend(("--chdir", str(cwd), "--", *payload))
        return args
