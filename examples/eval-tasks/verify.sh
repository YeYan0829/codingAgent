#!/usr/bin/env bash
set -euo pipefail

task_id="${1:-}"
workspace_or_session="${2:-}"
session_root="${3:-}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cache_base="${CODEAGENT_EVAL_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/codeagent-evals}"
venv_dir="$cache_base/$task_id"

if [[ -z "$task_id" || -z "$workspace_or_session" ]]; then
  echo "用法: $0 <task-id> <workspace> | $0 <task-id> <session-id> <session-root>" >&2
  exit 2
fi
if [[ ! -x "$venv_dir/bin/python" ]]; then
  echo "评估 venv 不存在，请先运行 prepare.sh" >&2
  exit 1
fi

if [[ -n "$session_root" ]]; then
  session_meta="$(find "$session_root" -mindepth 2 -maxdepth 3 -path "*/$workspace_or_session/session.json" -print -quit)"
  if [[ -z "$session_meta" ]]; then
    echo "找不到 Session metadata: $workspace_or_session ($session_root)" >&2
    exit 1
  fi
  workspace="$("$venv_dir/bin/python" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["active_workspace"])' "$session_meta")"
else
  workspace="$workspace_or_session"
fi

if [[ ! -d "$workspace/.git" ]]; then
  echo "评估环境不存在或不完整，请先运行 prepare.sh" >&2
  exit 1
fi

case "$task_id" in
  itsdangerous-strict-base64)
    python_path="$workspace/src"
    oracle="$script_dir/oracles/test_itsdangerous_strict_base64.py"
    ;;
  click-short-help-sentences)
    python_path="$workspace/src"
    oracle="$script_dir/oracles/test_click_short_help_sentences.py"
    ;;
  httpx-no-proxy-cidr)
    python_path="$workspace"
    oracle="$script_dir/oracles/test_httpx_no_proxy_cidr.py"
    ;;
  *)
    echo "未知 task-id: $task_id" >&2
    exit 2
    ;;
esac

PYTHONPATH="$python_path" "$venv_dir/bin/python" -m pytest -q -p no:cacheprovider "$oracle"
echo "verified workspace: $workspace"
git -C "$workspace" status --short
