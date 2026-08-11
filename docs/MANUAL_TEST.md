# 真实终端人工验收

本文是当前版本唯一的人工验收清单。它合并了 v0.3 的只读/受控 pytest 验证和 v0.4 的编辑/Candidate/apply 验证，只保留用户可观察的关键行为。

真实 DeepSeek 验收会使用 API Key 和网络，并会在专用 fixture 中制造、恢复和提交修改。不要对重要仓库执行。内部 artifact 文件格式由自动化测试覆盖，人工验收只抽查交付证据，不逐个检查所有诊断文件。

## 0. 当前验收基线

- 版本：v0.4.0 功能分支。
- 自动化：`112 passed, 2 skipped`；两个 skip 为当前 Windows 环境不能创建 symlink。
- 真实 TTY：2026-08-11 在 Windows PowerShell / DeepSeek V4 Flash 上完成本清单 1–7。
- 已验证修复：CRLF patch 上下文、12 步模型额度、Git porcelain 状态路径。

## 1. 准备 disposable fixture

在项目根目录打开 PowerShell：

```powershell
$env:DEEPSEEK_API_KEY="你的 key"
$fixture = Join-Path $env:TEMP "codeagent-v04-tiny-sort"
$sessionRoot = Join-Path $env:TEMP "codeagent-v04-sessions"

Copy-Item -Recurse examples\buggy-repos\tiny-sort-bug $fixture
Set-Location $fixture
git init
git config user.email "manual@example.com"
git config user.name "CodeAgent Manual"
@(".pytest_cache/", "__pycache__/", "*.pyc") | Set-Content .gitignore
git add .
git commit -m "fixture baseline"
git status --short
```

最后一条命令必须无输出。若路径已存在，改用新的临时目录，不要覆盖或清理真实用户 session。

## 2. Readonly 能力边界

```powershell
codeagent start $fixture `
  --provider deepseek `
  --model deepseek-v4-flash `
  --session-root $sessionRoot
```

执行 `/tools`，预期只有文件/Git 只读工具，不出现：

```text
run_check
apply_text_patch
freeze_candidate
```

让模型分析 bug 后 `/exit`。确认 source `git status --short` 无输出。这覆盖历史 v0.3 readonly tool calling 基线。

## 3. 真实读取、失败测试、编辑、复测和冻结

```powershell
codeagent start $fixture `
  --mode execution `
  --provider deepseek `
  --model deepseek-v4-flash `
  --session-root $sessionRoot
```

发送：

```text
请先读取实现和测试，运行目标 pytest 确认失败；只用受控文本 patch 修复排序 bug，再运行同一测试。通过后冻结 Candidate，并告诉我 candidate id。不要修改测试。
```

对两个固定 pytest 请求分别批准一次。预期：

1. 第一次 command `status=completed / exit_code=1`，断言显示 `None != [1, 2, 3]`。
2. Agent 只修改 task worktree；若一次 patch 上下文失败，可以重新读取并自我修正。
3. 第二次 pytest 为 `1 passed`。
4. `freeze_candidate` 返回 candidate id、patch SHA-256 和 `sort_utils.py`。
5. 单轮有足够额度返回最终模型总结，而不是在冻结后立刻触发步骤上限。

## 4. 审查 Candidate 和 source 隔离

```powershell
git -C $fixture status --short
codeagent show-candidate <candidate-id> --session-root $sessionRoot
```

预期 source 仍 clean 且保留原 bug；Candidate 页面展示：

- candidate/session/base identity；
- 固定 patch hash；
- changed files；
- 成功 pytest 摘要和 receipt 引用；
- 完整 diff。

人工只需确认这些交付证据。需要诊断时再查看 session 下的 `commands/`、`edits/`、`candidates/`；不要求普通用户理解每个中间文件。

## 5. Reject 和批准 Apply

先执行 apply 并输入 `n`：

```powershell
codeagent apply-candidate <candidate-id> --session-root $sessionRoot
```

预期 `status=rejected`，source 仍 clean。

再次执行，核对 Candidate id、hash、changed files、测试证据和 diff 完全一致后输入 `y`。预期：

- `status=applied`；
- `applied_patch_sha256` 等于审查时的 hash；
- source diff 正是 Candidate patch；
- `python -B -m pytest -q $fixture\test_sort_utils.py` 为 `1 passed`。

`-B` 用于避免在 source 生成 `__pycache__`。

## 6. Source 变化时 fail closed

本步骤只在 disposable fixture 中恢复实现文件，复用旧 Candidate 验证两个拒绝分支：

```powershell
git -C $fixture restore -- sort_utils.py
git -C $fixture status --short
```

### Source dirty

```powershell
Add-Content $fixture\README.md "user concurrent edit"
codeagent apply-candidate <candidate-id> --session-root $sessionRoot
```

预期 `status=source_dirty`、`applied_patch_sha256=null`，用户 README 修改保留，`sort_utils.py` 仍是原实现。

### Source HEAD changed

```powershell
git -C $fixture restore -- README.md
Add-Content $fixture\README.md "unrelated committed change"
git -C $fixture add README.md
git -C $fixture commit -m "unrelated source change"
git -C $fixture status --short
codeagent apply-candidate <candidate-id> --session-root $sessionRoot
```

预期 source clean，但 HEAD 与 Candidate base 不同；结果为 `status=source_changed` 且不应用 patch。Runtime 不 stash、reset、merge 或 rebase。

## 7. Resume 候选现场和保守 cleanup

本步骤使用 Fake provider，不产生 API 费用：

```powershell
codeagent start $fixture --mode execution --provider fake --session-root $sessionRoot
```

记下 session id，然后在 CLI 中执行：

```text
/call apply_text_patch {"path":"sort_utils.py","old_text":"    return nums.reverse()","new_text":"    return sorted(nums)"}
/exit
```

恢复时必须使用相同 session root：

```powershell
codeagent resume <session-id> --session-root $sessionRoot
```

预期 metadata 状态为 `candidate_changes`，且 `/tools` 仍包含 `run_check`、`apply_text_patch` 和 `freeze_candidate`。继续执行：

```text
/call run_check {"kind":"pytest","targets":["test_sort_utils.py"]}
/exit
```

批准后应为 `1 passed`。最后尝试：

```powershell
codeagent cleanup <session-id> --session-root $sessionRoot
```

预期 cleanup 拒绝删除 dirty worktree并保留现场。第一次不带 `--session-root` 找不到自定义目录属于当前 CLI 约束，不是 session 丢失。

## 验收结论

满足以下条件即可认定 v0.4 在定义范围内合格：

- 1–7 的用户可观察行为通过；
- 全量 pytest 通过；
- source 拒绝、HEAD 变化、Candidate hash 和 apply receipt 均 fail closed；
- 未把 host sandbox、dirty source snapshot、自动 merge/rebase 或任意 shell误宣称为已实现。

这证明 v0.4 达到“可审计的最小完整 Coding Agent 闭环”，不代表已经达到低摩擦的成熟产品体验。
