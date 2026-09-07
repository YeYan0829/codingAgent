from __future__ import annotations

import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREPARE = PROJECT_ROOT / "examples" / "showcase" / "prepare.sh"


def test_showcase_prepare_creates_clean_failing_baseline(tmp_path: Path) -> None:
    workspace = tmp_path / "quick"

    subprocess.run([str(PREPARE), str(workspace)], check=True, capture_output=True, text=True)

    status = subprocess.run(
        ["git", "status", "--short"], cwd=workspace, check=True, capture_output=True, text=True
    )
    verification = subprocess.run(
        ["python3", "verify.py"], cwd=workspace, check=False, capture_output=True, text=True
    )
    assert status.stdout == ""
    assert verification.returncode != 0
    assert "ZeroDivisionError" in verification.stderr
    assert (workspace / "CODEAGENT_TASK.md").is_file()


def test_showcase_prepare_refuses_existing_content(tmp_path: Path) -> None:
    workspace = tmp_path / "quick"
    workspace.mkdir()
    (workspace / "keep.txt").write_text("keep", encoding="utf-8")

    result = subprocess.run(
        [str(PREPARE), str(workspace)], check=False, capture_output=True, text=True
    )

    assert result.returncode == 1
    assert "拒绝覆盖非空路径" in result.stderr
    assert (workspace / "keep.txt").read_text(encoding="utf-8") == "keep"
