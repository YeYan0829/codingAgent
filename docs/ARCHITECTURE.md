# 当前 Runtime 实现

本文按模块记录 v0.4 Runtime 已经实现的结构、状态和运行边界，用于代码接入与排障。它不定义下一版本方向，也不表示归档设计已经实现。

项目目标见 [System Vision](系统目标.md)，当前设计状态和下一步调研入口见[当前设计上下文](PROJECT_GUIDE.md)。用户能力见项目 [README](../README.md)，测试边界见 [TESTING.md](TESTING.md)，真实终端验收见 [MANUAL_TEST.md](MANUAL_TEST.md)。

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
    ├── TextPatchService / Edit journal
    └── CandidateService / Apply receipt
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

v0.4 的纵向关系是：

```text
Source workspace (用户正式代码)
  │ clean HEAD snapshot
  ▼
Task worktree (Agent 读取、pytest、受控编辑)
  │ freeze immutable patch + test evidence
  ▼
Candidate (用户审查的交付物)
  │ explicit approval + source identity recheck
  ▼
Source workspace (应用完全相同的 patch bytes)
```

对话和工具循环只操作 active workspace；正式 apply 是独立 CLI/Runtime 路径，不是 Agent 写工具。

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

当前拒绝类事件的配对仍需加强：Policy/Approval denial 目前只留下 `tool_denied`，可能让 provider 看到没有配对结果的 assistant tool call。这是当前实现待办，需要补 provider-compatible context 测试；不能描述为已解决能力。

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

`ready` 和可由 edit journal/command delta 解释的 `candidate_changes` 可以恢复 execution capability。Runtime 不自动刷新 stale session，也不把 source 新变化同步到旧 worktree。

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

Runtime 目录与 command artifact 通过 session id 和 command id 关联，但不是长期交付证据。当前 v0.4 仍会保留这些目录；自动清理和 retention policy 尚未实现。

真实命令前后采集 `git status --porcelain --untracked-files=all`。结束后保存 tracked binary diff 和 untracked no-index diff；receipt 记录 changed files、`workspace_changed` 和 audit error。Audit 失败不会覆盖已经获得的 pytest 结果，也不能被解释为“workspace 没有变化”。

pytest 默认缓存可能让未配置忽略规则的 worktree 变脏，从而影响 resume 和 cleanup。当前尚未统一缓存重定向或禁用策略，不能依赖仓库自己的 `.gitignore`。

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
- 自动 merge/rebase 和通用写文件接口。

敏感路径检查覆盖 workspace 内的父路径组件。工具 schema 仍需要在 Runtime 侧统一验证，而不只作为模型提示。

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

长期产品应把 readonly/execution 降为内部 capability，而不是要求普通用户预先理解 mode；这是明确的 UX 演进项，不是当前 v0.4 行为。

## 12. v0.4 受控编辑

只有可执行的 execution session 注册 `apply_text_patch`。服务必须同时确认：

- `workspace_kind == git_worktree` 且 `active_root != source_root`；
- 相对路径位于 `active_root`，路径组件不是 symlink，且不属于敏感路径；
- 已有文件是有限大小的 UTF-8 文本，新增文件的父目录已经存在；
- 已有文件的 `old_text` 非空并且只匹配一次；
- patch 与编辑后文件没有超过固定上限。

`read_file` 对文本使用通用换行语义；因此当文件统一使用 CRLF、LF 或 CR 时，patch 服务会把模型提交的上下文适配到原文件行尾后再匹配，并保持原行尾风格。混合行尾文件不做隐式规范化，仍要求原始上下文精确匹配。

第一版只提供这一种文本替换/新增能力，不删除文件。写入成功后保存：

```text
artifacts/edits/<edit-id>.json
  edit_id / path / before_sha256 / after_sha256 / patch / created_at
  session_id / source_root / active_root / base_commit
```

journal 写入失败时回滚文件变化。Readonly registry 不包含写工具；provider 仍只收到不含 handler 的轻量 schema。

## 13. Frozen Candidate

`CandidateService.freeze` 只收集 edit journal 明确归属的文件，并要求最后一次编辑后存在成功的真实 pytest command receipt。冻结时校验 worktree HEAD/base、文件类型以及当前内容与最后 journal hash；测试产生但不属于编辑的 dirty path 单独记录为 `workspace_side_effects`。

```text
artifacts/candidates/<candidate-id>/
├── candidate.json       # immutable manifest
├── candidate.patch      # immutable delivery bytes
└── status.json          # frozen/rejected/applied 等可变生命周期
```

Manifest 绑定 candidate/session/source/active/base、patch SHA-256、changed files、测试 receipt 和创建时间。读取 Candidate 时总是重新计算 artifact hash，因此 worktree 后续变化或 artifact 篡改不能静默改变用户看到的交付物。

## 14. 独立 Apply 流程

正式 apply 不注册为 Agent 普通写工具。CLI/Runtime 依次执行：

```text
load + verify candidate hash
→ source HEAD == base_commit 且完全 clean
→ git apply --check 固定 patch
→ 展示 identity、测试证据和完整 diff
→ 用户明确批准
→ 再次检查 HEAD/clean 和 patch preflight
→ git apply 完全相同的 bytes
→ artifacts/applies/<apply-id>.json
```

拒绝、source dirty、HEAD 改变、preflight 失败或批准期间并发变化都生成 receipt 且不应用 Candidate。第一版不自动 merge/rebase，也不对 dirty source 建 snapshot。

## 15. Candidate workspace 与命令审计

普通未知 dirty worktree 仍是 `worktree_dirty`，禁用 execution capability。只有当前文件 hash 能由 edit journal 解释，且其余 dirty path 能由 command delta 解释时，resume 才标记为 `candidate_changes` 并继续开放编辑与 pytest。

命令 receipt 同时记录：

- `preexisting_changed_files`：命令开始前已有的 Candidate 状态；
- `command_introduced_changes`：命令前后新增的 Git 状态行；
- `changed_files`：命令结束后的完整 dirty 文件集合。

这样 pytest 开始前已有的 Agent edits 不会被误报成该命令新产生的副作用。

## 16. 产品任务与内部 capability

当前 CLI 在 session 创建前要求选择 `readonly` 或 `execution`，原因是 worktree、工具注册和 executor 必须在 `AgentRunner` 构造前固定。这使安全边界清楚，但把内部实现泄漏给了用户：用户想表达的是“分析或修复这个任务”，而不是选择 workspace backend。

目标形态应是：

```text
用户创建一个 task
→ Agent 默认读取
→ 首次需要 pytest/编辑时 Runtime 展示能力升级和风险
→ 用户批准后创建 task workspace
→ 同一任务继续测试、编辑、审查和 apply
```

要实现它，session 需要支持显式 workspace revision/capability transition，而不能在同一个 loop 中静默切换 `active_root`。在该数据模型完成前，v0.4 保留 mode 参数作为诚实但偏底层的入口。

## 17. 证据、诊断和 retention

当前 session 目录混合了三类生命周期不同的数据：

```text
长期交付证据
  candidate.patch / candidate.json / apply receipt / 最终测试摘要

任务历史
  messages / tool timeline / approval decisions / 关键失败原因

诊断与运行副产物
  完整 stdout/stderr / command workspace patch / edit journals / runtime HOME/TEMP
```

完整 trace 对开发安全边界、复现 Windows/pytest 问题和验证 Candidate 没有被偷换很有价值；真实 Coding Agent 和企业平台也常保存这些信息。但产品层通常只展示任务摘要、diff 和测试状态，把底层 trace 放入“诊断详情”，并对大日志与临时目录设置 retention。

v0.4 是 audit-first 原型：三层目前都持久化，`cleanup` 也只处理 worktree，不删除 session/artifacts。这不是最终存储策略。后续应保持 Candidate/apply 证据稳定，同时为命令日志、edit journal 和 runtime 目录增加容量上限、TTL、压缩/聚合和用户可控删除；模型上下文也应消费摘要而非长期依赖全部原始文件。
