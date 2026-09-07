import json
from collections import Counter
from pathlib import Path

import pytest

from codeagent.benchmark.selection import (
    Candidate, PreflightEvidence, SelectionGenerationError, VerifiedSelectionGenerator,
    candidate_queues, candidates_from_repository, proportional_targets,
)
from codeagent.benchmark.swebench_preflight import SWEbenchTaskRepository


def candidate(index, difficulty, repo=None):
    return Candidate(f"owner__repo-{index:03d}", repo or f"org/repo-{index % 8}", difficulty,
                     {"easy": "<15 min fix", "medium": "15 min - 1 hour", "hard": "1-4 hours"}[difficulty],
                     f"image-{index}:latest", f"{index:040x}")


class FakePreflight:
    def __init__(self, failures=(), invalid_reuse=()):
        self.failures, self.invalid_reuse = set(failures), set(invalid_reuse)
        self.calls, self.reuse_calls = [], []

    def run(self, item, artifact_root):
        self.calls.append(item.instance_id)
        if item.instance_id in self.failures:
            return PreflightEvidence("failed", False, "gold_unresolved", "not resolved",
                                     str(artifact_root / "report.json"), "sha256:bad", None)
        return PreflightEvidence("passed", True, None, None, str(artifact_root / "report.json"),
            "sha256:" + "a" * 64, {"image_digest": "sha256:" + "a" * 64,
            "dataset_base_commit": item.base_commit, "prepared_head": "h", "prepared_tree": "t"})

    def validate_reuse(self, item, evidence):
        self.reuse_calls.append(item.instance_id)
        return item.instance_id not in self.invalid_reuse


def fixture_candidates(counts=(20, 25, 10)):
    result = []
    offset = 0
    for tier, count in zip(("easy", "medium", "hard"), counts):
        result.extend(candidate(offset + i, tier) for i in range(count))
        offset += count
    return result


def generator(tmp_path, preflight, *, candidates=None, excluded=(), size=10, seed=42, identity_extra=None):
    identity = {"selection_id": "eval", "excluded_selection": {"path": "dev.json", "sha256": "x"},
        "dataset_identity": {"suite": "SWE-bench_Verified", "task_commit": "task"},
        "runtime": {"git_commit": "runtime", "tracked_diff_sha256": "diff"},
        "preflight": {"command": ["official"]}}
    identity.update(identity_extra or {})
    return VerifiedSelectionGenerator(candidates=candidates or fixture_candidates(),
        excluded_ids=set(excluded), size=size, seed=seed, preflight=preflight,
        output_path=tmp_path / "selection.json", report_path=tmp_path / "generation.json",
        state_root=tmp_path / "state", identity=identity)


def test_proportional_targets_use_largest_remainder():
    assert proportional_targets(Counter(easy=188, medium=257, hard=45), 50) == {
        "easy": 19, "medium": 26, "hard": 5,
    }


def test_seeded_candidate_queue_is_deterministic_and_seed_sensitive():
    values = fixture_candidates()
    one = [x.instance_id for x in candidate_queues(values, 42)["easy"]]
    two = [x.instance_id for x in candidate_queues(list(reversed(values)), 42)["easy"]]
    three = [x.instance_id for x in candidate_queues(values, 43)["easy"]]
    assert one == two and one != three


def test_exclusion_pass_only_replacement_and_report(tmp_path):
    values = fixture_candidates()
    queues = candidate_queues([x for x in values if x.instance_id != values[0].instance_id], 42)
    failed = queues["easy"][0].instance_id
    fake = FakePreflight({failed})
    report = generator(tmp_path, fake, candidates=values, excluded={values[0].instance_id}).run()
    selection = json.loads((tmp_path / "selection.json").read_text())
    ids = {x["instance_id"] for x in selection["tasks"]}
    assert len(ids) == 10 and values[0].instance_id not in ids and failed not in ids
    assert all(x["gold_preflight"] == "passed" for x in selection["tasks"])
    assert report["preflight_failed"] == 1 and report["replacement_candidates"] == 1
    rejected = next(x for x in report["attempts"] if x["instance_id"] == failed)
    assert rejected["failure_stage"] == "gold_unresolved" and rejected["failure_reason"]


def test_multiple_failures_fill_tier_and_pool_shortage_fails(tmp_path):
    values = fixture_candidates((8, 8, 8))
    first = [x.instance_id for x in candidate_queues(values, 42)["easy"][:3]]
    report = generator(tmp_path, FakePreflight(first), candidates=values, size=6).run()
    assert report["accepted_count"] == 6 and report["preflight_failed"] == 3
    with pytest.raises(SelectionGenerationError, match="候选池不足"):
        generator(tmp_path / "short", FakePreflight({x.instance_id for x in values}),
                  candidates=values, size=6).run()


def test_repo_concentration_rule_is_deterministic(tmp_path):
    values = fixture_candidates((12, 12, 12))
    values = [Candidate(x.instance_id, "dominant/repo" if int(x.instance_id[-3:]) % 2 == 0 else x.repo,
                        x.difficulty, x.official_difficulty, x.image, x.base_commit) for x in values]
    report = generator(tmp_path, FakePreflight(), candidates=values, size=10).run()
    selection = json.loads((tmp_path / "selection.json").read_text())
    assert max(selection["repo_distribution"].values()) <= 3
    assert report["failure_stage_distribution"].get("repo_concentration", 0) > 0


def test_resume_reuses_passed_and_reruns_invalid_identity(tmp_path):
    first = FakePreflight(); generator(tmp_path, first).run()
    second = FakePreflight(); generator(tmp_path, second).run()
    assert second.calls == [] and len(second.reuse_calls) == 10
    invalid = {second.reuse_calls[0]}
    third = FakePreflight(invalid_reuse=invalid); generator(tmp_path, third).run()
    assert third.calls == list(invalid)


def test_generation_identity_change_fails_closed(tmp_path):
    generator(tmp_path, FakePreflight()).run()
    with pytest.raises(SelectionGenerationError, match="identity 不兼容"):
        generator(tmp_path, FakePreflight(), seed=99).run()


def test_unknown_exclusion_and_unexpected_preflight_failure_are_explicit(tmp_path):
    with pytest.raises(SelectionGenerationError, match="非 Verified"):
        generator(tmp_path / "unknown", FakePreflight(), excluded={"missing"}).run()

    class Crashing(FakePreflight):
        def run(self, item, artifact_root):
            if not self.calls:
                self.calls.append(item.instance_id)
                raise RuntimeError("docker vanished")
            return super().run(item, artifact_root)

    report = generator(tmp_path / "crash", Crashing()).run()
    assert report["failure_stage_distribution"]["other_infrastructure_failed"] == 1
    assert "docker vanished" in next(x["failure_reason"] for x in report["attempts"]
                                     if x["failure_stage"] == "other_infrastructure_failed")


def test_selection_has_exactly_fifty_unique_without_gold_content(tmp_path):
    values = fixture_candidates((80, 100, 30))
    report = generator(tmp_path, FakePreflight(), candidates=values, size=50).run()
    selection = json.loads((tmp_path / "selection.json").read_text())
    assert len(selection["tasks"]) == len({x["instance_id"] for x in selection["tasks"]}) == 50
    rendered = json.dumps(selection)
    assert all(f'"{key}"' not in rendered for key in
               ("patch", "gold_patch", "test_patch", "oracle", "hints_text", "eval_script"))
    assert report["accepted_count"] == 50


def test_pinned_task_repository_exposes_exact_verified_mother_distribution():
    root = Path(__file__).parents[1] / "reference" / "swe-bench-tasks"
    if not root.is_dir():
        pytest.skip("固定 task repo 未安装")
    values = candidates_from_repository(SWEbenchTaskRepository(root))
    assert len(values) == 500
    assert Counter(x.difficulty for x in values) == Counter(easy=194, medium=261, hard=45)
