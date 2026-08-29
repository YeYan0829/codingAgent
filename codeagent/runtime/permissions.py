from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class Capability(StrEnum):
    FILESYSTEM_READ = "filesystem_read"
    FILESYSTEM_WRITE = "filesystem_write"
    NETWORK = "network"


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class PermissionScope(StrEnum):
    ONCE = "once"
    SESSION = "session"


class NetworkMode(StrEnum):
    OFF = "off"
    HOST = "host"


class PermissionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ResourceIdentity:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int

    @classmethod
    def from_path(cls, path: Path) -> "ResourceIdentity":
        stat = path.stat()
        return cls(stat.st_dev, stat.st_ino, stat.st_mode, stat.st_size, stat.st_mtime_ns)


@dataclass(frozen=True)
class PermissionRequest:
    capability: Capability
    reason: str
    scope: PermissionScope
    requested_resource: str | None = None
    resolved_resource: Path | None = None
    resource_identity: ResourceIdentity | None = None
    resource_kind: str | None = None
    parent_expanded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability.value,
            "reason": self.reason,
            "scope": self.scope.value,
            "requested_resource": self.requested_resource,
            "resolved_resource": str(self.resolved_resource) if self.resolved_resource else None,
            "resource_kind": self.resource_kind,
            "parent_expanded": self.parent_expanded,
        }


@dataclass(frozen=True)
class PermissionGrant:
    capability: Capability
    scope: PermissionScope
    resolved_resource: Path | None
    resource_identity: ResourceIdentity | None
    resource_kind: str | None
    approval_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability.value,
            "scope": self.scope.value,
            "resolved_resource": str(self.resolved_resource) if self.resolved_resource else None,
            "resource_identity": (
                {
                    "device": self.resource_identity.device,
                    "inode": self.resource_identity.inode,
                    "mode": self.resource_identity.mode,
                    "size": self.resource_identity.size,
                    "mtime_ns": self.resource_identity.mtime_ns,
                }
                if self.resource_identity else None
            ),
            "resource_kind": self.resource_kind,
            "approval_id": self.approval_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PermissionGrant":
        raw_identity = value.get("resource_identity")
        identity = ResourceIdentity(**raw_identity) if isinstance(raw_identity, dict) else None
        raw_resource = value.get("resolved_resource")
        return cls(
            Capability(value["capability"]), PermissionScope(value["scope"]),
            Path(raw_resource) if raw_resource else None, identity,
            value.get("resource_kind"), value.get("approval_id"),
        )


@dataclass(frozen=True)
class EffectiveSandboxPolicy:
    policy_revision: str
    candidate_root: Path
    source_root: Path
    runtime_cache: Path
    read_grants: tuple[Path, ...]
    write_grants: tuple[Path, ...]
    hard_deny_paths: tuple[Path, ...]
    network_mode: NetworkMode
    protected_readonly_paths: tuple[Path, ...] = ()

    def summary(self) -> dict[str, Any]:
        return {
            "policy_revision": self.policy_revision,
            "read_grants": [str(path) for path in self.read_grants],
            "write_grants": [str(path) for path in self.write_grants],
            "hard_deny_paths": [str(path) for path in self.hard_deny_paths],
            "protected_readonly_paths": [str(path) for path in self.protected_readonly_paths],
            "network_mode": self.network_mode.value,
        }


def parse_permission_request(
    raw: dict[str, Any], candidate_root: Path, hard_deny_paths: tuple[Path, ...] = (),
) -> PermissionRequest:
    if not isinstance(raw, dict):
        raise PermissionError("invalid_arguments", "permission 必须是 object")
    try:
        capability = Capability(raw.get("capability"))
        scope = PermissionScope(raw.get("scope"))
    except ValueError as exc:
        raise PermissionError("invalid_arguments", "permission capability/scope 无效") from exc
    allowed = {"capability", "reason", "scope"} if capability == Capability.NETWORK else {
        "capability", "resource", "reason", "scope"
    }
    if set(raw) != allowed:
        raise PermissionError("invalid_arguments", "permission 字段不完整或包含未知字段")
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500 or "\x00" in reason:
        raise PermissionError("invalid_arguments", "permission reason 必须是 1..500 字符")
    if capability == Capability.NETWORK:
        return PermissionRequest(capability, reason.strip(), scope)
    resource = raw.get("resource")
    if not isinstance(resource, str) or not resource or len(resource) > 4096 or "\x00" in resource:
        raise PermissionError("invalid_arguments", "filesystem permission resource 无效")
    requested = Path(resource).expanduser()
    requested = requested if requested.is_absolute() else candidate_root / requested
    parent_expanded = False
    try:
        resolved = requested.resolve(strict=True)
    except RuntimeError as exc:
        raise PermissionError("permission_resource_invalid", f"resource symlink loop: {resource}") from exc
    except FileNotFoundError:
        if capability == Capability.FILESYSTEM_READ:
            raise PermissionError("permission_resource_not_found", f"read resource 不存在: {resource}")
        parent = requested.parent
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        try:
            resolved = parent.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PermissionError("permission_resource_invalid", f"write resource parent 无法解析: {resource}") from exc
        if resolved == Path(resolved.anchor):
            raise PermissionError("permission_denied", "不存在 write target 不能扩大授权到 filesystem root")
        parent_expanded = True
    except OSError as exc:
        raise PermissionError("permission_resource_invalid", f"resource 无法解析: {resource}: {exc}") from exc
    for denied in hard_deny_paths:
        denied = denied.resolve(strict=False)
        if _contains(denied, resolved) or _contains(resolved, denied):
            raise PermissionError("permission_denied", f"resource 跨越 hard deny boundary: {resource} -> {resolved}")
    if not resolved.is_file() and not resolved.is_dir():
        raise PermissionError("permission_resource_invalid", "resource 必须是普通文件或目录")
    kind = "directory" if resolved.is_dir() else "file"
    return PermissionRequest(
        capability, reason.strip(), scope, resource, resolved,
        ResourceIdentity.from_path(resolved), kind, parent_expanded,
    )


def recheck_request(request: PermissionRequest, candidate_root: Path) -> None:
    if request.resolved_resource is None or request.resource_identity is None:
        return
    raw = Path(request.requested_resource or "")
    requested = raw.expanduser() if raw.is_absolute() else candidate_root / raw
    if request.parent_expanded:
        while not requested.exists() and requested != requested.parent:
            requested = requested.parent
    try:
        resolved = requested.resolve(strict=True)
        identity = ResourceIdentity.from_path(resolved)
    except (OSError, RuntimeError) as exc:
        raise PermissionError("permission_resource_changed", f"approval 后 resource 无法复检: {exc}") from exc
    if resolved != request.resolved_resource or identity != request.resource_identity:
        raise PermissionError("permission_resource_changed", "approval 后 resource identity 已变化")


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False
