from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

import pytest

from codeagent.benchmark.swebench_source import (
    SWEbenchPreparationError,
    SWEbenchSourcePreparer,
    SWEbenchTaskSource,
    _replace_tag_with_digest,
)


DIGEST = "sha256:" + "a" * 64


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True)
    return proc.stdout.strip()


def prepared_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "official-testbed"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "setup@swebench.invalid")
    git(repo, "config", "user.name", "SWE-bench")
    (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(repo, "add", "module.py")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "commit", "--allow-empty", "-m", "SWE-bench")
    return repo, base, git(repo, "rev-parse", "HEAD")


class FakeDocker:
    def __init__(self, source: Path, *, dirty_export: bool = False) -> None:
        self.source = source
        self.dirty_export = dirty_export
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
        call = tuple(argv)
        self.calls.append(call)
        if call[1] == "pull":
            return self.result(call, stdout="pulled\n")
        if call[1:3] == ("image", "inspect"):
            payload = [{"RepoDigests": [f"example/project@{DIGEST}"]}]
            return self.result(call, stdout=json.dumps(payload))
        if call[1] == "create":
            return self.result(call, stdout="container-123\n")
        if call[1] == "cp":
            target = Path(call[-1])
            shutil.copytree(self.source, target, dirs_exist_ok=True)
            if self.dirty_export:
                (target / "generated.txt").write_text("dirty\n", encoding="utf-8")
            return self.result(call)
        if call[1:3] == ("rm", "-f"):
            return self.result(call)
        return self.result(call, returncode=1, stderr=f"unexpected command: {call}")

    @staticmethod
    def result(
        args: Sequence[str], *, stdout: str = "", stderr: str = "", returncode: int = 0
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def test_prepares_official_testbed_and_reuses_digest_cache(tmp_path: Path) -> None:
    official, base, prepared_head = prepared_repo(tmp_path)
    docker = FakeDocker(official)
    preparer = SWEbenchSourcePreparer(tmp_path / "cache", command_runner=docker)
    task = SWEbenchTaskSource("owner__repo-1", "example/project:latest", base)

    first = preparer.prepare(task)

    assert first.cache_hit is False
    assert first.prepared_head == prepared_head
    assert first.dataset_base_commit == base
    assert first.image_digest == DIGEST
    assert first.source_root.is_dir()
    assert git(first.source_root, "status", "--porcelain", "--untracked-files=all") == ""
    assert len([call for call in docker.calls if call[1] == "create"]) == 1
    assert ("docker", "rm", "-f", "container-123") in docker.calls
    assert list((tmp_path / "cache" / "staging").glob("owner__repo-1-*")) == []

    second = preparer.prepare(task)

    assert second.cache_hit is True
    assert second.source_root == first.source_root
    assert len([call for call in docker.calls if call[1] == "create"]) == 1
    manifest = json.loads(second.manifest_path.read_text(encoding="utf-8"))
    assert manifest["dataset_base_commit"] == base
    assert manifest["prepared_head"] == prepared_head
    assert manifest["source_status"] == "clean"


def test_rejects_dirty_export_and_removes_container(tmp_path: Path) -> None:
    official, base, _ = prepared_repo(tmp_path)
    docker = FakeDocker(official, dirty_export=True)
    preparer = SWEbenchSourcePreparer(tmp_path / "cache", command_runner=docker)

    with pytest.raises(SWEbenchPreparationError) as raised:
        preparer.prepare(SWEbenchTaskSource("owner__repo-2", "example/project:latest", base))

    assert raised.value.stage == "git_admission"
    assert "generated.txt" in raised.value.detail
    assert ("docker", "rm", "-f", "container-123") in docker.calls
    assert list((tmp_path / "cache" / "staging").iterdir()) == []


def test_rejects_dataset_base_outside_prepared_history(tmp_path: Path) -> None:
    official, _, _ = prepared_repo(tmp_path)
    other = tmp_path / "other"
    shutil.copytree(official, other)
    (other / "other.txt").write_text("other\n", encoding="utf-8")
    git(other, "add", "other.txt")
    git(other, "commit", "-m", "other")
    unrelated = git(other, "rev-parse", "HEAD")
    docker = FakeDocker(official)

    with pytest.raises(SWEbenchPreparationError) as raised:
        SWEbenchSourcePreparer(tmp_path / "cache", command_runner=docker).prepare(
            SWEbenchTaskSource("owner__repo-3", "example/project:latest", unrelated)
        )

    assert raised.value.stage == "git_admission"


def test_rejects_tampered_cached_source(tmp_path: Path) -> None:
    official, base, _ = prepared_repo(tmp_path)
    docker = FakeDocker(official)
    preparer = SWEbenchSourcePreparer(tmp_path / "cache", command_runner=docker)
    task = SWEbenchTaskSource("owner__repo-4", "example/project:latest", base)
    prepared = preparer.prepare(task)
    (prepared.source_root / "module.py").write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(SWEbenchPreparationError) as raised:
        preparer.prepare(task)

    assert raised.value.stage == "git_admission"


def test_accepts_usable_snapshot_with_broken_unrelated_remote_ref(tmp_path: Path) -> None:
    official, base, prepared_head = prepared_repo(tmp_path)
    remote_refs = official / ".git" / "refs" / "remotes" / "origin"
    remote_refs.mkdir(parents=True)
    (remote_refs / "HEAD").write_text("0" * 40 + "\n", encoding="ascii")

    prepared = SWEbenchSourcePreparer(
        tmp_path / "cache", command_runner=FakeDocker(official)
    ).prepare(SWEbenchTaskSource("owner__legacy-1", "example/project:latest", base))

    assert prepared.prepared_head == prepared_head
    assert git(prepared.source_root, "status", "--porcelain", "--untracked-files=all") == ""


def test_image_reference_is_pinned_by_digest() -> None:
    assert _replace_tag_with_digest("registry:5000/team/image:latest", DIGEST) == (
        f"registry:5000/team/image@{DIGEST}"
    )
    assert _replace_tag_with_digest(f"registry/team/image@{DIGEST}", DIGEST) == (
        f"registry/team/image@{DIGEST}"
    )


def test_missing_docker_is_classified_as_preparation_failure(tmp_path: Path) -> None:
    def missing_docker(argv: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("docker")

    with pytest.raises(SWEbenchPreparationError) as raised:
        SWEbenchSourcePreparer(tmp_path / "cache", command_runner=missing_docker).prepare(
            SWEbenchTaskSource("owner__repo-5", "example/project:latest", "a" * 40)
        )

    assert raised.value.stage == "image_pull"
    assert "Docker 调用失败" in raised.value.detail
