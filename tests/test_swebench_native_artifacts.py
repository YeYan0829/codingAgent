"""基线扩展随隔离Candidate可用，但普通ignored内容和链接不被复制。"""
import hashlib
from dataclasses import replace
import subprocess

import pytest

from codeagent.benchmark.swebench_native_artifacts import seed_prepared_native_artifacts
from codeagent.workspace.workspace import WorkspaceContext


@pytest.fixture
def prepared_candidate(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for args in (["init", "-q"], ["config", "user.name", "C4"], ["config", "user.email", "c4@example.invalid"]):
        subprocess.run(["git", *args], cwd=source, check=True)
    (source / ".gitignore").write_text("*.so\n*.so.*\n.env\nbuild/\n")
    (source / "app.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=source, check=True)
    (source / "build").mkdir()
    (source / "build/native.so").write_bytes(b"\x7fELF\x00baseline")
    (source / "build/generated.c").write_text("not an extension\n")
    (source / ".env").write_text("PRIVATE=baseline\n")
    active = tmp_path / "active"
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(source), str(active)], check=True)
    yield WorkspaceContext(source, active)


def test_seed_native_dependencies_without_source_edits_or_ignored_data(prepared_candidate):
    ctx = prepared_candidate
    before = (ctx.source_root / "build/native.so").read_bytes()
    result = seed_prepared_native_artifacts(ctx)
    assert (ctx.active_root / "build/native.so").read_bytes() == before
    assert result["copied"] == [{"path": "build/native.so", "size_bytes": len(before),
                                "sha256": hashlib.sha256(before).hexdigest()}]
    assert not (ctx.active_root / ".env").exists()
    assert not (ctx.active_root / "build/generated.c").exists()
    assert (ctx.source_root / "build/native.so").read_bytes() == before
    for root in (ctx.source_root, ctx.active_root):
        assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=root)


def test_existing_candidate_extension_is_never_overwritten(prepared_candidate):
    ctx = prepared_candidate
    (ctx.active_root / "build").mkdir()
    (ctx.active_root / "build/native.so").write_bytes(b"new build")
    result = seed_prepared_native_artifacts(ctx)
    assert not result["copied"]
    assert (ctx.active_root / "build/native.so").read_bytes() == b"new build"


def test_native_artifacts_do_not_follow_links(prepared_candidate, tmp_path):
    ctx = prepared_candidate
    outside = tmp_path / "outside"
    outside.mkdir()
    (ctx.source_root / "external.so").symlink_to(outside / "outside.so")
    result = seed_prepared_native_artifacts(ctx)
    assert not (ctx.active_root / "external.so").exists()
    assert any(x["path"] == "external.so" and x["reason"] == "not_regular_file" for x in result["skipped"])
    (ctx.active_root / "build/native.so").unlink()
    (ctx.active_root / "build").rmdir()
    (ctx.active_root / "build").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="父路径"):
        seed_prepared_native_artifacts(ctx)
    assert not (outside / "native.so").exists()


def test_source_only_workspace_cannot_seed_artifacts(prepared_candidate):
    with pytest.raises(ValueError, match="输入缓存"):
        seed_prepared_native_artifacts(WorkspaceContext.source(prepared_candidate.source_root))


def test_denied_repository_paths_are_not_seeded(prepared_candidate):
    ctx = replace(prepared_candidate, denied_paths=("build",))
    result = seed_prepared_native_artifacts(ctx)
    assert not result["copied"]
    assert not (ctx.active_root / "build/native.so").exists()
    assert any(x["reason"] == "repository_access" for x in result["skipped"])
