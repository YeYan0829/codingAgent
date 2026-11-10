from pathlib import Path

import pytest

from codeagent.runtime.bubblewrap import BubblewrapBackend, BubblewrapFeatures, MountPlan, SandboxUnavailable
from codeagent.runtime.permissions import EffectiveSandboxPolicy, NetworkMode


def policy(tmp_path, *, network=NetworkMode.OFF):
    candidate = tmp_path / "candidate"
    source = tmp_path / "source"
    cache = tmp_path / "cache"
    secret = tmp_path / "secret"
    for path in (candidate, source, cache, secret):
        path.mkdir()
    return EffectiveSandboxPolicy("v1", candidate, source, cache, (), (), (secret,), network)


def test_mount_plan_is_deterministic_and_reprotects_last(tmp_path):
    value = policy(tmp_path)
    mask_dir = tmp_path / "mask"
    mask_dir.mkdir()
    mask_file = tmp_path / "empty"
    mask_file.touch()
    plan = MountPlan.build(value, mask_dir, mask_file)
    assert plan.operations[0].mode == "ro_bind"
    assert plan.operations[0].destination == Path("/")
    assert [op.destination for op in plan.operations if op.mode == "bind"] == sorted(
        (value.workspace_root, value.runtime_cache), key=lambda item: (len(item.parts), str(item)),
    )
    assert plan.operations[-2].destination == value.source_root
    assert plan.operations[-1].destination == value.hard_deny_paths[0]
    assert plan.operations[-1].mode == "mask"


def test_network_translation_only_changes_unshare_net(tmp_path):
    off = policy(tmp_path, network=NetworkMode.OFF)
    host = EffectiveSandboxPolicy(**{**off.__dict__, "network_mode": NetworkMode.HOST})
    mask = tmp_path / "mask"
    mask.mkdir()
    empty = tmp_path / "empty"
    empty.touch()
    features = BubblewrapFeatures(Path("/usr/bin/bwrap"), "test", True)
    off_args = BubblewrapBackend.invocation(features, MountPlan.build(off, mask, empty), off, off.workspace_root, {}, ["/bin/true"])
    host_args = BubblewrapBackend.invocation(features, MountPlan.build(host, mask, empty), host, host.workspace_root, {}, ["/bin/true"])
    assert "--unshare-net" in off_args
    assert "--unshare-net" not in host_args


def test_missing_bwrap_fails_closed(tmp_path):
    with pytest.raises(SandboxUnavailable):
        BubblewrapBackend(tmp_path / "missing").probe(tmp_path)


def test_overlapping_masks_keep_ancestor_and_unrelated_siblings(tmp_path):
    from dataclasses import replace
    value = policy(tmp_path)
    secret = value.hard_deny_paths[0]
    (secret / ".git").mkdir()
    (secret / "answer").write_text("hidden")
    sibling = tmp_path / "secret-extra"
    sibling.write_text("also hidden")
    alias = tmp_path / "alias"
    alias.symlink_to(secret, target_is_directory=True)
    denied = (secret / ".git", sibling, secret / "answer", alias, secret)
    value = replace(value, hard_deny_paths=denied)
    mask = tmp_path / "mask"
    mask.mkdir()
    empty = tmp_path / "empty"
    empty.touch()
    plan = MountPlan.build(value, mask, empty)
    assert {op.destination for op in plan.operations if op.mode == "mask"} == {secret, sibling}
    assert value.hard_deny_paths == denied, "audit/permission policy must not lose descendant denies"
    assert plan == MountPlan.build(replace(value, hard_deny_paths=tuple(reversed(denied))), mask, empty)


def test_real_bubblewrap_nested_denial_allows_payload_but_keeps_secrets_hidden(tmp_path):
    import subprocess
    from dataclasses import replace
    value = policy(tmp_path)
    secret = value.hard_deny_paths[0]
    (secret / ".git").mkdir()
    (secret / "answer").write_text("hidden")
    value = replace(value, hard_deny_paths=(secret, secret / ".git", secret / "answer"))
    mask = tmp_path / "mask"
    mask.mkdir()
    empty = tmp_path / "empty"
    empty.touch()
    backend = BubblewrapBackend()
    features = backend.probe(value.workspace_root)
    argv = backend.invocation(features, MountPlan.build(value, mask, empty), value, value.workspace_root, {},
        ["/bin/sh", "-c", 'test ! -e "$1/answer" && test ! -e "$1/.git" && printf ok > probe && cat probe',
         "sh", str(secret)])
    result = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0 and result.stdout == "ok", result.stderr
    assert (value.workspace_root / "probe").read_text() == "ok"
    assert (secret / "answer").read_text() == "hidden"
