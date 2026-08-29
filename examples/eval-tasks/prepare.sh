#!/usr/bin/env bash
set -euo pipefail

task_id="${1:-}"
workspace_base="${2:-$HOME/codeagent-evals}"
cache_base="${CODEAGENT_EVAL_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/codeagent-evals}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/../.." && pwd)"

case "$task_id" in
  itsdangerous-strict-base64)
    snapshot="itsdangerous"
    dependencies=('pytest==9.1.1' 'freezegun==1.5.5')
    ;;
  click-short-help-sentences)
    snapshot="click"
    dependencies=('pytest==9.1.1')
    ;;
  httpx-no-proxy-cidr)
    snapshot="httpx"
    dependencies=(
      'pytest==9.1.1' 'certifi==2026.7.22' 'httpcore==1.0.9'
      'anyio==4.14.2' 'idna==3.19' 'trio==0.34.0'
      'trustme==1.2.1' 'cryptography==50.0.1' 'uvicorn==0.52.4'
    )
    ;;
  *)
    echo "用法: $0 {itsdangerous-strict-base64|click-short-help-sentences|httpx-no-proxy-cidr} [workspace-base]" >&2
    exit 2
    ;;
esac

source_dir="$repo_root/examples/eval-repos/$snapshot"
workspace="$workspace_base/workspaces/$task_id"
venv_dir="$cache_base/$task_id"

if [[ -e "$workspace" || -e "$venv_dir" ]]; then
  echo "拒绝覆盖已有环境: $workspace 或 $venv_dir" >&2
  exit 1
fi

mkdir -p "$(dirname -- "$workspace")" "$(dirname -- "$venv_dir")"
cp -a -- "$source_dir" "$workspace"
python3 -m venv "$venv_dir"
"$venv_dir/bin/python" -m pip install "${dependencies[@]}"

git -C "$workspace" init
git -C "$workspace" config user.name "CodeAgent Eval"
git -C "$workspace" config user.email "codeagent-eval@example.invalid"
git -C "$workspace" add --all
git -C "$workspace" commit -m "CodeAgent eval baseline: $task_id"

cat <<EOF
环境已准备：
  workspace: $workspace
  venv:      $venv_dir
  task:      $script_dir/tasks/$task_id.md

下一步请阅读任务文档，然后以 workspace 路径启动 codeagent。
EOF
