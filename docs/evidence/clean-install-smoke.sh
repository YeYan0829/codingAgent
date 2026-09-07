#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: bash clean-install-smoke.sh <repository-url-or-path> <commit> <empty-output-directory>" >&2
  exit 2
fi

repository_source=$1
release_commit=$2
validation_root=$3

if [[ -e "$validation_root" ]]; then
  echo "output directory already exists: $validation_root" >&2
  exit 2
fi

mkdir -p "$validation_root"
validation_root=$(cd "$validation_root" && pwd)
git clone --no-local "$repository_source" "$validation_root/repository"
git -C "$validation_root/repository" checkout --detach "$release_commit"

actual_commit=$(git -C "$validation_root/repository" rev-parse HEAD)
if [[ "$actual_commit" != "$release_commit" ]]; then
  echo "checked out $actual_commit instead of $release_commit" >&2
  exit 1
fi

if [[ -n "$(git -C "$validation_root/repository" status --porcelain --untracked-files=all)" ]]; then
  echo "clean clone unexpectedly contains working-tree changes" >&2
  exit 1
fi

python3 -m venv "$validation_root/source-venv"
env -u PYTHONPATH -u VIRTUAL_ENV -u DEEPSEEK_API_KEY -u GLM_API_KEY \
  "$validation_root/source-venv/bin/pip" install --no-cache-dir \
  -e "$validation_root/repository[dev]"

cd "$validation_root"

env -u PYTHONPATH -u VIRTUAL_ENV \
  "$validation_root/source-venv/bin/python" -c \
  "import sys; from pathlib import Path; import codeagent; expected=Path(sys.argv[1]).resolve(); actual=Path(codeagent.__file__).resolve(); assert actual.is_relative_to(expected), (actual, expected); print(actual)" \
  "$validation_root/repository"

env -u PYTHONPATH -u VIRTUAL_ENV \
  "$validation_root/source-venv/bin/codeagent" --help
env -u PYTHONPATH -u VIRTUAL_ENV \
  "$validation_root/source-venv/bin/codeagent-rpc" --help
env -u PYTHONPATH -u VIRTUAL_ENV -u DEEPSEEK_API_KEY -u GLM_API_KEY \
  "$validation_root/source-venv/bin/codeagent" ask \
  "$validation_root/repository" "只读取 README，并用三句话概括项目" \
  --provider fake --session-root "$validation_root/sessions"

if [[ -n "$(git -C "$validation_root/repository" status --porcelain --untracked-files=all)" ]]; then
  echo "source install or smoke changed tracked/untracked repository files" >&2
  git -C "$validation_root/repository" status --short
  exit 1
fi

echo "clean source install smoke PASS: $actual_commit"
