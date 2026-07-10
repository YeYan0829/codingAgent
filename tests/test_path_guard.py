from pathlib import Path

import pytest

from codeagent.safety.path_guard import PathGuard, PathGuardError


def test_workspace_path_allowed(tmp_path):
    guard = PathGuard(tmp_path)
    inside = tmp_path / "a.txt"
    inside.write_text("ok", encoding="utf-8")

    assert guard.resolve("a.txt") == inside.resolve()


def test_parent_escape_denied(tmp_path):
    guard = PathGuard(tmp_path)

    with pytest.raises(PathGuardError):
        guard.resolve("../outside.txt")


def test_absolute_escape_denied(tmp_path):
    guard = PathGuard(tmp_path)
    outside = tmp_path.parent / "outside.txt"

    with pytest.raises(PathGuardError):
        guard.resolve(outside)


def test_symlink_escape_denied_when_available(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink is not available on this platform")

    with pytest.raises(PathGuardError):
        PathGuard(tmp_path).resolve("link.txt")
