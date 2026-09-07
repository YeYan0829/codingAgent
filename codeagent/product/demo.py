from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


ENVIRONMENT_NETWORK_PROMPT = (
    "Do not modify, vendor, or reimplement any tracked source or dependency. Create an isolated environment "
    "under .project-env, install the exact dependencies declared in requirements.txt, and run "
    ".project-env/bin/python verify.py. Request network permission with once scope when required. "
    "If permission is rejected, stop and report that the exact verification could not run successfully."
)


FILES = {
    "calculator.py": "def divide(total, count):\n    return total / count\n",
    "test_calculator.py": (
        "from calculator import divide\n\n\n"
        "def test_divide():\n    assert divide(8, 2) == 4\n\n\n"
        "def test_zero_count_returns_zero():\n    assert divide(8, 0) == 0\n"
    ),
    "verify.py": (
        "from calculator import divide\n\n\n"
        "assert divide(8, 2) == 4\n"
        "assert divide(8, 0) == 0\n"
        "print('focused verification passed')\n"
    ),
    "README.md": (
        "# CodeAgent Product Demo\n\n"
        "Fix the failing zero-count behavior and run the repository's offline focused verification.\n\n"
        "Run `python3 verify.py`; it requires no third-party packages or network.\n"
    ),
}

ENVIRONMENT_NETWORK_FILES = {
    ".gitignore": ".project-env/\n__pycache__/\n",
    "requirements.txt": "humanize==4.13.0\n",
    "report.py": (
        "import humanize\n\n\n"
        "def file_count(value):\n"
        "    return humanize.intcomma(value)\n"
    ),
    "verify.py": (
        "from report import file_count\n\n\n"
        "assert file_count(12000) == '12,000'\n"
        "print('target verification passed')\n"
    ),
    "README.md": (
        "# CodeAgent Environment / Network Dogfood\n\n"
        + ENVIRONMENT_NETWORK_PROMPT
        + "\n"
    ),
}


def create_demo(root: Path, *, scenario: str = "offline") -> Path:
    root = root.expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise RuntimeError(f"demo directory must be empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    files = FILES if scenario == "offline" else ENVIRONMENT_NETWORK_FILES
    for relative, content in files.items():
        (root / relative).write_text(content, encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.email", "demo@codeagent.invalid")
    _git(root, "config", "user.name", "CodeAgent Demo")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "demo baseline")
    return root


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, timeout=20)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a safe CodeAgent product acceptance demo repository")
    parser.add_argument("--root", type=Path, default=Path("/tmp/codeagent-product-demo"))
    parser.add_argument("--scenario", choices=("offline", "environment-network"), default="offline")
    args = parser.parse_args()
    try:
        root = create_demo(args.root, scenario=args.scenario)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    print(root)
    print('Open this directory in the Extension Development Host and ask:')
    if args.scenario == "offline":
        print('"Fix the zero-count behavior, run the repository focused verification, and summarize the change."')
    else:
        print(f'"{ENVIRONMENT_NETWORK_PROMPT}"')


if __name__ == "__main__":
    main()
