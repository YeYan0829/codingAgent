# CodeAgent Runtime v0.3 总览

## 1. 当前定位

CodeAgent Runtime v0.3 是一个面向**可信本地 Git 仓库**的受控 Coding Agent Runtime。它已经支持只读代码探索和 pytest-only controlled execution：模型可以读取代码、申请运行 pytest、观察结果并继续分析，但还不能主动编辑项目文件或把 patch 应用到用户仓库。

v0.3 的核心目标不是交付完整 coding loop，而是验证下面这条受控执行链：

```text
模型提出结构化测试请求
→ Runtime 固化命令
→ Policy 校验
→ 用户单次 Approval
→ 独立 Git worktree 中执行 pytest
→ 保存完整输出与工作区副作用
→ 精简结果返回模型
```

当前包版本为 `0.3.0`。v0.3 已在 `wip/v0.3` 分支形成经过验证的开发基线，但尚未合并到 `master` 或创建公开 release/tag。自动化测试基线为 `92 passed, 1 skipped`；2026-08-10 已在 Windows PowerShell 中完成 DeepSeek V4 Flash 真实 TTY 核心端到端验收，包括连接、tool calling、deny 后继续对话、approve once、真实 pytest、artifact 和 source clean 检查。详细记录见[人工验收清单](V03_MANUAL_TEST.md)。

## 2. 用户现在能做什么

CLI 提供：

- `start`：创建交互 session。
- `ask`：执行一次非交互任务。
- `list-sessions`：列出统一 session root 中的历史任务。
- `resume`：恢复对话和任务工作区。
- `cleanup`：显式清理干净的 execution worktree。

工具能力包括：

- 文件只读工具：`list_dir`、`show_tree`、`read_file`、`search_text`、`find_files`。
- Git 只读工具：`git_status`、`git_diff_stat`，执行前仍需 approval。
- Execution 工具：`run_check`，只接受结构化 pytest 请求。

尚不支持：

- Agent 主动写文件、编辑文件或应用 patch。
- 任意 shell、npm、pnpm、ruff、mypy 或依赖安装。
- 项目 `.venv` 自动发现。
- 主机文件系统、网络和系统调用隔离。
- dirty Git source 的 execution session。
- 候选修改审查后应用到 source 的完整流程。

## 3. Readonly 与 execution

### Readonly

Readonly 是默认模式。Agent 直接读取用户指定的目录：

```text
source_root == active_root
```

它不创建 worktree，也不注册 `run_check`。Readonly 可以读取 dirty Git 仓库中的当前磁盘内容，也可以用于非 Git 目录。

### Execution

Execution 必须显式启用：

```bash
codeagent start . --mode execution
```

它要求 source 是完全 clean 的 Git 仓库根目录，然后从当前 HEAD 创建 detached worktree：

```text
source_root                         active_root
用户正式仓库 ── Git HEAD 快照 ──→ session 独立 worktree
```

文件工具和 pytest 都使用 `active_root`。pytest 的缓存、生成文件或 tracked 修改不会直接写入 source。

Clean-only 是 v0.3 的阶段性限制。因为 worktree 从 Git HEAD 创建，如果 source 含有未提交修改，worktree 将看不到这些修改，模型可能分析和测试错误版本。v0.3 选择拒绝，而不是隐式 stash、commit 或复制用户现场；dirty workspace snapshot 留待后续版本。

## 4. 核心对象

| 对象 | 含义 | 当前职责 |
| --- | --- | --- |
| Source workspace | 用户原始项目目录 | 保存正式代码；Runtime 不 stash、不自动 commit，也不回写 task 变化 |
| Active workspace | 当前工具实际操作的目录 | readonly 时等于 source；execution 时是 worktree |
| Worktree | execution session 的独立 Git 工作目录 | 隔离 source 与测试产生的 Git 状态变化 |
| Session | 一次任务的持久化记录 | 保存对话、tool calls、metadata、workspace 状态和 artifacts |
| CommandSpec | Runtime 固化后的不可变命令 | 固定 argv、cwd、timeout 和 command kind |
| Receipt | 一次命令的结构化回执 | 记录 policy、approval、状态、结果和副作用 |
| Artifact | 完整执行证据 | 保存 request/result、stdout/stderr 和 workspace diff |

当前一个 execution session 绑定一个 worktree 和一个 `base_commit`。Resume 只检查并恢复原现场，不做自动同步、merge 或 rebase。

未来完整产品可以让一个逻辑对话包含多个 workspace revision，但这不是 v0.3 的数据模型，也不应在写入闭环稳定前提前实现。

## 5. 一次 execution session 的数据流

```text
CLI --mode execution
  → 校验 clean Git repository root
  → 从 source HEAD 创建 detached worktree
  → 保存 WorkspaceContext(source_root, active_root, base_commit, task id)
  → 所有只读工具统一绑定 active_root
  → 模型调用 run_check(kind="pytest", targets=..., cwd=..., timeout=...)
  → CommandService 构造 frozen CommandSpec
  → CommandPolicy 检查命令和路径边界
  → ApprovalGate 展示固定 argv、cwd、timeout 和风险提示
  → LocalCommandExecutor 使用 shell=False 执行 pytest
  → CommandArtifactStore 保存完整日志和 workspace diff
  → 精简 ToolResult 返回模型
```

Policy、Approval、Executor 接收同一个不可变 `CommandSpec`。模型不能提交完整 shell string，也不能在 approval 后替换解释器或改写 argv。

## 6. Session 与 worktree 生命周期

Execution resume 会检查 source、active worktree、Git 注册关系、两端 HEAD 和 dirty 状态：

- `ready`：允许恢复 `run_check`。
- `source_dirty`：source 出现未提交变化，保留历史但禁用执行。
- `stale`：source 或 worktree HEAD 不再等于 `base_commit`。
- `worktree_dirty`：任务工作区存在 Git 可见变化。
- `missing`：source、worktree 或 Git 注册关系缺失。
- `discarded`：用户已经显式 cleanup。

普通退出保留 worktree，以便 resume。`cleanup` 只移除 session root 下受管且干净的 worktree，不使用 `--force`；dirty worktree 和命令 artifacts 会保留。

这不是持续同步系统：source 在任务期间发生变化后，旧 session 会变成 stale 或 source_dirty，而不是自动吸收新代码。

## 7. Worktree、Approval 与 Sandbox

这些机制解决不同问题：

- **Worktree**：隔离 source 和 task 的 Git 代码状态。
- **Policy**：判断请求是否符合系统规则。
- **Approval**：让用户决定是否接受一次固定的高风险操作。
- **Audit**：记录请求、结果和 workspace 变化。
- **Host sandbox**：限制进程实际能访问的文件、网络、资源和系统调用。

v0.3 实现了前四项，没有实现 host sandbox。pytest 仍是当前用户权限下的本地进程，可能访问 worktree 外文件、网络、本地服务或启动其他进程。因此 execution mode 只适用于用户信任的本地仓库。

## 8. Artifacts 与测试结果

每个进入命令 Policy 的请求使用独立目录：

```text
<session_dir>/artifacts/commands/<command_id>/
├── request.json
├── result.json
├── stdout.log
├── stderr.log
└── workspace-change.patch
```

每次真实命令还使用短路径运行目录：

```text
<session_root>/runtime/<session_id>/<短 command_id>/
├── home/
└── tmp/
```

真实执行时，stdout/stderr 从进程启动起直接写入日志。模型只接收有限的头尾摘要；tracked diff、untracked diff、changed files 和 audit error 写入结构化结果。HOME/TEMP 与深层 artifact 路径分离，为 Windows 子进程保留路径空间。Policy deny 和 Approval deny 也生成 request/result 与空日志，保证审计链完整，但不会创建 runtime 目录。

## 9. 当前成熟度

v0.3 已经建立“理解代码 + 受控验证”的底座，但还不是完整 Coding Agent：

```text
理解代码        已具备
验证判断        已具备 pytest-only 基础
修改候选代码    尚未实现
测试候选修改    尚未形成闭环
用户审查与应用  尚未实现
主机级隔离      尚未实现
```

v0.3 现在作为受控执行基线冻结。下一阶段直接实现“隔离修改 → 测试 → 候选 diff → 用户接受并安全应用或放弃”的最小完整闭环，不再以零散基础设施优化延迟用户可用能力。详细范围见 [ROADMAP.md](ROADMAP.md)。

### v0.3 验收结论

2026-08-10 的 release smoke 使用 Python 3.11.5、pytest 9.1.1 和 `deepseek-v4-flash`，验证了：

- readonly DeepSeek 请求能够正常完成；
- execution session 从 clean source 创建独立 detached worktree；
- DeepSeek 能发现并调用 `run_check`；
- 用户 deny 后命令不执行，模型仍能收到 observation 并继续回答；
- approve once 后在 active worktree 中运行目标 pytest，结果为 `completed / exit_code=0 / 1 passed`；
- command request/result、stdout/stderr 和 workspace patch 均有留痕；
- `workspace_changed=false`，source `git status --short` 保持无输出；
- Windows 命令 TEMP 短路径修复在原失败用例上生效。

这证明 v0.3 的核心“模型请求 → Policy → Approval → 隔离执行 → 审计 → 模型解释”链路可用。它不证明恶意代码受到主机沙箱隔离，也不改变 pytest-only、clean source 和不能编辑文件的产品边界。

## 10. 文档入口

- [ARCHITECTURE.md](ARCHITECTURE.md)：模块边界、运行链路、workspace、命令执行和 artifacts。
- [DECISIONS.md](DECISIONS.md)：关键设计取舍及其适用边界。
- [ROADMAP.md](ROADMAP.md)：从 v0.3 到完整候选修改闭环的版本路线。
- [TESTING.md](TESTING.md)：自动化测试、smoke test 和测试不能证明的内容。
- [V03_MANUAL_TEST.md](V03_MANUAL_TEST.md)：真实终端 release 验收清单。
