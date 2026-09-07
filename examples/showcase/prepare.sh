#!/usr/bin/env bash
set -euo pipefail

target="${1:-}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
fixture="$script_dir/repos/offline-zero-count"

if [[ -z "$target" ]]; then
  echo "用法: $0 <empty-target-directory>" >&2
  exit 2
fi
if [[ -e "$target" && ( ! -d "$target" || -n "$(find "$target" -mindepth 1 -maxdepth 1 -print -quit)" ) ]]; then
  echo "拒绝覆盖非空路径: $target" >&2
  exit 1
fi

mkdir -p "$target"
cp -a -- "$fixture/." "$target/"
git -C "$target" init -q
git -C "$target" config user.name "CodeAgent Showcase"
git -C "$target" config user.email "showcase@codeagent.invalid"
git -C "$target" add --all
git -C "$target" commit -q -m "CodeAgent showcase baseline"

cat <<EOF
快速体验仓库已准备：
  workspace: $target
  task:      $target/CODEAGENT_TASK.md

先运行失败基线：
  cd $target
  python3 verify.py

然后用 VS Code 打开 workspace，并输入：
  修复零数量时的错误，运行仓库中的验证脚本并总结修改。
EOF
