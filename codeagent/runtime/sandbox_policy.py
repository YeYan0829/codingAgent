from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from codeagent.runtime.permissions import (
    Capability, EffectiveSandboxPolicy, NetworkMode, PermissionDecision,
    PermissionGrant, PermissionRequest, PermissionScope, ResourceIdentity,
)
from codeagent.session.store import SessionStore
from codeagent.workspace.workspace import WorkspaceContext


POLICY_REVISION = "controlled-arbitrary-command-v1"


@dataclass(frozen=True)
class PermissionEvaluation:
    request: PermissionRequest
    decision: PermissionDecision
    reason: str


class SandboxPolicy:
    def __init__(self, context: WorkspaceContext, store: SessionStore, runtime_cache: Path) -> None:
        self.context = context
        self.store = store
        self.runtime_cache = runtime_cache.resolve()
        self.hard_deny_paths = _hard_deny_paths(store)
        self.protected_readonly_paths = _protected_git_paths(context)

    def evaluate(self, request: PermissionRequest, session_grants: tuple[PermissionGrant, ...]) -> PermissionEvaluation:
        if _covered(request, session_grants):
            return PermissionEvaluation(request, PermissionDecision.ALLOW, "已有 Session grant 覆盖")
        if request.capability == Capability.NETWORK:
            return PermissionEvaluation(request, PermissionDecision.ASK, "访问 WSL host network 需要批准")
        path = request.resolved_resource
        if path is None:
            return PermissionEvaluation(request, PermissionDecision.DENY, "filesystem capability 缺少 resource")
        if any(_overlap(path, denied) for denied in self.hard_deny_paths):
            return PermissionEvaluation(request, PermissionDecision.DENY, "resource 位于 hard deny boundary")
        if request.capability == Capability.FILESYSTEM_READ:
            return PermissionEvaluation(request, PermissionDecision.ALLOW, "host root 默认只读可见")
        if _inside(path, self.context.source_root) or any(_overlap(path, item) for item in self.protected_readonly_paths):
            return PermissionEvaluation(request, PermissionDecision.DENY, "source/Git metadata 不允许写")
        if _inside(path, self.context.active_root) or _inside(path, self.runtime_cache):
            return PermissionEvaluation(request, PermissionDecision.ALLOW, "Candidate/runtime cache 默认可写")
        return PermissionEvaluation(request, PermissionDecision.ASK, "Candidate 外 host write 需要批准")

    def grant(self, request: PermissionRequest, approval_id: str | None = None) -> PermissionGrant:
        return PermissionGrant(
            request.capability, request.scope, request.resolved_resource,
            request.resource_identity, request.resource_kind, approval_id,
        )

    def effective(
        self, session_grants: tuple[PermissionGrant, ...], once_grants: tuple[PermissionGrant, ...],
    ) -> EffectiveSandboxPolicy:
        grants = (*session_grants, *once_grants)
        reads = tuple(sorted({g.resolved_resource for g in grants if g.resolved_resource and g.capability == Capability.FILESYSTEM_READ}, key=str))
        writes = tuple(sorted({g.resolved_resource for g in grants if g.resolved_resource and g.capability == Capability.FILESYSTEM_WRITE}, key=str))
        network = NetworkMode.HOST if any(g.capability == Capability.NETWORK for g in grants) else NetworkMode.OFF
        return EffectiveSandboxPolicy(
            POLICY_REVISION, self.context.active_root, self.context.source_root,
            self.runtime_cache, reads, writes, self.hard_deny_paths, network,
            self.protected_readonly_paths,
        )


def _hard_deny_paths(store: SessionStore) -> tuple[Path, ...]:
    home = Path.home().resolve()
    candidates = [home / ".ssh", home / ".aws", home / ".gnupg", store.session_dir]
    runtime_dir = Path(f"/run/user/{os.getuid()}")
    candidates.extend((runtime_dir, Path("/run/dbus"), Path("/run/docker.sock"), Path("/var/run/docker.sock"), Path("/run/containerd/containerd.sock")))
    return tuple(sorted({path.resolve(strict=False) for path in candidates if path.exists()}, key=str))


def _protected_git_paths(context: WorkspaceContext) -> tuple[Path, ...]:
    values = {context.active_root / ".git"}
    for flag in ("--absolute-git-dir", "--git-common-dir"):
        try:
            proc = subprocess.run(
                ["git", "rev-parse", flag], cwd=context.active_root, text=True,
                capture_output=True, shell=False, check=False, timeout=5,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                path = Path(proc.stdout.strip())
                values.add((context.active_root / path).resolve() if not path.is_absolute() else path.resolve())
        except (OSError, subprocess.SubprocessError):
            continue
    return tuple(sorted((path.resolve(strict=False) for path in values if path.exists()), key=str))


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _overlap(left: Path, right: Path) -> bool:
    return _inside(left, right) or _inside(right, left)


def _covered(request: PermissionRequest, grants: tuple[PermissionGrant, ...]) -> bool:
    for grant in grants:
        if request.capability == Capability.NETWORK and grant.capability == Capability.NETWORK:
            return True
        if request.resolved_resource is None or grant.resolved_resource is None:
            continue
        capability_ok = grant.capability == request.capability or (
            grant.capability == Capability.FILESYSTEM_WRITE and request.capability == Capability.FILESYSTEM_READ
        )
        if not capability_ok:
            continue
        if grant.resource_identity and request.resource_identity and not _same_resource(
            grant.resource_identity, request.resource_identity
        ):
            continue
        if grant.resource_kind == "directory" and _inside(request.resolved_resource, grant.resolved_resource):
            return True
        if grant.resolved_resource == request.resolved_resource:
            return True
    return False


def _same_resource(left: ResourceIdentity, right: ResourceIdentity) -> bool:
    """Grant 绑定 filesystem object，而不是可由获批命令合法改变的内容元数据。"""
    return (left.device, left.inode, left.mode) == (right.device, right.inode, right.mode)
