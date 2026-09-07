# VS Code 产品层 Technical Design v1.1

状态：**Phase 0～5 已实现。** 本文保留批准时的设计与分阶段验收，用于追溯；当前协议、交互和人工验收分别以
[Product RPC](PRODUCT_RPC.md)、[会话时间线 UX](CHAT_TIMELINE_UX_DESIGN.md)和
[产品验收](PRODUCT_ACCEPTANCE.md)为准。

## 1. 修改摘要

本版不改变已确定的总体架构，只收敛产品状态和实施边界：

- 将单一 job 状态拆为客观的 `ExecutionState` 与基于已有事实计算的 attention/actions；不判断“用户任务已完成”。
- 明确 Continue 是继续同一 UserTurn，Resume 是重新打开并恢复 Session；首版不定义 Pause。
- Stop 只承诺在安全边界停止，对模型调用、短 Tool 和长 Command 分别定义保证。
- 将 Progress Projector 收敛为 `ActivitySummaryProjector`，只汇总已经发生的事实，不预测下一步。
- pending approval 只在当前 Runtime 进程内等待；重启后旧请求失效，不恢复执行栈或授权。
- JSON-RPC loop 与 Runner worker 分离，使运行或等待审批时仍能响应读取、Stop 和审批请求。
- UI 使用默认位于 Secondary Side Bar 的容器、Session Detail Webview View 和 VS Code 原生 Diff Editor；要求
  VS Code 1.106 或更高版本。
- Phase 1 改为纯只读 Product Shell；Run/Continue、Changes、Human Intervention 分阶段进入。

## 2. 目标与不变量

本设计将 Runtime 收尾为“可观测、可干预的本地 Coding Agent”产品原型，支撑 1～3 分钟 Demo：
打开或创建任务、观察活动、审查修改和验证，并执行必要的人为控制。

以下不变量贯穿全部 Phase：

- Python Runtime 是唯一业务状态写者；Extension 不直接读写 `session.json`、`events.jsonl` 或 Git worktree。
- `session.json`、append-only Events 和 Git worktree 仍是权威事实；Product Read Model 只做投影。
- Extension 不能绕过 ToolRegistry、Policy、Approval、PathGuard、Candidate 或 Validation identity。
- notification 只降低延迟；丢失后通过 `after_seq` 补读。
- Runtime 统一计算 `AvailableActions`，UI 不复制安全和 lifecycle 规则。
- 不新增 Task 模型、数据库、durable workflow engine 或第二套 Runtime。
- Runtime 能力扩张冻结：不做 Multi-Agent、Memory、MCP、Planner、Context 大改、RepoMap/RAG、远程执行或
  proposed Agent Sessions API。

## 3. 现状审计与最小缺口

| 产品需要 | 已有事实/入口 | 最小缺口 |
| --- | --- | --- |
| Session 列表 | `SessionStore.list_sessions/list_all_sessions`、metadata | 产品摘要、changed file count |
| 对话与历史 | `read_events()`；`seq/ts/turn_id/model_step_id` | Product DTO、增量 API |
| Activity | tool/command/edit/workspace/approval Events | 有界、用户语言的事实摘要 |
| 自动切片 / 本轮扩额 | `AgentRunner.continue_turn()`、`execution_slice_exhausted`、turn budget Events | 自动调度与显式 budget mutation |
| Reopen/Resume | metadata、Events、Git audit；CLI 已有恢复逻辑 | 从 CLI 最小提取应用服务 |
| Changes | Git worktree、`CurrentChangesService.preview()` | changed-files/diff read model |
| Accept/Discard | `CurrentChangesService.accept/discard` | RPC adapter，不改变既有语义 |
| Validation | command Events、`validation_completed`、当前 tree 匹配 | Validation View 与 stale 投影 |
| Approval | ApprovalGate、Policy/Permission、审批 Events | 当前进程内异步桥接与 RPC resolve |
| Live update | 进程内 `progress_callback` | 统一 Event publisher、补读机制 |
| Stop | 尚无产品控制入口 | cooperative cancellation 与事实事件 |

Application Service 只封装已有 create、reopen/recovery、run、continue、changes、validation、accept/discard
orchestration。它不得借接入 UI 重新设计 Session、Runner、Candidate、Workspace 或 Validation 语义。

## 4. 完整状态与生命周期定义

### 4.1 Execution State

`ExecutionState` 只回答“Runtime 当前是否正在执行或等待什么”，不表达任务结果：

| 状态 | 定义 | 进入 | 离开 |
| --- | --- | --- | --- |
| `idle` | 当前 Session 没有 active execution | 初始、reopen recovery 后、execution 结束 | `session/run` 或合法 `session/continue` |
| `running` | Runner worker 正在处理当前 execution | job 获得 Session execution mutex | 等待审批、收到 Stop、或本次 execution 返回 |
| `awaiting_approval` | 当前 worker 在进程内等待一个审批决定 | ApprovalGate 产生待决请求 | approve/reject、Stop 或进程退出 |
| `stopping` | 已接受 Stop，等待到达并确认安全停止边界 | `session/stop` | 写入 stop acknowledgement 后回到 `idle` |

没有 `paused/completed/failed` Runtime 状态。execution 返回、slice exhaustion、turn termination、命令失败、
修改待审查等均由已有 Event 和派生摘要表达。UI 可显示“Validation failed”或“Step budget reached”，但不能
据此宣称 Agent 已完成或未完成整个用户任务。

每个 Session 最多一个 active execution。`session/run`、`session/continue`、Accept/Discard 等 mutation 按
Session 串行化；互斥冲突返回明确错误，不排队制造隐式工作流。

### 4.2 Attention 与 AvailableActions

Product Read Model 基于已有 metadata、Events、Git 状态和当前进程状态计算，例如：

- `can_continue`：最新 UserTurn 未闭合，且符合 `AgentRunner.continue_turn()` 的既有条件。
- `changes_ready`：存在可信 pending changes，可供 review。
- `validation_passed/current`：成功 evidence 仍匹配当前 workspace/base/tree。
- `validation_failed`：最近 validation command 失败；它不是任务失败。
- `validation_stale`：历史验证不再匹配当前修改。
- `needs_attention`：审批等待、tainted、恢复问题、命令/turn 终止等需要用户查看的事实摘要。
- `recovery_required`：直接来自 Runtime workspace state，并关闭危险操作。

这些不要求成为一个互斥枚举；同一 Session 可以同时 changes ready、validation stale、can continue。
`AvailableActions` 至少包含 `canRun`、`canContinue`、`canIncreaseBudget`、`canStop`、`canResolveApproval`、`canViewChanges`、
`canAcceptChanges`、`canDiscardChanges`，并附不可用原因。tainted 仅允许安全检查和 Discard；
recovery required 继续 fail closed。

### 4.3 Continue、Resume 与 Stop

当前实现已从批准时的手动 Continue 收敛为自动切片：`execution_slice_exhausted` 后 Product 在同一 job 中调用
`AgentRunner.continue_turn()`，不追加或伪造用户消息，也不把 execution state 切回 idle。达到 UserTurn 总预算
时才等待用户；用户明确输入 `+N` 追加步数或 N 设置新的总上限后继续。扩额没有默认值，只作用于当前 UserTurn。
已由 `assistant_message` 或 `turn_terminated` 闭合的 UserTurn 不可继续或扩额。

**Resume** 不是 execution command 或状态。产品中的 Resume 就是重新打开历史 Session：加载持久状态、执行
recovery audit、重建 Product Read Model。打开后若 `can_continue=true`，UI 才显示 Continue。

**Stop** 是用户请求当前 execution 在安全边界终止，必须产生 `execution_stop_requested` 和
`execution_stop_acknowledged` 事实。acknowledgement 至少记录 execution/job ID、请求时间、确认时间和停止边界。
收到请求后不得启动新的 Agent step 或 Tool Call；Stop 与自然的 `execution_slice_exhausted` 分开表达。

首版 Stop 保证：

- Model call：不强制取消正在进行的 HTTP 请求；返回后在下一安全边界停止，不执行其新 Tool Call。
- 短 Tool Call：允许当前调用完成，之后停止，不启动下一个调用。
- 长 Command：只有 executor 已能可靠终止 subprocess/process group/Docker container 时才主动终止；在实现和
  测试证明前，产品语义退化为“当前命令结束后停止”。不得为此重构 Model Gateway 或引入复杂抢占。

首版没有 Pause，也不承诺任意时刻冻结进程并原地恢复。Stop 会闭合 UserTurn，之后由新用户消息开始下一轮；
只有处于 `turn_budget_exhausted` 等待状态的未闭合 UserTurn 才允许用户扩额。

## 5. Activity Summary（UI 可命名为 Task Progress）

Runtime 使用 `ActivitySummaryProjector`，不使用计划型 Progress 模型。它只从 Events 和 Git 状态汇总：

- `currentActivity`：如 Inspecting repository、Editing `parser.py`、Running validation、Inspecting test failure；
  必须对应当前或最后一个真实 Event。
- `recentMilestones`：如 Modified `parser.py`、Ran `pytest`、2 tests failed；仅陈述已发生结果。
- `counters`：inspected files、changed files、commands executed 等可确定统计。
- `recentActivity`：默认压缩的 Activity items，可按 Event 展开原始、有界详情。

不提供 `next`，不预测下一步，不推断 Diagnose/Plan，不将模型自然语言自动升级为 Runtime 任务阶段。UI 标题
可以是 Task Progress，但其数据契约仍是事实型 Activity Summary。

## 6. Pending Approval v1

- approval request 继续写持久 Event；当前 Runtime 进程内保存带 `approvalId`、`jobId` 和冻结请求摘要的待决项。
- Extension 通过 notification 或 `session/get` 获得当前 pending approval，并用 `approval/resolve` 决策。
- 决策回到 ApprovalGate/Policy 流程；执行前继续做 stale、workspace、permission/resource recheck。
- Stop 可以使当前待决请求 aborted；reject/aborted 均不得留下授权。
- Runtime 或 Extension 退出时，旧 pending approval 不继承为授权，也不原地恢复执行栈。reopen 时从未配对的历史
  request 投影为 aborted/stale 诊断；后续 Continue 再次触发敏感操作时必须重新申请。

这不是跨重启 durable approval workflow。

## 7. Runtime ↔ Extension 通信边界

### 7.1 传输与并发模型

Extension 启动并监管本地 Python Runtime 子进程，使用 JSON-RPC 2.0 over newline-delimited stdio；stdout
只承载协议，日志写 stderr。`initialize` 协商 `protocolVersion`、Runtime 版本和 capability flags。

RPC read/dispatch loop 不得被 Runner 或 ApprovalGate 阻塞。首版采用一个持续响应的 RPC loop，加后台
worker/thread/task 执行 Runner；approval 使用进程内 condition/future 将决定交回 worker。即使 job 正在运行
或 awaiting approval，RPC 仍必须响应：

- `session/get`
- `session/events`
- `session/stop`
- `approval/resolve`
- `changes/get`

每 Session 一个 execution mutex，mutation 经 Session 级串行化；只读查询读取一致快照。Runtime 保持唯一
writer，不引入数据库、多客户端调度或常驻 daemon。若未来确有多客户端需求，再让相同 Application Service
适配 HTTP/SSE，不在首版同时实现两套传输。

### 7.2 方法

| 方法 | 类型 | 语义 |
| --- | --- | --- |
| `initialize` | read/control | 协议握手与 capabilities |
| `session/list` | read | workspace 可过滤的 `SessionSummary[]` |
| `session/create` | mutation | 创建 Session，不预建 worktree |
| `session/get` | read | metadata、ExecutionState、attention、Activity Summary、摘要和 actions |
| `session/events` | read | `afterSeq/limit` 增量历史与 `lastSeq` |
| `session/run` | mutation | 追加新 UserTurn 并启动 execution，返回 `jobId` |
| `session/continue` | mutation | 恢复中断且仍有剩余预算的同一 UserTurn，返回 `jobId` |
| `session/increaseBudget` | mutation | 用户明确追加步数或设置本轮新总上限，并继续同一 UserTurn |
| `session/stop` | control | 幂等请求当前 job 停止，返回 accepted/current state |
| `approval/resolve` | control | 对当前进程内、匹配 ID 的 pending request approve/reject |
| `changes/get` | read | changed files、stat、有界 diff 或 diff document handles |
| `changes/accept` | mutation | 复用冻结、复检、三方合并 |
| `changes/discard` | mutation | 复用 fail-closed discard |

`changes/get` 在 running/awaiting approval 时可响应，但只返回调用时的一致快照；Accept/Discard 必须等待没有
active execution，并由 `AvailableActions` 限制。

### 7.3 Notifications 与恢复

通知至少包含 `session/event`、`session/executionStateChanged`、`approval/requested`。通知携带 session ID、
Event seq（适用时）和最小 payload。客户端记录最后确认的 `seq`；重连调用 `session/get`，再用
`session/events(afterSeq)` 补漏。notification 乱序或丢失不能改变最终视图。

进程重启后 ExecutionState 从 `idle` 开始，再结合持久事实投影 attention；旧 running/awaiting approval 不会
复活。历史未确认 stop/approval 显示为上次执行被中断的诊断事实，而非活跃控制项。

## 8. Product Read Model

首版 DTO：

- `SessionSummary`：title、workspace label、execution state/attention summary、last active、changed file count。
- `SessionDetail`：conversation、Activity Summary、recent activity、changes/validation summary、pending approval、
  `AvailableActions`、last seq。
- `ActivityItem`：用户语言 summary、timestamp、kind/status，可选 bounded raw detail 与关联 Event seq。
- `ChangesView`：changed files、add/delete stat、snapshot identity、diff handles/content。
- `ValidationView`：command、pass/fail、exit code、duration、bounded failure summary、是否适用于当前 changes。

Candidate revision、Validation Evidence、workspace fingerprint/tree/base commit/patch SHA 留在 Runtime；UI 分别
显示为 Changes、Validation、Needs attention 等普通语言。Session ID 和内部 Event type 只在诊断详情出现。

## 9. 首版 VS Code UI 信息架构

会话区的详细显示逻辑以已经落地的
[会话时间线 UX Design](CHAT_TIMELINE_UX_DESIGN.md) 为准；其核心方向是隐藏式 History、单一 UserTurn
timeline、折叠工具活动和轮次末尾 Changes/Validation，而不是固定的多栏目 dashboard。

### 9.1 Secondary Side Bar

使用单一 CodeAgent Webview View，View Container 通过稳定的 `viewsContainers.secondarySidebar` 贡献点默认位于
右侧 Secondary Side Bar，让左侧 Explorer 与右侧 Agent 同时可见。Header 提供 New Task 与 History；历史列表
平时隐藏，按需覆盖展开。选择历史 Session 后立即回到当前会话时间线。内部 ID/provider/fingerprint 不占主视图。

### 9.2 Session Detail

Session Detail 是按 `turn_id` 与 Event seq 组织的单一时间线：右侧用户气泡、无气泡的 Agent Markdown、
自然穿插的折叠工具活动，以及每轮末尾的 changed files、validation 与 attention。它不再拆成固定的
Conversation、Recent Activity、Changes、Validation 栏目。

实施阶段中 Phase 1 只读，Phase 2 加入任务输入、Run/Continue 和 live activity，Phase 4 加入 Stop/Approval。running 中的
新指令首版不注入当前上下文：UI 要求用户先 Stop，收到 acknowledgement 后作为新的 UserTurn 发送。

VS Code 仍允许用户移动 View Container 并记住自定义位置；旧版升级后可用 **View: Reset View Locations** 恢复
右侧默认值。Extension 不使用 Webview Panel 占据 Editor，也不在激活时调用命令覆盖用户已经保存的布局。

### 9.3 Diff

优先使用 VS Code 原生 Diff Editor。Extension 通过受控的 virtual document/content provider 展示 Runtime
返回的 baseline/current snapshot，不直接打开或解析受管 worktree；不自研 Diff Viewer。

## 10. Phase 计划

### Phase 0：现状审计

- 目标：冻结边界，完成状态、事件、控制和缺口清单。
- 修改范围：TD、计划与导航；不改 Runtime。
- 验收：所有 UI 信息可追溯到已有事实或明确缺口；通过本 TD 自检。
- 不做：协议/UI 代码、Runtime 扩张、正式评测。

### Phase 1：Read-only Product Shell

- 目标：Extension 可启动 Runtime 并只读浏览当前和历史 Session。
- 修改范围：最小 Application Service、Product DTO/projector、JSON-RPC stdio、initialize、session/list/get/events、
  Sidebar、只读 Session Detail、reconnect/after_seq、契约与 CLI 回归测试。
- 验收：不能从 UI 启动 job；当前/历史 Session 事实正确展示；断线补读无遗漏/重复副作用；stdout 无日志污染。
- 不做：run/continue、live notification、Stop、Approval、Changes mutation。

### Phase 2：Run / Continue + Live Activity

- 目标：从 UI 启动新 UserTurn、继续同一 UserTurn，并观察实时事实活动。
- 修改范围：session/run/continue、ExecutionState、background worker、live Event notification、
  ActivitySummaryProjector、单 Session execution mutex。
- 验收：RPC 在 running 时仍响应读取；Continue 不新增 user_message；并发 execution 被拒绝；无完成判定或 next。
- 不做：Pause、Stop、Approval UI、Changes mutation。

### Phase 3：Changes + Validation

- 目标：审查 pending changes 和验证事实，并安全 Accept/Discard。
- 修改范围：changes/get、原生 Diff provider、ValidationView、stale 投影、Accept/Discard adapter。
- 验收：文件/stat/diff 与 Git 一致；validation 显示 current/stale；Accept/Discard 保持 identity 和 fail-closed 测试。
- 不做：自研 Diff Viewer、测试充分性判断、自动 merge conflict 处理。

### Phase 4：Human Intervention

- 目标：加入有界 Stop、进程内 Approval、历史 Session reopen/recovery 和最终 actions 规则。
- 修改范围：cooperative cancellation、stop facts、approval bridge/resolve、recovery Application Service、
  AvailableActions、running 中新指令的 Stop-then-new-turn UX。
- 验收：各 Stop 边界与文档一致；Stop 后不启动新 step/tool；RPC 等待审批时仍响应；重启不恢复旧授权或执行栈；
  Resume 与 Continue 在 UI/协议中无重叠含义；tainted/recovery 限制正确。
- 不做：任意时刻冻结、强制取消模型 HTTP、durable approval、多客户端调度。

### Phase 5：Release

**A. Product Acceptance**

- 范围：安装/启动、README、Demo fixture/脚本或媒体、完整 UI 主链路和自动化测试。
- 验收：新环境按文档运行；1～3 分钟完成打开/创建→观察→修改→验证→Diff 审查→控制/处理修改。

**B. Release Evaluation**

- 范围：版本冻结后运行一次固定 SWE-bench 集合，汇总 total/pass/fail、cost、duration、tool calls、failure categories。
- 验收：manifest、结果和失败分类可追溯；评测结果单独报告。

Release Evaluation 不作为 Product Acceptance 是否通过的条件。不为改善结果临时 prompt tuning 或扩大能力。

## 11. 保留的技术债（本轮不处理）

1. CLI 当前承载 resume recovery 与 runner orchestration；只在对应 Phase 做产品接入所需的最小提取。
2. ApprovalGate 同步阻塞；Phase 4 增加进程内 bridge，不重写为 durable workflow。
3. `progress_callback` 不是完整事件总线；Phase 2 由 append 后 publisher 补齐，不改变 Event 权威性。
4. Event type/payload 没有统一版本化 schema；先为产品消费子集定义 DTO/兼容测试，不迁移全部历史。
5. 文件 store 没有多进程写者协议；首版单 Runtime writer + Session 串行化，不引入数据库。
6. 失败 validation 需从 command events 投影；在 Product projector 统一，不复制新的权威事实。
7. Session list 的 changed file count 可能产生 Git 查询成本；Demo 规模先按需缓存，测得瓶颈后再优化。
8. 长 Command 主动终止能力需按 executor 单独验证；验证前使用“当前命令结束后停止”的保守语义。

## 12. v1.1 自检

- Runtime 是否需要猜“用户任务是否完成”：否。
- Activity Summary 是否预测下一步：否；没有 `next` 字段。
- Resume 是否与 Continue 混淆：否；前者是 reopen/recovery，后者继续同一 UserTurn。
- 是否承诺 Pause：否；首版只有 Stop 和条件满足时的 Continue。
- Approval 是否恢复跨重启执行栈：否；旧请求失效，敏感操作必须重新申请。
- 是否为 UI 设计第二套 Runtime：否；Application Service 仅封装已有 orchestration，权威状态和安全边界不变。
