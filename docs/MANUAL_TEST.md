# 正式 CLI Dogfood

前半部分只验证工具接入和用户可观察闭环，不使用真实 LLM 判断推理能力。真实模型评估使用 `examples/eval-tasks`，结果与已知编排问题见 [真实 LLM Agent 测试审计](REAL_LLM_AGENT_EVALUATION_2026-08-30.md)。

## 准备

使用干净、可丢弃的 Git repository，并确认 `rg`、`bwrap` 可用：

```bash
command -v rg
bwrap --version
codeagent start /path/to/fixture --session-root /tmp/codeagent-manual-sessions
```

## Source-only 与按需升级

进入后 `/tools` 应包含 Search/Read、`apply_workspace_edit` 和 `run_command`。Session 初始不创建 worktree。

先调用只读工具；再调用：

```text
/call run_command {"command":"printf 'generated\n' > generated.txt && wc -l generated.txt","purpose":"validation"}
```

批准创建 Agent worktree。结果应为 exit 0，source repository 不出现 `generated.txt`，Session 状态为 `changes_active`。

## 权限与沙盒

- 不声明 NETWORK 时，访问 host/local network 应失败，并返回 Effective Policy 摘要；Runtime 不自动重试。
- Agent 提交新的 `execution_id` 和显式 NETWORK permission 后，应出现 requested/resolved/scope/reason 审批；批准后共享 WSL network。
- 尝试写 source absolute path 或 Candidate `.git` 应失败且宿主内容不变。
- Bubblewrap 不在 PATH 时应返回 `sandbox_unavailable`，不能裸执行。

## 当前修改与验证

```bash
codeagent show-changes <session-id> --session-root /tmp/codeagent-manual-sessions
```

输出应包含命令生成的 tracked/untracked 文件。成功 validation command 形成当前证据；只有后续操作改变 subject tree、workspace/base identity 或相关 Policy identity 时证据才失效。没有改变 workspace 内容的 utility command 不推进 Candidate revision，也不使证据失效。

使用 `apply_workspace_edit` 后重新运行一条有意义的 `purpose=validation` 命令。`accept-changes` 应展示 exact patch 并请求最终确认；`discard-changes` 应恢复 accepted baseline 且不修改 source。

## 异常状态抽查

- 注入 after audit failure：状态只能为 `workspace_tainted`，edit/command/validation/accept 拒绝，read/discard 可用；
- 注入进程树无法确认或 rollback/discard verification failure：进入 `recovery_required`；
- resume 时即使 worktree 表面 clean，`recovery_required` 仍不能重新开放 execution。

## Context 与执行切片抽查

- 单个 UserTurn 每 12 个 ModelStep 暂停为 execution slice，不写伪 Final 或“继续”UserMessage；`/continue` 延续原始请求；
- 同一 UserTurn 总计 48 个 ModelStep 时明确未完成终止；
- 最低 Context 集合超限时显示 `Context Capacity Reached`，说明 estimated/usable input tokens，Session/workspace 保留；
- `read_file` 部分读取显示 requested/returned range、file total lines 和前后剩余内容；
- `search_text` 默认 literal，正则必须显式 `mode=regex` 并在结果回显实际模式。
