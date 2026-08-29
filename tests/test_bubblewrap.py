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
        (value.candidate_root, value.runtime_cache), key=lambda item: (len(item.parts), str(item)),
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
    off_args = BubblewrapBackend.invocation(features, MountPlan.build(off, mask, empty), off, off.candidate_root, {}, ["/bin/true"])
    host_args = BubblewrapBackend.invocation(features, MountPlan.build(host, mask, empty), host, host.candidate_root, {}, ["/bin/true"])
    assert "--unshare-net" in off_args
    assert "--unshare-net" not in host_args


def test_missing_bwrap_fails_closed(tmp_path):
    with pytest.raises(SandboxUnavailable):
        BubblewrapBackend(tmp_path / "missing").probe(tmp_path)
