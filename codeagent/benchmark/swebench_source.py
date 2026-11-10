from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence


class SWEbenchPreparationError(RuntimeError):
    """SWE-bench source 获取或准入失败；该失败不应归因给 Agent。"""

    def __init__(self, stage: str, detail: str) -> None:
        self.stage = stage
        self.detail = detail
        super().__init__(f"SWE-bench source preparation 在 {stage} 阶段失败: {detail}")


@dataclass(frozen=True)
class SWEbenchTaskSource:
    instance_id: str
    image: str
    dataset_base_commit: str


@dataclass(frozen=True)
class PreparedSWEbenchSource:
    instance_id: str
    image_ref: str
    image_digest: str
    dataset_base_commit: str
    prepared_head: str
    prepared_tree: str
    source_root: Path
    manifest_path: Path
    cache_hit: bool


@dataclass(frozen=True)
class SWEbenchImage:
    instance_id: str
    image_ref: str
    image_digest: str
    dataset_base_commit: str


CommandRunner = Callable[[Sequence[str], int], subprocess.CompletedProcess[str]]
_SAFE_INSTANCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z")
_OBJECT_ID = re.compile(r"[0-9a-f]{40,64}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class SWEbenchSourcePreparer:
    """正式入口打开原生单题容器；prepare仅保留为历史源码缓存工具。"""

    MANIFEST_VERSION = 1

    def __init__(
        self,
        cache_root: str | Path | None = None,
        *,
        command_runner: CommandRunner | None = None,
        docker_timeout_seconds: int = 1800,
        allow_pull: bool = True,
        network_config=None,
    ) -> None:
        self.cache_root = Path(cache_root).expanduser().resolve() if cache_root is not None else None
        self.command_runner = command_runner or _run_command
        self.docker_timeout_seconds = docker_timeout_seconds
        self.allow_pull = allow_pull
        from codeagent.benchmark.swebench_network import SWEbenchNetworkConfig
        self.network_config = network_config or SWEbenchNetworkConfig()

    def prepare_image(self, task: SWEbenchTaskSource) -> SWEbenchImage:
        """优先使用已缓存镜像，不导出或重建源码。"""
        self._validate_task(task)
        try:
            raw = self._docker("image", "inspect", task.image)
        except SWEbenchPreparationError as exc:
            if not self.allow_pull or not any(x in str(exc).lower() for x in ("no such image", "not found")):
                raise
            digest = self._resolve_image_digest(task.image)
        else:
            details = json.loads(raw)[0]
            digests = {v.rsplit("@", 1)[1] for v in details.get("RepoDigests", []) if "@" in v}
            if len(digests) != 1 or not _DIGEST.fullmatch(next(iter(digests))):
                raise SWEbenchPreparationError("image_identity", "镜像缺少唯一RepoDigest")
            digest = next(iter(digests))
        return SWEbenchImage(task.instance_id, task.image, digest, task.dataset_base_commit)

    def open_workspace(self, task: SWEbenchTaskSource, root: Path):
        from codeagent.benchmark.swebench_container import SWEbenchContainerWorkspace
        return SWEbenchContainerWorkspace(self.prepare_image(task), root, network_config=self.network_config)

    def prepare(self, task: SWEbenchTaskSource) -> PreparedSWEbenchSource:
        if self.cache_root is None:
            raise SWEbenchPreparationError('legacy_source_cache', '历史源码导出工具需显式指定cache_root；正式入口使用open_workspace')
        self._validate_task(task)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        image_digest = self._resolve_image_digest(task.image)
        cache_dir = self.cache_root / "sources" / task.instance_id / image_digest.removeprefix("sha256:")
        cached = self._load_cached(cache_dir, task, image_digest)
        if cached is not None:
            return cached

        staging_parent = self.cache_root / "staging"
        staging_parent.mkdir(parents=True, exist_ok=True)
        staging = staging_parent / f"{task.instance_id}-{uuid.uuid4().hex}"
        source = staging / "source"
        staging.mkdir()
        source.mkdir()
        container_id: str | None = None
        try:
            immutable_image = _replace_tag_with_digest(task.image, image_digest)
            container_id = self._docker("create", immutable_image, "tail", "-f", "/dev/null").strip()
            if not container_id:
                raise SWEbenchPreparationError("container_create", "docker create 未返回 container id")
            self._docker("cp", f"{container_id}:/testbed/.", str(source))
            self._remove_container(container_id, strict=True)
            container_id = None
            prepared_head, prepared_tree = self._admit_source(source, task.dataset_base_commit)
            manifest = {
                "manifest_version": self.MANIFEST_VERSION,
                "instance_id": task.instance_id,
                "image_ref": task.image,
                "image_digest": image_digest,
                "dataset_base_commit": task.dataset_base_commit,
                "prepared_head": prepared_head,
                "prepared_tree": prepared_tree,
                "source_status": "clean",
                "prepared_at": datetime.now(timezone.utc).isoformat(),
            }
            (staging / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            cache_dir.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(staging, cache_dir)
            except OSError:
                # 同一 instance/digest 的并发准备只允许一个结果获胜。
                cached = self._load_cached(cache_dir, task, image_digest)
                if cached is None:
                    raise
                return cached
            return self._result(cache_dir, manifest, cache_hit=False)
        except SWEbenchPreparationError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise SWEbenchPreparationError("source_export", str(exc)) from exc
        finally:
            if container_id:
                self._remove_container(container_id, strict=False)
            if staging.exists():
                shutil.rmtree(staging)

    def _resolve_image_digest(self, image: str) -> str:
        self._docker("pull", image)
        raw = self._docker("image", "inspect", image)
        try:
            inspected = json.loads(raw)
            repo_digests = inspected[0]["RepoDigests"]
            digests = [value.rsplit("@", 1)[1] for value in repo_digests if "@" in value]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise SWEbenchPreparationError("image_identity", "无法解析 docker image inspect 输出") from exc
        unique = sorted({value for value in digests if _DIGEST.fullmatch(value)})
        if len(unique) != 1:
            raise SWEbenchPreparationError("image_identity", f"镜像没有唯一的 sha256 RepoDigest: {repo_digests!r}")
        return unique[0]

    def _load_cached(
        self, cache_dir: Path, task: SWEbenchTaskSource, image_digest: str
    ) -> PreparedSWEbenchSource | None:
        if not cache_dir.exists():
            return None
        manifest_path = cache_dir / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SWEbenchPreparationError("cache_validation", f"cache manifest 无效: {exc}") from exc
        expected = {
            "manifest_version": self.MANIFEST_VERSION,
            "instance_id": task.instance_id,
            "image_ref": task.image,
            "image_digest": image_digest,
            "dataset_base_commit": task.dataset_base_commit,
            "source_status": "clean",
        }
        if any(manifest.get(key) != value for key, value in expected.items()):
            raise SWEbenchPreparationError("cache_validation", "cache manifest identity 不匹配")
        head, tree = self._admit_source(cache_dir / "source", task.dataset_base_commit)
        if head != manifest.get("prepared_head") or tree != manifest.get("prepared_tree"):
            raise SWEbenchPreparationError("cache_validation", "cached source 与 manifest identity 不匹配")
        return self._result(cache_dir, manifest, cache_hit=True)

    def _admit_source(self, source: Path, dataset_base_commit: str) -> tuple[str, str]:
        if not (source / ".git").is_dir():
            raise SWEbenchPreparationError("git_admission", "导出的 /testbed/.git 不是普通目录")
        top = self._git(source, "rev-parse", "--show-toplevel")
        if Path(top).resolve() != source.resolve():
            raise SWEbenchPreparationError("git_admission", "导出的 /testbed 不是 repository 根目录")
        if self._git(source, "rev-parse", "--is-shallow-repository") != "false":
            raise SWEbenchPreparationError("git_admission", "prepared source 是 shallow repository")
        if (source / ".git" / "objects" / "info" / "alternates").exists():
            raise SWEbenchPreparationError("git_admission", "prepared source 使用 Git object alternates")
        status = self._git(source, "status", "--porcelain=v1", "--untracked-files=all")
        if status:
            raise SWEbenchPreparationError("git_admission", f"prepared source 包含 tracked/untracked changes: {status[:1000]}")
        base = self._git(source, "rev-parse", f"{dataset_base_commit}^{{commit}}")
        if base != dataset_base_commit:
            raise SWEbenchPreparationError("git_admission", "dataset base commit identity 不匹配")
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", dataset_base_commit, "HEAD"],
            cwd=source, capture_output=True, text=True, check=False, timeout=20,
        )
        if ancestor.returncode != 0:
            raise SWEbenchPreparationError("git_admission", "dataset base commit 不是 prepared HEAD 的 ancestor")
        head = self._git(source, "rev-parse", "HEAD")
        tree = self._git(source, "rev-parse", "HEAD^{tree}")
        if not _OBJECT_ID.fullmatch(head) or not _OBJECT_ID.fullmatch(tree):
            raise SWEbenchPreparationError("git_admission", "prepared Git identity 格式无效")
        # 不对完整历史执行 `git fsck --full`。部分经典仓库包含与目标快照无关的
        # 旧对象格式或损坏的远端引用，但官方 image 中的 prepared HEAD 仍可正常
        # checkout。这里验证benchmark依赖的commit/tree可读取；独立实验clone
        # 的实际检出由prepare_swebench_workspace验证，不创建Runtime worktree。
        self._git(source, "cat-file", "-e", f"{base}^{{tree}}")
        self._git(source, "cat-file", "-e", f"{head}^{{tree}}")

        return head, tree

    def _docker(self, *args: str) -> str:
        try:
            proc = self.command_runner(["docker", *args], self.docker_timeout_seconds)
        except (OSError, subprocess.SubprocessError) as exc:
            stage = "image_pull" if args and args[0] == "pull" else "docker"
            raise SWEbenchPreparationError(stage, f"Docker 调用失败: {exc}") from exc
        if proc.returncode:
            detail = (proc.stderr or proc.stdout or "docker command failed").strip()
            stage = "image_pull" if args and args[0] == "pull" else "docker"
            raise SWEbenchPreparationError(stage, detail[-4000:])
        return proc.stdout.strip()

    def _remove_container(self, container_id: str, *, strict: bool) -> None:
        try:
            proc = self.command_runner(["docker", "rm", "-f", container_id], 60)
        except (OSError, subprocess.SubprocessError) as exc:
            if strict:
                raise SWEbenchPreparationError("container_cleanup", f"Docker container 清理失败: {exc}") from exc
            return
        if strict and proc.returncode:
            detail = (proc.stderr or proc.stdout or "docker rm failed").strip()
            raise SWEbenchPreparationError("container_cleanup", detail[-4000:])

    @staticmethod
    def _git(cwd: Path, *args: str) -> str:
        try:
            proc = subprocess.run(
                ["git", *args], cwd=cwd, capture_output=True, text=True, check=False, timeout=60
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SWEbenchPreparationError("git_admission", f"Git 调用失败: {exc}") from exc
        if proc.returncode:
            raise SWEbenchPreparationError("git_admission", (proc.stderr or proc.stdout).strip()[-4000:])
        return proc.stdout.strip()

    @staticmethod
    def _validate_task(task: SWEbenchTaskSource) -> None:
        if not _SAFE_INSTANCE.fullmatch(task.instance_id):
            raise SWEbenchPreparationError("input", "instance_id 格式无效")
        if not task.image or any(ch.isspace() for ch in task.image):
            raise SWEbenchPreparationError("input", "image reference 格式无效")
        if not _OBJECT_ID.fullmatch(task.dataset_base_commit):
            raise SWEbenchPreparationError("input", "dataset_base_commit 格式无效")

    @staticmethod
    def _result(cache_dir: Path, manifest: dict[str, object], *, cache_hit: bool) -> PreparedSWEbenchSource:
        return PreparedSWEbenchSource(
            instance_id=str(manifest["instance_id"]),
            image_ref=str(manifest["image_ref"]),
            image_digest=str(manifest["image_digest"]),
            dataset_base_commit=str(manifest["dataset_base_commit"]),
            prepared_head=str(manifest["prepared_head"]),
            prepared_tree=str(manifest["prepared_tree"]),
            source_root=cache_dir / "source",
            manifest_path=cache_dir / "manifest.json",
            cache_hit=cache_hit,
        )


def _replace_tag_with_digest(image: str, digest: str) -> str:
    repository = image.split("@", 1)[0]
    slash = repository.rfind("/")
    colon = repository.rfind(":")
    if colon > slash:
        repository = repository[:colon]
    return f"{repository}@{digest}"


def _run_command(argv: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv), capture_output=True, text=True, shell=False, check=False, timeout=timeout
    )
