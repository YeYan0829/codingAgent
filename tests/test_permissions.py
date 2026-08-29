from pathlib import Path

import pytest

from codeagent.runtime.permissions import (
    Capability, PermissionError, PermissionScope, parse_permission_request, recheck_request,
)


def test_network_has_no_resource(tmp_path):
    request = parse_permission_request(
        {"capability": "network", "reason": "download", "scope": "once"}, tmp_path,
    )
    assert request.capability == Capability.NETWORK
    assert request.resolved_resource is None
    with pytest.raises(PermissionError):
        parse_permission_request(
            {"capability": "network", "resource": "/tmp", "reason": "x", "scope": "once"}, tmp_path,
        )


def test_relative_file_and_symlink_bind_to_canonical_target(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("x")
    (tmp_path / "alias").symlink_to(target)
    request = parse_permission_request(
        {"capability": "filesystem_read", "resource": "alias", "reason": "read", "scope": "session"}, tmp_path,
    )
    assert request.requested_resource == "alias"
    assert request.resolved_resource == target
    assert request.resource_kind == "file"
    assert request.scope == PermissionScope.SESSION
    recheck_request(request, tmp_path)


def test_nonexistent_write_expands_to_existing_parent(tmp_path):
    parent = tmp_path / "cache"
    parent.mkdir()
    request = parse_permission_request(
        {"capability": "filesystem_write", "resource": "cache/new/deep/file", "reason": "create", "scope": "once"}, tmp_path,
    )
    assert request.resolved_resource == parent
    assert request.resource_kind == "directory"
    assert request.parent_expanded


def test_nonexistent_read_and_hard_deny_rejected(tmp_path):
    with pytest.raises(PermissionError) as missing:
        parse_permission_request(
            {"capability": "filesystem_read", "resource": "missing", "reason": "read", "scope": "once"}, tmp_path,
        )
    assert missing.value.code == "permission_resource_not_found"
    denied = tmp_path / "secret"
    denied.mkdir()
    with pytest.raises(PermissionError) as blocked:
        parse_permission_request(
            {"capability": "filesystem_write", "resource": str(denied), "reason": "write", "scope": "once"},
            tmp_path, (denied,),
        )
    assert blocked.value.code == "permission_denied"


def test_identity_change_after_approval_is_rejected(tmp_path):
    target = tmp_path / "file"
    target.write_text("before")
    request = parse_permission_request(
        {"capability": "filesystem_read", "resource": "file", "reason": "read", "scope": "once"}, tmp_path,
    )
    target.unlink()
    target.write_text("after")
    with pytest.raises(PermissionError) as changed:
        recheck_request(request, tmp_path)
    assert changed.value.code == "permission_resource_changed"
