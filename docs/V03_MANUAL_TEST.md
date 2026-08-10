# v0.3 真实终端人工验收清单

这份清单用于 release 前人工验证真实 TTY、DeepSeek tool calling、approval、pytest、artifact 和 worktree 生命周期。不要在包含敏感代码或重要未提交修改的仓库上执行。

## 测试前条件

- 准备一个专用的小型 Git 仓库，包含至少一个通过测试和一个失败测试。
- 为验证 clean resume，fixture 应提交 `.gitignore` 并忽略 `.pytest_cache/`；pytest cache 的统一重定向/禁用仍是 v0.3 收口项。
- Source workspace 必须完全 clean：`git status --porcelain --untracked-files=all` 无输出。
- 运行 CodeAgent 的 `sys.executable` 已安装 pytest。
- 已配置可用的 `DEEPSEEK_API_KEY`，并确认 provider/model 可访问。
- 使用真实交互终端；不要通过 pipe 模拟 approval。
- 建议显式设置专用 `CODEAGENT_SESSION_ROOT`，便于检查和清理。

PowerShell 示例：

```powershell
$env:DEEPSEEK_API_KEY="你的 key"
$env:CODEAGENT_SESSION_ROOT="D:\codeagent-v03-manual-sessions"
$REPO="D:\repos\codeagent-v03-fixture"
git -C $REPO status --porcelain --untracked-files=all
```

记录 session id、source path 和 active workspace：

```text
SESSION_ID=
SOURCE_ROOT=
ACTIVE_ROOT=
```

## 验收记录

### 1. Readonly session 不创建 worktree

- 执行命令：`codeagent start $REPO --provider deepseek --model deepseek-v4-flash`
- 检查动作：在 CLI 中执行 `/tools`，随后 `/exit`。
- 预期结果：工具列表没有 `run_check`；session metadata 中 `workspace_kind=source` 且 source/active 相同；session root 下没有该 session 的 task worktree。
- 实际结果：
- 是否通过：`[ ]`
- 备注：

### 2. Execution session 创建 detached worktree

- 执行命令：`codeagent start $REPO --mode execution --provider deepseek --model deepseek-v4-flash`
- 预期结果：metadata 记录 `mode=execution` 以及 source/active/base commit/task id；`git -C <ACTIVE_ROOT> rev-parse --abbrev-ref HEAD` 输出 `HEAD`。
- 实际结果：
- 是否通过：`[ ]`
- 备注：

### 3. 模型能够看到 `run_check`

- 执行命令：在 execution CLI 中执行 `/tools`。
- 预期结果：工具列表出现 `run_check`。完整 schema 由自动化测试和 provider 请求 mock 验证；当前 `/tools` 只显示名称、权限级别和描述。
- 实际结果：
- 是否通过：`[ ]`
- 备注：

### 4. Approval 展示完整固定命令

- 执行命令：输入“请运行 test_fail.py 并解释失败”。
- 预期结果：终端显示 kind、完整 `sys.executable -m pytest ...` argv、相对 cwd、timeout、active workspace，以及没有 host sandbox/network isolation 的警告。
- 实际结果：
- 是否通过：`[ ]`
- 备注：

### 5. Deny 后命令不执行

- 执行动作：在 approval prompt 选择 deny。
- 预期结果：模型收到拒绝；artifact receipt 状态为 `approval_denied`；没有 pytest 进程结果。
- 实际结果：
- 是否通过：`[ ]`
- 备注：

### 6. Approve once 后真实执行 pytest

- 执行命令：再次请求相同测试，在 approval prompt 选择 approve once。
- 预期结果：pytest 真实运行；批准时看到的 `CommandSpec` argv 与 request/result 中记录的 argv 一致。
- 实际结果：
- 是否通过：`[ ]`
- 备注：

### 7. 模型收到成功或失败摘要

- 检查动作：观察 tool result 和模型后续回答。
- 预期结果：包含 execution status、exit code 和有限 stdout/stderr 摘要；失败测试状态仍是 `completed`，exit code 非 0。
- 实际结果：
- 是否通过：`[ ]`
- 备注：

### 8. Command artifacts 完整生成

- 执行命令：检查 `<session_dir>/artifacts/commands/<command_id>/`。
- 预期结果：存在 `request.json`、`result.json`、`stdout.log`、`stderr.log` 和 `workspace-change.patch`，完整输出在日志中；命令 HOME/TEMP 位于 `<session_root>/runtime/<session_id>/<短 command_id>/`，不再嵌套于 artifact 目录。
- 实际结果：
- 是否通过：`[ ]`
- 备注：

### 9. Source workspace 不受 pytest 副作用污染

- 执行命令：`git -C $REPO status --porcelain --untracked-files=all`
- 预期结果：无输出；source 中没有由该 execution session 产生的 `.pytest_cache` 或源码变化。
- 实际结果：
- 是否通过：`[ ]`
- 备注：

### 10. 普通退出后 resume 为 ready

- 执行动作：在 execution CLI 中 `/exit`，然后执行 `codeagent resume <SESSION_ID>`。
- 预期结果：在 fixture 已忽略 pytest cache 且测试没有产生其他副作用时，worktree 仍存在、resume 状态为 `ready`，`/tools` 仍显示 `run_check`。若 worktree 出现 Git 可见变化，应识别为 `worktree_dirty`，不能误报 ready。
- 实际结果：
- 是否通过：`[ ]`
- 备注：

### 11. Source HEAD 改变后识别为 stale

- 执行动作：退出 session，在 clean source 新建并提交一个 commit，然后 `codeagent resume <SESSION_ID>`。
- 预期结果：显示 `stale`，不自动刷新或重建旧 worktree，`run_check` 被禁用。
- 实际结果：
- 是否通过：`[ ]`
- 备注：

### 12. pytest 产生的文件变化被审计

- 前置准备：使用一个测试，在执行时修改 tracked fixture 并生成 untracked 文件。
- 执行动作：在新的 ready execution session 中批准运行该测试。
- 预期结果：`result.json` 中 `workspace_changed=true`，changed files 包含两类文件，`workspace-change.patch` 保存 tracked/untracked diff；ToolResult 只返回有限列表和 artifact 引用。
- 实际结果：
- 是否通过：`[ ]`
- 备注：

### 13. Dirty worktree cleanup 被拒绝

- 执行命令：`codeagent cleanup <DIRTY_SESSION_ID>`
- 预期结果：命令失败并明确说明 worktree dirty；现场和 artifact 均保留；不使用强制删除。
- 实际结果：
- 是否通过：`[ ]`
- 备注：

### 14. Clean worktree 可以显式 cleanup

- 执行命令：`codeagent cleanup <CLEAN_SESSION_ID>`
- 预期结果：只删除对应受管 worktree；metadata lifecycle/state 标记为 discarded；source 仓库不变。
- 实际结果：
- 是否通过：`[ ]`
- 备注：

### 15. 两个 execution session 相互独立

- 执行动作：对同一 clean source 依次创建两个 execution session，记录两个 active root；清理其中一个 clean session。
- 预期结果：两个 active root 和 task id 不同；一个 session 的 pytest/cache/artifact 不影响另一个；cleanup 一个不会删除另一个 worktree。
- 实际结果：
- 是否通过：`[ ]`
- 备注：

## 汇总

- 测试日期：2026-08-10
- 操作系统与终端：Windows / PowerShell 真实 TTY
- Python / pytest 版本：Python 3.11.5 / pytest 9.1.1
- CodeAgent 版本：`v0.3.0`
- DeepSeek model：`deepseek-v4-flash`
- 核心 smoke session：`abeed1f609e4`
- 核心 smoke：通过；完整 15 项清单没有逐项手工重复，生命周期和双 session 场景由自动化测试覆盖
- 阻塞问题：无
- Artifact/session 位置：`<TEMP>/ca-v03-ds/`；批准命令 artifact 已验证包含 request/result、stdout/stderr 和 workspace patch
- 验收结论：DeepSeek 连接、tool calling、deny/approve、pytest、结果回传、artifact、worktree 隔离和 Windows 短 TEMP 路径均通过。v0.3 核心 release smoke 通过。

### 本次核心 smoke 实际记录

1. readonly 非交互请求收到“DeepSeek 连接成功”。
2. execution session 创建 active worktree，`/tools` 显示 `run_check`。
3. 第一次运行 `tests/test_runner_fake_llm.py` 时选择 deny；pytest 未执行，DeepSeek 正确解释拒绝并继续对话。
4. 第二次选择 approve once；receipt 为 `status=completed`、`exit_code=0`、`timed_out=false`，pytest 为 `1 passed in 1.26s`。
5. `workspace_changed=false`、changed files 为空，source `git status --short` 无输出。
6. artifact 文件完整；命令在短 runtime TEMP 下完成，没有复现原 Windows 路径过长错误。
