# v0.5 Application Layer Design

> **Historical design：** 本文依赖原 v0.5 用户故事和 VS Code 路线，尚未实现，也不再是当前 Application Layer 计划。当前方向见[当前设计上下文](../PROJECT_GUIDE.md)。

本文从 [v0.5 用户故事与产品行为](V05_USER_STORIES.md) 反推 Application Layer 的语义契约。它不冻结 JSON-RPC、stdio framing、TypeScript 类型或具体 Python 类；文中的接口名是 Application Use Case，而不是传输协议 method。

## 1. 定位与边界

```text
VS Code Extension / CLI
        ↓ 用户操作
Server / Transport Adapter
        ↓ 类型化请求
Application Layer
        ↓ 用例编排与产品语义
Runtime / Domain Services
        ↓ 安全事实、执行事实、持久化证据
Workspace / Session / Git / Process / Artifacts
```

Application 负责：

- 初始化 workspace 产品上下文并报告 readiness；
- 创建、列出、读取和 Resume Task；
- 创建及 Stop Run，并保证 active Run 约束；
- 协调 Workspace Revision 的按需物化；
- 暂停 Run 等待 Approval，并接收用户决定；
- 获取、接受、继续调整或放弃整组 Changes；
- 将 Domain 状态投影成稳定 View、allowed actions 和产品事件；
- 将底层异常映射成稳定 Application Error；
- 为重复 Stop、Approval、Accept 和 Abandon 提供幂等边界。

Application 不负责：

- 自己读取 Git HEAD/status、文件内容或 VS Code unsaved buffer；
- 自己创建 worktree、执行 pytest、启动进程或调用 `git apply`；
- 自己计算 Candidate hash、解释 patch 或实现 Policy；
- 根据终端文本、stdout、`events.jsonl` 字符串或 artifact 目录猜状态；
- 维护与 Runtime/Domain 冲突的第二套安全事实；
- 定义传输 framing、RPC request id 或 Server 进程生命周期。

Server 只负责消息收发、校验、分发和事件转发。Runtime/Domain 仍负责 source identity、Workspace、Policy、模型工具循环、命令执行、编辑、Candidate、Apply 和底层持久化。

## 2. 核心对象与状态

```text
Workspace (source)
  └── Task (长期上下文)
        ├── Run 1..N（顺序执行，最多一个 active）
        │     ├── tool operations
        │     └── pending Approval 0..1
        ├── Workspace Revision 0..1 current
        └── current Changes 0..1
              ├── immutable Candidate / ChangeSnapshot
              └── fixed Test evidence
```

Task 状态只描述稳定生命周期：

```text
open / changes_ready / completed / abandoned
```

Run 状态承载短暂与失败状态：

```text
running / waiting_for_approval / cancelling /
completed / cancelled / failed / interrupted
```

Run phase 只用于进度展示：

```text
starting / exploring / preparing_workspace /
testing / editing / verifying / reviewing
```

Workspace Revision 和 Candidate 保持独立 Domain 状态，不压进 Task enum。一个调整 Run 启动时，旧 Changes 继续是 current；只有新 Candidate 成功冻结后才 supersede 旧版本。调整 Run cancelled/failed/interrupted 时，Task 仍为 `changes_ready`。

## 3. 并发与幂等

v0.5 采用最小规则：

- 每个 Task 最多一个 active Run；
- 每个 Runtime 进程全局最多一个 active Run，不提供后台队列；
- 不同 Task 可以同时查看和恢复，但并发启动返回 `runtime_busy`；
- 一个 Task 不并行创建多个 Workspace Revision；
- Accept 使用 source workspace 级互斥，并继续执行 Domain 的 source recheck；
- active Run 期间允许只读 Snapshot 查询；
- Approval、Stop、Accept 和 Abandon 按目标 identity 串行处理。

幂等规则：

- 传输重试通过 operation identity 去重；
- 重复 Stop 返回同一个取消结果；
- Approval 重复提交相同决定返回原结果，相反决定返回冲突；
- 已 applied Changes 重复 Accept 返回既有 ApplyResult，不重复应用；
- 已 abandoned Changes 重复 Abandon 返回既有结果；
- 旧或 superseded `changes_id` 返回 `changes_not_current`；
- 幂等记录不能替代 Domain 的 source/Candidate 复检。

## 4. 稳定 Application Errors

```text
runtime_not_ready
runtime_busy
workspace_not_supported
workspace_unavailable
provider_not_configured
provider_error
task_not_found
invalid_task_state
run_not_found
run_already_active
run_not_active
approval_not_pending
approval_already_resolved
changes_not_found
changes_not_current
source_changed
source_dirty
workspace_revision_unavailable
operation_cancelled
operation_interrupted
policy_denied
internal_error
```

错误包含稳定 category、用户可读 message、`retryable`、关联 identity、建议动作和可选 `diagnostic_id`。默认不暴露堆栈、内部路径或 secret。

## 5. Application Use Cases

### 5.1 Workspace 与 Task

#### `initialize_workspace`

- **Intent**：连接建立后识别 source workspace，并报告产品能力。
- **Input**：`workspace_root`，可选 provider selection。
- **Preconditions**：Runtime 进程已启动，path 可解析。
- **Result**：`RuntimeStatus`。
- **Errors**：`runtime_not_ready`、`workspace_unavailable`、`workspace_not_supported`。
- **Side effects**：建立进程内 workspace scope，执行只读 capability probe；不创建 Task、Run 或 worktree。进程 handshake 仍属于 Server。

#### `get_runtime_status`

- **Intent**：连接建立后刷新 Runtime、workspace 和 provider 的当前 readiness。
- **Input**：无，使用 initialized workspace scope。
- **Preconditions**：Application 对象已创建；workspace 可以尚未 ready。
- **Result**：`RuntimeStatus`。
- **Errors**：只有无法形成结构化状态时返回 `internal_error`；普通未就绪作为 View 状态返回。
- **Side effects**：纯读取，不启动 provider 调用、不创建 Task 或 Revision。

#### `list_tasks`

- **Intent**：列出当前 workspace 的历史 Task。
- **Input**：initialized workspace scope，可选分页。
- **Preconditions**：Application 已初始化。
- **Result**：`TaskSummary[]`。
- **Errors**：`runtime_not_ready`、`workspace_unavailable`。
- **Side effects**：纯读取；单个损坏 Session 形成 warning，不阻断其他结果。

#### `create_task`

- **Intent**：创建长期 Task，并以第一条消息启动首个 Run。
- **Input**：`message`、可选 provider/model、operation identity；不接受 mode/worktree。
- **Preconditions**：Runtime ready，workspace 可只读，全局无 active Run。
- **Result**：已持久化 `TaskSnapshot`，其中包含首个 `RunView`。
- **Errors**：`runtime_busy`、`workspace_unavailable`、`provider_not_configured`。
- **Side effects**：创建 Session/Task、记录 source observed identity、持久化消息、启动 Run 和调用模型；默认不创建 Revision。Run 启动失败时 Task 仍保留为 `open`。

#### `get_task_snapshot`

- **Intent**：恢复或刷新 Task 的权威产品视图。
- **Input**：`task_id`。
- **Preconditions**：无运行前置条件。
- **Result**：`TaskSnapshot`。
- **Errors**：`task_not_found`、`internal_error`。
- **Side effects**：纯读取；不调用模型、不迁移 workspace、不执行工具。

#### `resume_task`

- **Intent**：让新 Runtime 进程接管历史 Task 并确认允许操作。
- **Input**：`task_id`。
- **Preconditions**：Task 存在，本进程没有该 Task 的 active Run。
- **Result**：`ResumeResult`，内含更新后的 Snapshot。
- **Errors**：`task_not_found`、`workspace_unavailable`。
- **Side effects**：把旧进程遗留 active Run 记为 `interrupted`；已有 Revision 时调用 Domain inspect；旧 pending Approval 失效。不调用模型、不自动创建 Revision、不 Apply。

### 5.2 Run 与 Approval

#### `start_run`

- **Intent**：在 `open` Task 中发送新消息并开始一轮工作。
- **Input**：`task_id`、`message`、operation identity。
- **Preconditions**：Task 为 `open`，Task/Runtime 均无 active Run，Resume 允许继续。
- **Result**：已启动的 `RunView`。
- **Errors**：`invalid_task_state`、`run_already_active`、`runtime_busy`、`provider_not_configured`、`workspace_revision_unavailable`。
- **Side effects**：持久化消息和 Run、调用模型；可以读取 source/Revision，并在需要时协调 Revision 物化、Approval、pytest、编辑和 Candidate freeze。客户端不提交模型历史。

#### `cancel_run`

- **Intent**：Stop 当前 Run，保留 Task 和最近稳定 Changes。
- **Input**：`task_id`、`run_id`、operation identity。
- **Preconditions**：`run_id` 是当前 active Run。
- **Result**：`RunView`，可以先为 `cancelling`，最终为 `cancelled`。
- **Errors**：`run_not_found`、`run_not_active`、`operation_interrupted`。
- **Side effects**：停止后续调度；请求取消模型或忽略迟到结果；请求终止子进程；不删除 Revision 或旧 Changes。已完成原子编辑以 Domain receipt 为准，不由 Application 逆向回滚。

#### `decide_approval`

- **Intent**：批准一次或拒绝当前 Run 的固定风险操作。
- **Input**：`task_id`、`run_id`、`approval_id`、`approve_once | reject`、operation identity。
- **Preconditions**：Run 为 `waiting_for_approval`，Approval 属于该 Run 且 pending。
- **Result**：`ApprovalView` 与更新后的 `RunView`。
- **Errors**：`approval_not_pending`、`approval_already_resolved`、`run_not_active`、`policy_denied`。
- **Side effects**：持久化决定；批准 pytest 时可以触发 source recheck、首次 Revision 物化和固定命令；拒绝时产生配对 tool result。

不提供单独 `get_pending_approval`：`TaskSnapshot.pending_approval` 已覆盖展示和恢复。

### 5.3 Changes

#### `get_changes`

- **Intent**：获取当前整组待审查修改。
- **Input**：`task_id`，可选 opaque `changes_id`。
- **Preconditions**：Task 存在 current Changes。
- **Result**：`ChangesView`。
- **Errors**：`changes_not_found`、`changes_not_current`。
- **Side effects**：纯读取；Domain 重新验证内部 Candidate，不从当前 worktree 重建 diff。

#### `continue_changes`

- **Intent**：对当前 Changes 提出调整并启动新 Run。
- **Input**：`task_id`、`changes_id`、`message`、operation identity。
- **Preconditions**：Task 为 `changes_ready`，Changes current，Revision 可继续，Task/Runtime 无 active Run。
- **Result**：新 `RunView`；旧 Changes 在新 Candidate 成功冻结前继续 current。
- **Errors**：`changes_not_current`、`workspace_revision_unavailable`、`run_already_active`、`runtime_busy`。
- **Side effects**：调用模型，在现有 Revision 编辑和测试；只有新 Candidate 成功冻结后旧 Candidate 才 superseded。调整 Run cancelled/failed/interrupted 时旧 Changes 继续 current。

#### `accept_changes`

- **Intent**：把当前整组 Changes 应用到 source。
- **Input**：`task_id`、`changes_id`、operation identity；不接受 patch/hash/path。
- **Preconditions**：Task 为 `changes_ready`，Changes current，无 active Run，取得 source lock。
- **Result**：`ApplyResult` 和终态 `TaskSnapshot`。
- **Errors**：`changes_not_current`、`source_changed`、`source_dirty`、`policy_denied`、`operation_interrupted`。
- **Side effects**：调用 Domain 验证 Candidate、preflight、两次检查 source、修改 source、保存 receipt、Task 置 `completed`；成功后可触发保守 cleanup。这是唯一修改正式 source 的 Use Case。

#### `abandon_changes`

- **Intent**：放弃整组 Changes 并终结 Task。
- **Input**：`task_id`、`changes_id`、operation identity。
- **Preconditions**：Task 为 `changes_ready`，Changes current，无 active Run。
- **Result**：终态 `TaskSnapshot`。
- **Errors**：`changes_not_current`、`invalid_task_state`。
- **Side effects**：记录 reject/abandon，Task 置 `abandoned`；不修改 source；worktree/artifacts 进入 retention 流程。

### 5.4 不提供的公共 Use Cases

- `cleanup_task`：普通用户不管理 worktree；终态 cleanup 是内部维护，诊断 CLI 可保留旧命令。
- `retry_run`：用户显式重新发送消息，不自动重放未知执行。
- `create_workspace_revision`：物化是 Run/Approval 内部编排，不是用户目标。
- partial accept / compose patch：Changes 是整体 review unit。

## 6. Stable View Models

### `RuntimeStatus`

```text
runtime_version
status: starting | ready | degraded | unavailable
workspace_id
workspace_display_name
workspace_support: readonly_available / coding_available
source_basis: saved_files_on_disk
provider_status[]
warnings[]
allowed_actions[]
```

### `TaskSummary`

```text
task_id
title
task_status
updated_at
last_run_status?
has_current_changes
warning_count
allowed_actions[]
```

### `TaskSnapshot`

```text
task_id
title
task_status
source_display
source_basis: saved_files_on_disk
active_run: RunView?
last_run: RunView?
pending_approval: ApprovalView?
current_changes: ChangesSummary?
latest_test: TestView?
assistant_messages[]        有限产品时间线
warnings[]
allowed_actions[]
last_event_seq
```

### `RunView`

```text
run_id
status
phase
trigger: user_message | continue_changes
started_at
finished_at?
activity_summary?
error: UserFacingError?
allowed_actions[]
```

### `ApprovalView`

```text
approval_id
run_id
kind: execute_repository_code
title
operation_summary
targets[]
cwd_display
timeout_seconds
risk_summary
allowed_decisions: approve_once / reject
status: pending | approved | rejected | expired
```

创建 worktree 不是独立 Approval kind；Accept Changes 是独立 Use Case。

### `TestView`

```text
test_id
kind: pytest
status: passed | failed | timed_out | cancelled | interrupted | not_run
targets[]
exit_code?
duration_ms?
summary
workspace_changed
side_effect_files[]
completed_at?
```

`passed` 必须来自完整 command receipt，不能来自 Agent 文本。

### `ChangesSummary` / `ChangesView`

```text
ChangesSummary:
  changes_id
  summary
  changed_files[]
  test_status
  warning_count

ChangesView:
  changes_id
  summary
  changed_files[]
  diff
  latest_test: TestView
  workspace_side_effects[]
  warnings[]
  allowed_actions[]
```

`changes_id` 是防止操作旧版本的 opaque identity，不暴露 Candidate hash、artifact 或 receipt path。

### `ResumeResult`

```text
status: ready | attention_required | unavailable
task: TaskSnapshot
workspace_revision: none | available | unavailable
interrupted_run_id?
warnings[]
allowed_actions[]
```

### `ApplyResult`

```text
status: applied | rejected | source_changed | source_dirty | failed | interrupted
task_id
changes_id
changed_files[]
summary
completed_at
error: UserFacingError?
```

### `UserFacingError`

```text
category
message
retryable
related_task_id?
related_run_id?
related_changes_id?
suggested_actions[]
diagnostic_id?
```

View 不返回 session root、worktree path、Candidate hash、receipt path、provider raw response 或 chain-of-thought。

## 7. `allowed_actions`

稳定动作集合：

```text
create_task
open_task
resume_task
send_message
cancel_run
approve_operation
reject_operation
view_changes
accept_changes
continue_changes
abandon_changes
```

| 当前事实 | 主要 allowed actions |
| --- | --- |
| Runtime ready、全局无 active Run | `create_task` |
| Task `open`、已 Resume、全局无 active Run | `send_message` |
| Run `running` / `waiting_for_approval` | `cancel_run` |
| pending Approval | `approve_operation`、`reject_operation` |
| `changes_ready`、无 active Run、Changes current | `view_changes`、`accept_changes`、`continue_changes`、`abandon_changes` |
| 历史 Task 尚未被本进程接管 | `open_task`、`resume_task` |

客户端用它决定展示和 enabled 状态，但 `allowed_actions` 不是授权令牌。操作请求到达时 Application 和 Domain 必须重新检查全部事实。

## 8. Application Event Model

事件 envelope：

```text
event_id
task_id
run_id?
seq                 Task 内单调递增
type
timestamp
payload             最小 UI 数据
```

最小产品事件：

```text
task_created
task_updated
run_started
run_phase_changed
assistant_delta
assistant_message_completed
tool_activity
approval_requested
approval_resolved
test_completed
changes_ready
run_completed
run_cancelled
run_failed
run_interrupted
changes_abandoned
apply_completed
```

规则：

- 产品事件由 Application 映射，不等同于内部 audit event；
- `assistant_delta` 是易失的 UI 增量，完成消息持久化后才发完成事件；
- `tool_activity` 只含 label、状态和安全摘要，不透传任意 arguments；
- `test_completed.passed` 只来自完整 receipt；
- `changes_ready` 携带 Summary，完整 diff 由 `get_changes` 获取；
- Event 和 Task seq 在单 Task 写入边界内串行化；
- 客户端丢事件后重新 `get_task_snapshot`，无需重放完整 journal。

## 9. Unsaved Buffer 边界

```text
Extension editor API
  → 检测 unsaved buffer
  → Save All / Continue with saved disk / Cancel
  → 调用 Application

Application / Runtime
  → source_basis = saved_files_on_disk
  → 独立执行磁盘 source identity / Git clean 检查
```

Application 不接收 unsaved 内容，也不把客户端的“editor clean”声明当作安全事实。Accept 前 Extension 应阻止 unsaved buffer，但 Domain 仍独立 fail closed。保存全部后产生的 Git dirty 是另一项磁盘事实。

## 10. Traceability Matrix

| User Story | Application Use Case | View/Event | Runtime capability |
| --- | --- | --- | --- |
| US-00 启动 | `initialize_workspace`、`get_runtime_status`、`list_tasks` | `RuntimeStatus`、`TaskSummary` | workspace probe、Session discovery |
| US-01 创建/探索 | `create_task` | `TaskSnapshot`、`run_started` | SessionStore、source identity、AgentRunner、read tools |
| US-02 进度 | `get_task_snapshot` | Snapshot、Events | Session events、receipts |
| US-02A 继续 Task | `start_run` | `RunView` | ContextBuilder、AgentRunner |
| US-02B Stop | `cancel_run` | `RunView`、`run_cancelled` | Runner cancellation、process termination |
| US-03 按需执行 | `start_run` / `decide_approval` 编排 | phase events | source recheck、GitWorktreeManager、runner rebuild |
| US-04 Approval | `decide_approval` | `ApprovalView` | Policy、Approval bridge、CommandService |
| US-05 查看 Changes | `get_changes` | `ChangesView` | CandidateService.load、test receipts |
| US-06 接受 | `accept_changes` | `ApplyResult` | CandidateService.apply、source preflight |
| US-07 调整 | `continue_changes` | `RunView`、`changes_ready` | Revision、AgentRunner、Candidate freeze |
| US-08 放弃 | `abandon_changes` | `TaskSnapshot` | Candidate reject、retention marker |
| US-09/10 发现和恢复 UI | `list_tasks`、`get_task_snapshot` | Summary、Snapshot | Session discovery、projector |
| US-11/12 Resume | `resume_task` | `ResumeResult` | context recovery、worktree inspect |
| US-13 恢复 Changes | `resume_task`、`get_changes` | Resume、Changes | immutable Candidate artifacts |
| US-14 中断 | `resume_task` | interrupted `RunView` | persisted operation evidence |
| US-14A unsaved | Extension preflight；无独立 Use Case | `source_basis`、warnings | disk source checks |
| US-15 source 变化 | `resume_task` / `accept_changes` | warnings、ApplyResult | source identity、fail closed |
| US-16 失败 | 所有命令 Use Case | `UserFacingError`、failure events | exceptions/receipts |
| US-17 兼容 | Server handshake + `initialize_workspace` | `RuntimeStatus` | version/capability provider |

## 11. 对 v0.4 Runtime 的影响

### 11.1 可以直接复用

- `safety/`、`tools/`、`model_gateway/`；
- `runtime/policy.py`；
- `runtime/command_service.py`、`local_executor.py`、`artifacts.py`；
- `runtime/edit_service.py`、`runtime/candidate.py`；
- `workspace/git_worktree.py`；
- `session/` 现有 metadata/events/artifacts 作为迁移基线。

### 11.2 需要 Application adapter/orchestration

- 从 `cli.py` 提取 session 创建、resume inspection、runner 组装、Candidate 查找和 Apply/Reject 编排；
- 用 projector 从 Domain facts 构建 Snapshot/View；
- 用 Approval bridge 替代 core 对 Console TTY 的依赖，CLI 仍可作为 adapter；
- 把内部 audit event 映射为产品事件；
- 在 Run 边界执行 source → Revision transition，并重建绑定 active root 的 Runner/Registry。

### 11.3 真正需要修改 Runtime core

| 当前实现 | 新要求 | Gap | 最小迁移 |
| --- | --- | --- | --- |
| Session 创建时固定 mode，execution 立即建 worktree | Task 默认只读、按需 Revision | 缺少显式 workspace transition | metadata 增加可选 Revision；Application 在 Run 边界创建并重建 Runner，不在活跃 loop 静默切根 |
| `run_turn` 同步执行完整循环 | Run 可 Stop、等待外部 Approval、发进度 | 无 run identity/cancel/event sink | 增加 `run_id`、event sink、协作取消检查；先使用 worker thread，不全面改 asyncio |
| ApprovalGate 同步返回，ConsoleGate 读 TTY | VS Code 异步决定 | 无持久化 pending Approval | 增加 Application Approval bridge，Command/Policy 保持不变 |
| SessionEvent 无 seq/run_id | Snapshot/Event 可恢复关联 | 缺少事件游标 | 增量增加 event id、run correlation、seq，兼容旧 event |
| SessionStore 多文件直接写 | Run/Approval/Accept 需串行幂等 | 并发写不原子 | 进程内单写者/Task lock、原子 meta replace、operation identity；暂不换数据库 |
| denial 只记 `tool_denied` | provider 上下文必须闭合 | 悬空 tool call | deny 映射成配对 tool result 并补测试 |
| Executor 只有 timeout 终止 | Stop pytest | 缺少外部 cancel | 增加 cancellation token，复用现有 process-tree termination |
| Candidate 主要由 CLI 展示 | Changes 是产品 review unit | CLI/artifact 不适合客户端 | CandidateService 不变，增加 opaque `changes_id` 和 View projector |

### 11.4 暂不修改

不引入数据库、HTTP、WebSocket、通用消息总线、partial accept、任意 shell、dirty source snapshot、自动 merge/rebase、多 Revision 或主机 sandbox。

## 12. 放弃的方案

- Server 直接复制 CLI 业务：会形成两套创建、Resume 和 Apply 逻辑；
- Task/Run 合成巨大状态机：Stop 或失败会错误终结 Task；
- 多 Task 并行或全局队列：当前同步 Runner 收益不足且难验证；
- 为创建 worktree 单独 Approval：内部隔离准备不是独立用户风险；
- 客户端提交 patch hash/worktree/receipt：泄漏实现并信任陈旧客户端；
- 只有 Events：断线恢复会迫使客户端重放 journal；
- 只有 Snapshot：无法自然展示实时 Agent 进度与 Approval。
