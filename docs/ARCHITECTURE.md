# 架构说明

本文是当前代码结构的唯一详细说明，合并了原 `MODULES.md`、workspace、command execution、local executor 和 CLI execution 等分散文档。版本定位和用户能力先看 [V03_OVERVIEW.md](V03_OVERVIEW.md)。

## 1. 总体结构

```text
CLI
├── SessionStore
├── WorkspaceContext / GitWorktreeManager
├── Model Gateway
└── AgentRunner
    ├── ContextBuilder
    ├── ToolRegistry
    ├── DefaultPolicy / ApprovalGate
    └── CommandService
        ├── CommandPolicy
        ├── ApprovalGate
        ├── LocalCommandExecutor
        └── CommandArtifactStore
```

主要模块：

| 模块 | 职责 | 不负责 |
| --- | --- | --- |
| `codeagent.cli` | CLI 参数、交互、session 创建/恢复/清理 | 直接解释或执行模型命令 |
| `workspace` | source/active 身份、worktree 创建和状态检查 | stash、merge、rebase、自动同步 |
| `session` | metadata、append-only events、transcript、artifact 路径 | Policy 决策或子进程管理 |
| `model_gateway` | 内部请求与 provider schema 的转换 | 文件系统、工具执行和审批 |
| `context` | 从 session events 重建模型消息 | 执行工具 |
| `tools` | 工具 schema、权限级别和 handler | 绕过 Policy 或直接开放 shell |
| `runtime` | Agent loop、Policy、Approval 和命令编排 | provider-specific response 解析 |
| `safety` | workspace 路径与敏感内容边界 | 主机级 sandbox |

## 2. Readonly Agent 数据流

```text
用户消息
→ CLI
→ SessionStore.append_event(user_message)
→ ContextBuilder 重建 messages
→ AgentRunner 构造 ModelRequest
→ Model Gateway 调用 Fake/DeepSeek
→ LLMResponse
```

如果模型返回普通文本：

```text
LLMResponse.text
→ assistant_message event
→ 当前 turn 结束
```

如果模型返回 tool calls：

```text
LLMToolCall
→ assistant_tool_calls / tool_requested events
→ ToolRegistry 查找 ToolSpec
→ DefaultPolicy: allow / ask / deny
→ 必要时 ApprovalGate
→ handler
→ tool_result event
→ observation 进入下一轮模型上下文
```

这是一种简化的 ReAct loop：模型交替进行推理、提出 Action（工具调用）、读取 Observation（工具结果），直到返回最终文本。模型只有请求权，执行权仍在 Runtime。

## 3. Model Gateway 与消息语义

`AgentRunner` 只依赖内部类型：

- `ModelRequest`
- `LLMResponse`
- `LLMToolCall`
- `ModelTool`

工具描述转换链路是：

```text
ToolSpec → ModelTool → provider-specific tools schema
```

`ToolSpec` 包含 handler 和权限等 Runtime 信息；`ModelTool` 只包含模型需要看到的 name、description 和 JSON schema。DeepSeek client 负责转换为 Chat Completions 格式。

真实 tool calling 要求 assistant tool call 与 tool result 通过 `tool_call_id` 配对。`ContextBuilder` 根据 session events 重建：

```python
{"role": "assistant", "content": None, "tool_calls": [...]}
{"role": "tool", "tool_call_id": "...", "content": "..."}
```

当前拒绝类事件的配对仍需加强：Policy/Approval denial 不能只留下 `tool_denied` 而让 provider 看到没有结果的 assistant tool call。该问题应在 v0.3 收口时修复并补 provider-compatible context 测试。

## 4. WorkspaceContext

`WorkspaceContext` 是一次任务的工作区身份：

```text
source_root        用户原始项目
active_root        当前工具实际操作目录
workspace_kind     source / git_worktree
base_commit        execution 创建时的 source HEAD
task_workspace_id  独立任务工作区标识
```

Readonly：

```text
source_root == active_root
workspace_kind = source
```

Execution：

```text
source_root != active_root
workspace_kind = git_worktree
```

所有文件与 Git 工具使用 `active_root` 建立路径边界。`active_root` 必须在构造 `ToolRegistry` 和 `AgentRunner` 前确定，一次 Agent loop 中不得静默切换。

## 5. Git worktree 创建、恢复与清理

### 创建

Execution 只接受 Git repository root，并要求 `git status --porcelain --untracked-files=all` 无输出。Runtime 记录 HEAD 为 `base_commit`，然后创建：

```text
<session_root>/worktrees/<task_workspace_id>/
```

使用 detached HEAD 可以复用 Git object database，同时让 task workspace 不绑定用户正式分支。

### Resume 状态

Resume 检查：

- source 是否仍是记录的 Git root；
- active worktree 是否存在且仍被 Git 注册；
- source/worktree HEAD 是否等于 `base_commit`；
- source 和 worktree 是否 dirty。

只有 `ready` 恢复 execution capability。Runtime 不自动刷新 stale session，也不把 source 新变化同步到旧 worktree。

### Cleanup

普通退出保留 worktree。显式 cleanup 先确认 active path 位于受管 `worktrees_root`，再调用不带 `--force` 的 `git worktree remove`。Dirty worktree 删除失败并保留现场；session 历史和 artifacts 不随 worktree 删除。

## 6. Session 持久化

默认目录：

```text
~/.codeagent/sessions/<workspace-key>/<session-id>/
├── meta.json
├── events.jsonl
├── transcript.md
└── artifacts/
```

`workspace-key` 由 workspace 名称和绝对路径 hash 组成。`CODEAGENT_SESSION_ROOT` 和 `--session-root` 可以覆盖根目录。旧版 `<workspace>/.codeagent/sessions/` 仍可在已知 workspace 下作为 load fallback。

职责划分：

- `meta.json`：session identity、provider/model、source/active、base commit 和 lifecycle。
- `events.jsonl`：append-only 结构化事件，是上下文和审计的主要来源。
- `transcript.md`：面向人阅读的简化日志，不代替完整 events 和 command artifacts。

当前一个 execution session 绑定一个 worktree。未来若引入逻辑 session 下的多个 workspace revision，应通过新数据模型显式表示，不能原地改写旧 revision 的 base 和 artifact 语义。

## 7. 受控 pytest 命令协议

模型只能提交：

- `kind="pytest"`
- workspace 内相对 targets
- 相对 `active_root` 的 cwd
- 1 到 300 秒 timeout

`CommandService` 在 Policy 前构造 frozen `CommandSpec`：

```text
(sys.executable, "-m", "pytest", *targets)
```

完整链路：

```text
run_check arguments
→ CommandSpec
→ CommandPolicy: deny / require_approval
→ ApprovalGate: deny / approve_once
→ CommandExecutor
→ CommandResult
→ CommandReceipt + session event + artifacts
→ 精简 ToolResult
```

命令状态包括：

- `policy_denied`
- `approval_denied`
- `completed`
- `timed_out`
- `spawn_failed`
- `executor_rejected`

测试断言失败仍是 `completed`，用非零 exit code 表示；只有进程启动失败才是 `spawn_failed`。

## 8. LocalCommandExecutor

Executor 只接收已经批准的 `CommandSpec`，不接收原始模型参数。执行前再次验证 worktree、pytest kind、固定 argv 和 cwd。

进程启动约束：

- argv list；
- `shell=False`；
- `stdin=DEVNULL`；
- 固定 timeout；
- stdout/stderr 直接写文件；
- POSIX 使用独立 process group；
- Windows 使用 `CREATE_NEW_PROCESS_GROUP` 和可信绝对 `taskkill.exe` 做 best-effort tree termination。

Timeout 后的进程终止仍是 best effort。终止失败、二次 wait timeout 和逃逸子进程需要在后续加强，不能将其描述成强隔离。

### Minimal environment

Executor 不复制完整 `os.environ`，只保留 PATH、必要 Windows 系统变量和有限 locale/timezone 变量。HOME、USERPROFILE 和临时目录指向 command-local runtime 目录，并设置：

```text
PYTHONIOENCODING=utf-8
PYTHONDONTWRITEBYTECODE=1
```

模型 API key、GitHub token、SSH agent、npm/PyPI token等不在 allowlist 中。Receipt 只记录环境变量名称，不记录 value。

## 9. Artifacts 与 workspace audit

命令 artifact 目录不位于 worktree：

```text
<session_dir>/artifacts/commands/<command_id>/
├── request.json
├── result.json
├── stdout.log
├── stderr.log
└── workspace-change.patch
```

子进程的 HOME/TEMP 使用独立短路径，避免 Windows 上被测程序继续创建深层目录时超过路径长度限制：

```text
<session_root>/runtime/<session_id>/<command_id 前 12 位>/
├── home/
└── tmp/
```

Runtime 目录与 command artifact 通过 session id 和 command id 关联，但不是长期审计证据。当前 v0.3 仍会保留这些目录；自动清理和 retention policy 留待后续生命周期增强。

真实命令前后采集 `git status --porcelain --untracked-files=all`。结束后保存 tracked binary diff 和 untracked no-index diff；receipt 记录 changed files、`workspace_changed` 和 audit error。Audit 失败不会覆盖已经获得的 pytest 结果，也不能被解释为“workspace 没有变化”。

pytest 默认缓存可能让未配置忽略规则的 worktree 变脏，从而影响 resume 和 cleanup。v0.3 收口时应明确缓存重定向或禁用策略，而不是依赖仓库自己的 `.gitignore`。

## 10. Safety 边界

已经实现：

- workspace path guard 和 symlink escape 检查；
- 敏感文件内容读取拒绝；
- 固定 pytest argv；
- cwd/target 边界；
- Policy 和 approve-once；
- minimal environment；
- timeout 和输出截断；
- artifacts、receipt 和 workspace side-effect audit；
- worktree 对 source Git 状态的隔离。

尚未实现：

- host filesystem sandbox；
- 网络隔离；
- CPU、内存、磁盘、进程数量和系统调用限制；
- 对恶意或不可信仓库代码的保护；
- dirty source snapshot；
- 写工具与候选 patch apply。

敏感路径检查目前主要依据目标文件名，嵌套在敏感目录中的普通文件名仍需补充父路径组件检查。工具 schema 也需要在 Runtime 侧统一验证，而不只作为模型提示。

## 11. CLI 组装边界

CLI 必须先确定 session mode 和 `WorkspaceContext`，再构造 registry/runner：

```text
create/load session
→ determine active workspace
→ inspect execution state
→ build ToolRegistry(active_root)
→ optionally inject CommandService/Executor
→ build AgentRunner
```

非交互 `ask --mode execution` 可以创建 execution session，但需要 approval 的 pytest 默认 fail closed。交互 `start` 使用 console approval。只有 ready execution session 注册 `run_check`。

长期产品可以把 readonly/execution 降为内部 capability，而不是要求普通用户预先理解模式；这是 UX 演进，不是 v0.3 当前行为。
