from __future__ import annotations

import hashlib
import json
import math
import random
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from codeagent.benchmark.manifest import file_sha256, write_json_atomic
from codeagent.benchmark.swebench_preflight import SWEbenchGoldPreflight, SWEbenchTaskRepository
from codeagent.benchmark.swebench_source import SWEbenchPreparationError, SWEbenchSourcePreparer, SWEbenchTaskSource


DIFFICULTY = {
    "<15 min fix": "easy",
    "15 min - 1 hour": "medium",
    "1-4 hours": "hard",
    ">4 hours": "hard",
}
TIERS = ("easy", "medium", "hard")
FORBIDDEN_SELECTION_KEYS = {"patch", "gold_patch", "test_patch", "oracle", "hints_text", "eval_script"}


class SelectionGenerationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candidate:
    instance_id: str
    repo: str
    difficulty: str
    official_difficulty: str
    image: str
    base_commit: str


@dataclass(frozen=True)
class PreflightEvidence:
    status: str
    passed: bool
    failure_stage: str | None
    failure_reason: str | None
    artifact_path: str | None
    image_identity: str | None
    prepared_source: dict[str, str] | None


class CandidatePreflight(Protocol):
    def run(self, candidate: Candidate, artifact_root: Path) -> PreflightEvidence: ...
    def validate_reuse(self, candidate: Candidate, evidence: PreflightEvidence) -> bool: ...


class OfficialCandidatePreflight:
    """组合现有 source preparation 与 official gold preflight，不实现第二套 grader。"""

    def __init__(self, preparer: SWEbenchSourcePreparer, gold: SWEbenchGoldPreflight,
                 artifact_root: str | Path) -> None:
        self.preparer, self.gold, self.artifact_root = preparer, gold, Path(artifact_root).resolve()

    def run(self, candidate: Candidate, artifact_root: Path) -> PreflightEvidence:
        try:
            prepared = self._prepare(candidate)
        except SWEbenchPreparationError as exc:
            stage = "image_unavailable" if exc.stage == "image_pull" else "source_preparation_failed"
            return PreflightEvidence("failed", False, stage, str(exc), None, None, None)
        result = self.gold.run([candidate.instance_id], artifact_root)
        task = result.tasks[0]
        source = _source_identity(prepared)
        if task.status == "gold_passed" and task.resolved is True:
            return PreflightEvidence("passed", True, None, None, result.report_path,
                                     prepared.image_digest, source)
        stage = _official_failure_stage(task.status, task.detail)
        return PreflightEvidence("failed", False, stage, task.detail, result.report_path,
                                 prepared.image_digest, source)

    def validate_reuse(self, candidate: Candidate, evidence: PreflightEvidence) -> bool:
        if not evidence.passed or not evidence.prepared_source:
            return False
        try:
            prepared = self._prepare(candidate)
        except SWEbenchPreparationError:
            return False
        return _source_identity(prepared) == evidence.prepared_source

    def _prepare(self, candidate: Candidate):
        return self.preparer.prepare(SWEbenchTaskSource(
            candidate.instance_id, candidate.image, candidate.base_commit,
        ))


class VerifiedSelectionGenerator:
    REPO_CAP_FRACTION = "0.30"

    def __init__(self, *, candidates: list[Candidate], excluded_ids: set[str], size: int, seed: int,
                 preflight: CandidatePreflight, output_path: str | Path, report_path: str | Path,
                 state_root: str | Path, identity: dict[str, object]) -> None:
        self.all_candidates = sorted(candidates, key=lambda item: item.instance_id)
        self.excluded_ids, self.size, self.seed, self.preflight = excluded_ids, size, seed, preflight
        self.output_path, self.report_path, self.state_root = Path(output_path), Path(report_path), Path(state_root)
        self.identity = identity
        self.repo_cap = max(1, math.floor(size * float(self.REPO_CAP_FRACTION)))

    def run(self) -> dict[str, object]:
        started = datetime.now(timezone.utc).isoformat()
        available_ids = {item.instance_id for item in self.all_candidates}
        missing_exclusions = sorted(self.excluded_ids - available_ids)
        if missing_exclusions:
            raise SelectionGenerationError(f"排除集包含非 Verified candidate: {missing_exclusions}")
        remaining = [item for item in self.all_candidates if item.instance_id not in self.excluded_ids]
        mother_distribution = Counter(item.difficulty for item in self.all_candidates)
        remaining_distribution = Counter(item.difficulty for item in remaining)
        targets = proportional_targets(remaining_distribution, self.size)
        queues = candidate_queues(remaining, self.seed)
        fingerprint = _fingerprint({**self.identity, "excluded_ids": sorted(self.excluded_ids),
                                    "size": self.size, "seed": self.seed,
                                    "repo_cap_fraction": self.REPO_CAP_FRACTION, "targets": targets})
        self.state_root.mkdir(parents=True, exist_ok=True)
        identity_path = self.state_root / "generation-identity.json"
        if identity_path.exists():
            old = json.loads(identity_path.read_text(encoding="utf-8"))
            if old.get("fingerprint") != fingerprint:
                raise SelectionGenerationError("resume generation identity 不兼容")
        else:
            write_json_atomic(identity_path, {"fingerprint": fingerprint, "identity": self.identity,
                                              "targets": targets, "started_at": started})

        accepted: list[dict[str, object]] = []
        attempts: list[dict[str, object]] = []
        repo_counts: Counter[str] = Counter()
        order = 0
        for tier in TIERS:
            tier_accepted = 0
            for candidate in queues[tier]:
                if tier_accepted >= targets[tier]:
                    break
                order += 1
                if repo_counts[candidate.repo] >= self.repo_cap:
                    attempts.append(_attempt(candidate, order, "rejected", "repo_concentration",
                                             f"repo cap={self.repo_cap}", None))
                    self._checkpoint(attempts, accepted, targets, started, fingerprint)
                    continue
                state_path = self.state_root / "candidates" / f"{candidate.instance_id}.json"
                evidence = self._load_reusable(state_path, candidate, fingerprint)
                if evidence is None:
                    write_json_atomic(state_path, {"schema_version": 1, "fingerprint": fingerprint,
                        "candidate": asdict(candidate), "status": "running", "candidate_order": order})
                    try:
                        evidence = self.preflight.run(candidate, self.state_root / "preflight" / candidate.instance_id)
                    except Exception as exc:
                        evidence = PreflightEvidence("failed", False, "other_infrastructure_failed",
                                                     f"{type(exc).__name__}: {exc}", None, None, None)
                    write_json_atomic(state_path, {"schema_version": 1, "fingerprint": fingerprint,
                        "candidate": asdict(candidate), "status": "completed", "candidate_order": order,
                        "evidence": asdict(evidence)})
                attempts.append(_attempt(candidate, order, evidence.status, evidence.failure_stage,
                                         evidence.failure_reason, evidence))
                if evidence.passed:
                    accepted.append(_selection_item(candidate, evidence))
                    repo_counts[candidate.repo] += 1
                    tier_accepted += 1
                self._checkpoint(attempts, accepted, targets, started, fingerprint)
            if tier_accepted != targets[tier]:
                raise SelectionGenerationError(
                    f"{tier} 候选池不足：需要 {targets[tier]}，只通过 {tier_accepted}"
                )
        if len(accepted) != self.size or len({x["instance_id"] for x in accepted}) != self.size:
            raise SelectionGenerationError("最终 selection 数量或唯一性不满足要求")
        selection = self._selection(accepted, targets, mother_distribution, remaining_distribution)
        _assert_no_gold(selection)
        write_json_atomic(self.output_path, selection)
        report = self._report(attempts, accepted, targets, mother_distribution, remaining_distribution,
                              started, datetime.now(timezone.utc).isoformat(), fingerprint, completed=True)
        write_json_atomic(self.report_path, report)
        return report

    def _load_reusable(self, path: Path, candidate: Candidate, fingerprint: str) -> PreflightEvidence | None:
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            evidence = PreflightEvidence(**value["evidence"])
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            return None
        if value.get("status") != "completed" or value.get("fingerprint") != fingerprint:
            return None
        if value.get("candidate") != asdict(candidate):
            raise SelectionGenerationError(f"candidate identity 不兼容: {candidate.instance_id}")
        return evidence if self.preflight.validate_reuse(candidate, evidence) else None

    def _checkpoint(self, attempts, accepted, targets, started, fingerprint):
        report = self._report(attempts, accepted, targets, Counter(item.difficulty for item in self.all_candidates),
                              Counter(item.difficulty for item in self.all_candidates if item.instance_id not in self.excluded_ids),
                              started, None, fingerprint, completed=False)
        write_json_atomic(self.report_path, report)

    def _selection(self, accepted, targets, mother, remaining):
        return {"schema_version": 1, "selection_id": self.identity["selection_id"],
            "dataset": "SWE-bench_Verified", "dataset_identity": self.identity["dataset_identity"],
            "excluded_selection": self.identity["excluded_selection"],
            "excluded_instance_ids": sorted(self.excluded_ids), "seed": self.seed,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "difficulty_distribution": {"mother": dict(mother), "after_exclusion": dict(remaining),
                                        "selected": dict(Counter(x["difficulty"] for x in accepted)),
                                        "targets": targets},
            "repo_distribution": dict(sorted(Counter(x["repo"] for x in accepted).items())),
            "repo_concentration_rule": {"max_fraction": self.REPO_CAP_FRACTION,
                                         "max_instances": self.repo_cap},
            "preflight_policy": "prepared official image + official gold grader; resolved only",
            "tasks": accepted}

    def _report(self, attempts, accepted, targets, mother, remaining, started, finished, fingerprint, completed):
        failures = Counter(str(x.get("failure_stage")) for x in attempts if x.get("status") != "passed")
        return {"schema_version": 1, "selection_id": self.identity["selection_id"],
            "generation_fingerprint": fingerprint, "completed": completed, "started_at": started,
            "finished_at": finished, "runtime": self.identity["runtime"],
            "dataset_identity": self.identity["dataset_identity"], "seed": self.seed, "size": self.size,
            "mother_candidate_count": len(self.all_candidates),
            "after_exclusion_count": len(self.all_candidates) - len(self.excluded_ids),
            "candidate_distribution": dict(mother), "after_exclusion_distribution": dict(remaining),
            "difficulty_targets": targets, "excluded_instance_ids": sorted(self.excluded_ids),
            "attempted_preflight": sum(x.get("preflight") is not None for x in attempts),
            "preflight_passed": sum(x.get("status") == "passed" for x in attempts),
            "preflight_failed": sum(x.get("preflight") is not None and x.get("status") != "passed" for x in attempts),
            "replacement_candidates": max(0, len(attempts) - len(accepted)),
            "failure_stage_distribution": dict(sorted(failures.items())),
            "repo_check": {"max_fraction": self.REPO_CAP_FRACTION, "max_instances": self.repo_cap,
                           "selected_distribution": dict(sorted(Counter(x["repo"] for x in accepted).items()))},
            "accepted_count": len(accepted), "accepted_instance_ids": [x["instance_id"] for x in accepted],
            "attempts": attempts}


def candidates_from_repository(repository: SWEbenchTaskRepository) -> list[Candidate]:
    result = []
    for metadata in repository.list_metadata():
        official = str(metadata.get("difficulty", ""))
        if metadata.get("split") != "test" or official not in DIFFICULTY:
            continue
        result.append(Candidate(str(metadata["instance_id"]), str(metadata["repo"]), DIFFICULTY[official],
                                official, str(metadata["image"]), str(metadata["base_commit"])))
    return result


def proportional_targets(distribution: Counter[str], size: int) -> dict[str, int]:
    total = sum(distribution.values())
    if total < size or any(distribution.get(tier, 0) == 0 for tier in TIERS):
        raise SelectionGenerationError("difficulty 候选池不足或缺少层级")
    exact = {tier: size * distribution[tier] / total for tier in TIERS}
    targets = {tier: math.floor(exact[tier]) for tier in TIERS}
    for tier in sorted(TIERS, key=lambda x: (-(exact[x] - targets[x]), TIERS.index(x)))[:size - sum(targets.values())]:
        targets[tier] += 1
    return targets


def candidate_queues(candidates: list[Candidate], seed: int) -> dict[str, list[Candidate]]:
    queues = {tier: sorted((x for x in candidates if x.difficulty == tier), key=lambda x: x.instance_id)
              for tier in TIERS}
    for tier in TIERS:
        derived = int(hashlib.sha256(f"{seed}:{tier}".encode()).hexdigest(), 16)
        random.Random(derived).shuffle(queues[tier])
    return queues


def runtime_identity(root: str | Path) -> dict[str, object]:
    root = Path(root).resolve()
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                                text=True, check=True, timeout=10).stdout.strip()
        diff = subprocess.run(["git", "diff", "HEAD", "--binary"], cwd=root, capture_output=True,
                              check=True, timeout=30).stdout
        return {"git_commit": commit, "dirty": bool(diff), "tracked_diff_sha256": hashlib.sha256(diff).hexdigest()}
    except (OSError, subprocess.SubprocessError):
        return {"git_commit": None, "dirty": None, "tracked_diff_sha256": None}


def _selection_item(candidate: Candidate, evidence: PreflightEvidence) -> dict[str, object]:
    return {"instance_id": candidate.instance_id, "repo": candidate.repo,
            "difficulty": candidate.difficulty, "official_difficulty": candidate.official_difficulty,
            "image_digest": evidence.image_identity, "prepared_source": evidence.prepared_source,
            "gold_preflight": "passed", "preflight_evidence": evidence.artifact_path}


def _attempt(candidate, order, status, stage, reason, evidence):
    return {"instance_id": candidate.instance_id, "difficulty": candidate.difficulty,
            "repo": candidate.repo, "candidate_order": order, "status": status,
            "failure_stage": stage, "failure_reason": reason,
            "image_identity": evidence.image_identity if evidence else None,
            "prepared_source": evidence.prepared_source if evidence else None,
            "preflight": asdict(evidence) if evidence else None}


def _source_identity(prepared) -> dict[str, str]:
    return {"image_digest": prepared.image_digest, "dataset_base_commit": prepared.dataset_base_commit,
            "prepared_head": prepared.prepared_head, "prepared_tree": prepared.prepared_tree}


def _official_failure_stage(status: str, detail: str) -> str:
    lower = detail.lower()
    if "timeout" in lower or "timed out" in lower:
        return "timeout"
    if status == "gold_failed":
        return "gold_unresolved"
    if "report" in lower:
        return "grader_report_invalid"
    if "container" in lower or "docker" in lower:
        return "command_or_container_failed"
    if "apply" in lower and "patch" in lower:
        return "gold_or_test_patch_apply_failed"
    return "grader_failed"


def _assert_no_gold(selection: dict[str, object]) -> None:
    serialized = json.dumps(selection, ensure_ascii=False)
    for key in FORBIDDEN_SELECTION_KEYS:
        if f'"{key}"' in serialized:
            raise SelectionGenerationError(f"Agent-facing selection 包含禁止字段: {key}")


def _fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
