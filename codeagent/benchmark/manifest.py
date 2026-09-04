from __future__ import annotations

import hashlib
import json
import subprocess
from urllib.parse import urlsplit, urlunsplit
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codeagent import __version__
from codeagent.config import ModelConfig, RuntimeConfig


def file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def config_fingerprint(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def safe_endpoint_identity(value: str) -> str:
    """只保留 endpoint 的 scheme/host/path，拒绝持久化凭据、query 和 fragment。"""
    parts = urlsplit(value)
    if not parts.scheme or not parts.hostname:
        return value.split("?", 1)[0].split("#", 1)[0]
    host = parts.hostname + ((f":{parts.port}") if parts.port else "")
    return urlunsplit((parts.scheme, host, parts.path.rstrip("/"), "", ""))


def git_identity(root: str | Path) -> tuple[str | None, bool | None]:
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                                text=True, check=True, timeout=10).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=root,
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout)
        return commit, dirty
    except (OSError, subprocess.SubprocessError):
        return None, None


def build_run_manifest(*, run_id: str, selection_path: str | Path, selection_id: str,
                       model: ModelConfig, runtime: RuntimeConfig, endpoint_identity: str,
                       swebench_commit: str | None, runtime_root: str | Path) -> dict[str, Any]:
    commit, dirty = git_identity(runtime_root)
    model_value = {
        "provider": model.provider, "model": model.resolved_model,
        "endpoint_identity": safe_endpoint_identity(endpoint_identity), "temperature": model.temperature,
        "max_output_tokens": model.max_tokens, "thinking": "disabled",
    }
    runtime_value = {
        **asdict(runtime), "context_limit": model.context_limit,
        "generation_reserve": model.max_tokens or 4_000,
        "continuation_reserve": 4_000, "safety_margin": 2_000,
        "context_reduction": "deterministic-v2", "minimum_context": "fail_closed",
    }
    fingerprint_input = {"model": model_value, "runtime": runtime_value}
    return {
        "schema_version": 1, "run_id": run_id, "selection_id": selection_id,
        "selection_path": str(Path(selection_path).resolve()),
        "selection_sha256": file_sha256(selection_path),
        "runtime": {"version": __version__, "git_commit": commit, "dirty": dirty, **runtime_value},
        "model": {**model_value, "config_fingerprint": config_fingerprint(fingerprint_input)},
        "benchmark": {"swebench_commit": swebench_commit, "tasks": {}},
        "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None,
    }


def write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
