import subprocess
import sys

import pytest

from codeagent.product.demo import create_demo


def test_product_demo_creates_committed_offline_failing_fixture(tmp_path):
    root = create_demo(tmp_path / "demo")

    status = subprocess.run(["git", "status", "--porcelain"], cwd=root, text=True, capture_output=True, check=True)
    test = subprocess.run([sys.executable, "verify.py"], cwd=root, text=True, capture_output=True, check=False)

    assert status.stdout == ""
    assert test.returncode == 1
    assert "ZeroDivisionError" in test.stderr


def test_product_demo_refuses_nonempty_directory(tmp_path):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "keep.txt").write_text("user data", encoding="utf-8")

    with pytest.raises(RuntimeError, match="must be empty"):
        create_demo(root)

    assert (root / "keep.txt").read_text(encoding="utf-8") == "user data"


def test_environment_network_demo_is_separate_and_missing_declared_dependency(tmp_path):
    root = create_demo(tmp_path / "environment-demo", scenario="environment-network")

    result = subprocess.run([sys.executable, "verify.py"], cwd=root, text=True, capture_output=True, check=False)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root, text=True, capture_output=True, check=True)

    assert result.returncode != 0
    assert "humanize" in result.stderr
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "Do not modify, vendor, or reimplement" in readme
    assert ".project-env/bin/python verify.py" in readme
    assert status.stdout == ""
