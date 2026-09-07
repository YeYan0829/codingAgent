# CodeAgent 会话时间线 UX Design v1.2

## 1. 本轮结论

CodeAgent 的右侧产品层采用一个持续存在的 **Chat/Task View**，而不是“Session List + Session Detail”两个
固定上下堆叠的 View，也不把会话详情打开到中央 Editor。

- 历史 Session 是按需打开的导航浮层：点击 header 的 History 按钮展开，选择后收起。
- 主体是一条按时间顺序渲染的 UserTurn timeline，不再拆成 Conversation、Task Progress、Recent Activity、
  Changes、Validation 五个纵向栏目。
- 用户消息显示为右对齐气泡；Agent 输出使用无气泡的 Markdown 正文。
- Agent 文本、工具活动、最终回复按真实发生顺序出现；工具细节归入一个默认折叠的 activity disclosure。
- 每个 UserTurn 末尾显示该轮文件变化和验证摘要；点击文件通过 Runtime snapshot 打开 VS Code 原生 Diff。
- Runtime 只提供事实型 timeline DTO；不生成计划阶段、不预测下一步、不判断整个用户任务是否完成。

本文固化已经批准并进入实现的显示逻辑。

## 1.1 UX 收敛实现边界

当前实现补充以下产品层约束，但不新增 Runtime 领域语义：

- Webview 首次加载后通过 `productState` 增量更新，保留输入草稿、滚动位置、折叠项以及历史/配置页状态。
- Header 稳定显示 workspace、provider/model 和运行状态；Runtime、恢复与执行错误使用页内持久提示。
- 执行期间禁用 Changes 的 Accept/Discard；Approval 中 Allow/Reject 为主决策，Stop task 降为次级动作。
- Current Delivery 与历史 `Changed in this turn` 分开；验证明确显示未验证、通过且最新、失败、证据过期四种状态。
- Accept/Discard 后显示结果回执；历史列表显示 Approval、Running、Recovery 或 Changes to review 徽标。
- 未配置 API Key 时提供页内配置入口。变更凭据会重启 Runtime，因此任务执行期间禁止保存新凭据或清除凭据。

这些内容属于 Product Read Model、AvailableActions、Product Service 和 Extension 显示状态；不会绕过
Safety、Policy、Approval 或 ToolRegistry。

内部 execution slice 对用户透明并自动衔接。只有达到 UserTurn 总 ModelStep 上限时，composer 才显示预算
等待状态：当前 used/limit、`Increase budget and continue` 与 `Stop this request`。扩额输入框不预填建议值；
用户输入 `+N` 表示追加 N 步，输入 N 表示设置新的本轮总上限。取消或无效输入不发送 mutation。

## 1.2 输入、复制与 Approval 微交互收敛

- 非空消息提交后立即清空输入框并暂时禁止重复提交；Runtime 拒绝接收消息时恢复原文并重新聚焦。状态刷新不会
  把已经发送的文本重新写回输入框。
- `Enter` 发送、`Shift+Enter` 换行；输入法处于 composition 阶段时，`Enter` 不触发发送。空白输入时发送按钮
  不可用。
- 用户气泡和每段 Agent Markdown 提供 hover/focus 可见的复制按钮。复制的是原始纯文本，不是渲染后的 HTML；
  内容通过 Webview message bridge 交给 Extension Host 的 VS Code Clipboard API，并使用 `aria-live` 报告结果。
- Approval 首屏明确区分“创建隔离 worktree”和“命令增量权限”。命令权限直接展示完整的有界 command、能力、
  原因与 scope；`once` 授权按钮显示为 `Allow once`，避免与 workspace 准备授权混淆。

## 2. 调研依据

### 2.1 用户提供的 Codex 界面

截图体现的关键模式：

1. Chat history 由 header 图标临时展开，平时不占主会话空间。
2. 当前 Session 始终占据主视图；历史列表只是切换入口。
3. 用户输入用右侧气泡形成视觉锚点，Agent 内容以文档式正文向下流动。
4. 工具执行是正文中的可折叠过程，不是独立的监控面板。
5. Changed files 作为一次工作结果附在会话内容之后，并提供 Review 入口。

这里参考的是信息层级和阅读节奏，不复制 Codex 品牌、图标或内部实现。

### 2.2 VS Code 官方交互依据

- VS Code 将 Chat View 描述为位于 workspace editor tabs 旁的侧栏界面，并明确其适合 code-first workflow；
  Session history 在 compact 模式下嵌入 Chat View，可隐藏，选择 Session 后回到会话内容。来源：
  [Manage agent sessions in VS Code](https://code.visualstudio.com/docs/agents/run/sessions/manage-sessions)。
- VS Code Chat 将工具步骤放在会话流中；搜索命中折叠的 completed steps 时会展开对应部分，并支持在每次
  request 后显示 changed-files summary。来源：
  [Use chat in VS Code](https://code.visualstudio.com/docs/chat/chat-overview)。
- VS Code UX 指南建议保持 View 数量最少，并明确不应让 Activity Bar item 打开占据 Editor 的 Webview；
  View 可以由用户移动到 Secondary Sidebar。来源：
  [Views UX Guidelines](https://code.visualstudio.com/api/ux-guidelines/views) 和
  [Sidebars UX Guidelines](https://code.visualstudio.com/api/ux-guidelines/sidebars)。
- Session 集成的 `chatSessionsProvider` 仍是 proposed API；本项目继续使用独立 Webview View，不依赖该接口。
  来源：[Manage agent sessions in VS Code](https://code.visualstudio.com/docs/agents/run/sessions/manage-sessions)。

因此，CodeAgent 应保留独立技术实现，但采用已经成熟的“隐藏历史导航 + 单一会话时间线 + 折叠工具过程 +
轮次结果附件”范式。

## 3. 整体布局

CodeAgent View Container 中只保留一个自定义 Webview View：

```text
┌─────────────────────────────────────┐
│ ←  Current task title       History │  Header
├─────────────────────────────────────┤
│                                     │
│                       ┌───────────┐ │
│                       │ User text │ │  User bubble
│                       └───────────┘ │
│                                     │
│ Agent Markdown response/progress    │
│                                     │
│ ▸ Worked for 18s · 7 tool calls     │  Collapsed activity
│                                     │
│ Final Agent Markdown                │
│                                     │
│ ┌ Changed 2 files ────────────────┐ │  Turn result
│ │ parser.py                 Review │ │
│ │ tests/test_parser.py             │ │
│ └─────────────────────────────────┘ │
│ ✓ Validation passed · pytest ...    │
│                                     │
├─────────────────────────────────────┤
│ Task input / controls                │
└─────────────────────────────────────┘
```

中央 Editor 不被会话占用，只用于源码与原生 Diff。整个 CodeAgent container 可以由用户移动到 Secondary
Sidebar；Extension 不通过 proposed API 强制改变布局。

## 4. History 交互

### 4.1 默认状态

- 主视图只显示当前 Session。
- Header 左侧为返回/当前任务标题，右侧为 History 图标；不常驻 Session Tree。
- 无当前 Session 时显示首次使用欢迎页：当前 repository、已选 provider/model、隔离修改说明和单一主操作。
  已配置时主操作为 Start a new task；未配置时为 Configure model；Runtime 失败时为 Retry Runtime。
  Webview 内不重复显示外层 View Container 已经提供的 CodeAgent 标题，Refresh 收入 History。

### 4.2 展开状态

点击 History 后，在同一个 Webview 内覆盖或替换 timeline，显示：

```text
┌ Chat history ───────────────────────┐
│ Search recent tasks...              │
│                                     │
│ Today                               │
│   Fix parser null handling      3m  │
│   Explain repository structure  2h  │
│ Last 7 days                         │
│   Add validation command        4d  │
└─────────────────────────────────────┘
```

History 默认选择 `Current workspace` 并按时间分组；切换 `All workspaces` 后按 repository 分组。搜索覆盖本地
Session 摘要中的任务标题、workspace、provider/model 和状态，状态筛选提供 All、Active、Needs attention 与
Changes。其他 workspace 的条目可被搜索和查看摘要，但在当前窗口中禁用，提示用户先打开对应 workspace；
Extension Host 在读取详情前再次核对规范化 workspace 路径，不能只依赖 Webview 的 disabled 状态。

当前不索引完整消息或 Tool Observation，因此这里的“全局搜索”是 Session 摘要搜索，不宣称聊天全文或语义
搜索。“全局”限定为当前配置的 Session Root 内跨 workspace，不扫描或合并其他产品、评测与临时根目录；空
列表明确显示 `No sessions in this Session Root`。归档、置顶、自定义分组和删除需要新增持久状态与恢复规则，
留待有明确使用需求后实现。

- 默认只列出当前 workspace 的 Session，按 `lastActiveAt` 倒序，并按 Today / Previous 7 Days / Older
  做时间分组；All workspaces 按 repository 分组。
- 每项显示 title、workspace、attention/execution 小标记、相对时间和 changed file count；不展示内部 Session ID。
- 点击 Session：更新当前 Session、关闭 history、恢复 timeline 滚动位置或滚到底部。
- 再点 History、点返回按钮或按 Escape：关闭 history，当前会话不变。
- 搜索匹配已加载摘要；不做消息全文索引、pin/archive、自定义分组或数据库。

### 4.3 VS Code 实现约束

History 是 Webview 内部的瞬时 view state，不是 Runtime lifecycle，也不写入 Session。Webview 可保存当前
Session ID、history 是否展开、各 Session 滚动位置等 UI 偏好；Session 内容仍来自 Product RPC。

## 5. UserTurn 时间线

### 5.1 基本单位

Timeline 的一级单位是已有 `turn_id` 对应的 UserTurn，而不是 Event type 分类。每个 Turn 依次包含：

1. User message；
2. Agent 在各 ModelStep 中实际输出的文本；
3. 与这些 ModelStep 关联的工具活动；
4. Agent final 或明确的 turn termination；
5. 本轮 changed files；
6. 本轮 validation 摘要；
7. 达到 UserTurn 总预算后的扩额/停止 control（存在时）。

不同 Turn 严格按首个 Event seq 排序，Turn 内条目按 Event seq 排序。不得把所有 Agent message 提到上方、
再把所有 Activity 堆到页面底部。

### 5.2 用户消息

- 右对齐，最大宽度约为 View 的 80%。
- 使用 VS Code theme token 的弱背景色和圆角；正文保留换行。
- 不重复显示 “user” 标签。hover 或次级文本可显示时间。
- 超长内容默认完整参与页面搜索，但视觉上可折叠到合理高度，用户主动展开。

### 5.3 Agent 文本

- 左对齐、无气泡，使用 Markdown 渲染；视觉上接近普通技术文档。
- 支持 paragraph、list、heading、blockquote、inline code、fenced code 和链接。
- Markdown 必须 sanitize；默认禁用原始 HTML，外部链接通过 VS Code message bridge 明确打开。
- `assistant_tool_calls.payload.message` 中非空文本出现在对应工具活动之前；`assistant_message` 作为该 Turn 的
  final text 出现在最后一个工具活动之后。
- 不显示“Agent”标签卡片，不把完整响应塞进有色容器。

### 5.4 工具活动

工具调用不是独立的 Recent Activity 栏目，而是插入 Agent 文本之间的折叠块：

```text
▸ Worked for 18s · 7 tool calls
```

展开后按发生顺序显示紧凑行：

```text
✓ Listed repository root
✓ Read README.md
✓ Searched "SessionStore" · 12 matches
✓ Edited parser.py
✕ Ran pytest · exit 1
  2 tests failed
```

规则：

- 相邻、属于同一 UserTurn 的工具活动合并为一个 disclosure；若中间出现可展示的 Agent 文本，则结束当前组，
  后续 Tool Call 建立新组，以保留“文本 → 行动 → 文本”的真实顺序。
- `assistant_tool_calls` 是 ModelStep/调用声明；`tool_requested`、`tool_result/tool_denied` 是同一调用的执行事实。
  Product projector 按 `call_id` 合并，UI 只显示一条工具项，禁止重复显示 “Agent requested tools / Tool
  requested / Tool completed” 三行。
- `command_requested/completed` 是 `run_command` 的细化事实，应 enrich 同一个 Tool item，而不是再生成两条
  timeline 项。显示 command、status、exit code、duration 和有界失败尾部。
- workspace/approval/recovery 事件若影响用户动作，作为该工具项的状态或紧邻的 notice；普通内部 lifecycle
  不单独刷屏。
- 默认折叠；包含 denial、command failure、tainted、recovery required 或 pending approval 时默认展开。
- disclosure summary 只能陈述事实，例如调用数、耗时、失败数；不使用 “Diagnosed/Planned/Solved”。

### 5.5 本轮 Changes

Turn 结束处显示 `Changed N files` 卡片。文件集合来自该 `turn_id` 下：

- `edit_transaction.payload.changed_files` / `file_changes[].path`；
- `edit_receipt.payload.path`；
- `command_completed.payload.changed_paths`。

同一路径去重，并保留 create/modify/delete/move（事件能够确定时）。卡片默认显示前 5 个文件，更多文件折叠。
点击文件通过 Runtime 提供的 snapshot/diff handle 打开 VS Code 原生 Diff Editor；Review 打开该轮
或当前 pending changes review。

必须区分：

- `Changed in this turn`：历史事实，来自这个 Turn 的 Event；即使后来 Accept/Discard 也不抹去。
- `Current pending changes`：当前 worktree 客观状态，属于 Session header/action，不等同于某一 Turn。

现有 Event 可以可靠列出本轮触及的文件，但不能对每个历史 Turn 可靠重建精确 `+N/-N`，因此历史 Turn 不显示
推测行数。Current Delivery 的统计来自 Runtime 当前 Git diff，不由 UI 猜测。

### 5.6 本轮 Validation

Validation 不再是页面底部的全局独立栏目。属于 Turn 的 validation command 作为工具项出现在真实时间位置，
并在 Turn 末尾形成一行结果摘要：

```text
✓ Validation passed · pytest tests/test_parser.py · 1.8s
✕ Validation failed · exit 1 · 2 failures
○ Validation is stale for current changes
```

- pass/fail、command、exit code、duration 来自 command/validation Event。
- “适用于当前修改”仍由 Runtime 用 revision/tree identity 判断。
- stale 表示后来修改使证据失效，不表示整个用户任务失败。
- Header 可显示当前 Session 的最新 validation attention，但不重复渲染完整 Validation panel。

## 6. Product Read Model 调整

当前 `conversation + recentActivity + changesSummary + validationSummary` 的分栏 DTO 改为 timeline-first：

```text
SessionDetail
├─ header
│  ├─ title
│  ├─ executionState
│  ├─ attentionSummary
│  └─ currentChangesSummary
├─ turns[]
│  ├─ turnId
│  ├─ userMessage
│  ├─ items[]                 # markdown | activityGroup | notice
│  ├─ changedFiles[]
│  ├─ validationSummary?
│  └─ attention[]
├─ availableActions
└─ lastSeq
```

`items[]` 投影规则：

| Event | Timeline 表达 |
| --- | --- |
| `user_message` | 开始 Turn，右侧 user bubble |
| `assistant_tool_calls.message` | 非空时生成 Agent Markdown item |
| `assistant_tool_calls.tool_calls[]` | 建立 activity tool item |
| `tool_requested` | 与 call_id 合并，补执行开始事实；不单独渲染 |
| `tool_result/tool_denied` | 关闭对应 tool item，补 status/result summary |
| `command_requested/completed` | enrich 对应 `run_command` item；无法关联时仍归入当前 Turn activity group |
| `edit_transaction/edit_receipt` | enrich edit item，并加入 Turn changed files |
| `approval_*` | enrich 当前 tool；需要用户关注时生成 notice |
| `assistant_message` | Agent final Markdown item并闭合 Turn |
| `execution_slice_exhausted` | 内部调度事实，默认不进入主 timeline；Product 自动继续同一 UserTurn |
| `turn_budget_exhausted` | Turn 尾部显示已用/上限，以及“明确扩额并继续”或 Stop |
| `turn_budget_increased` | 显示用户选择的追加步数与新总上限，然后继续原 UserTurn |
| `turn_terminated` | Turn 尾部 termination notice，不伪装 Agent 正常回复 |
| workspace tainted/recovery | 高优先级 error notice，并影响 AvailableActions |
| `model_usage` | 默认隐藏；未来诊断详情使用 |

投影必须容忍旧 Event 缺少 `turn_id/model_step_id`：复用 `context.projector` 的 legacy 行序规则或提取共同的
机械 grouping helper，不能复制一套含义不同的 UserTurn 重建逻辑。

## 7. 视觉与交互优先级

### 默认可见

- 当前任务标题和关键 attention；
- User bubble；
- Agent Markdown；
- activity disclosure 的一行摘要；
- Turn 末尾 changed files / validation / budget attention 等结果。

### 默认折叠或隐藏

- Session history；
- tool arguments、完整 command output、内部 Event type；
- workspace revision、Candidate revision、tree/SHA、Session ID；
- model usage 和协议诊断。

### 自动展开

- approval required；
- tool/command failed 或 denied；
- workspace tainted / recovery required；
- 用户从搜索或错误摘要跳转到某个 activity item。

### 滚动

- 首次打开 Session 默认滚到底部。
- 用户正在查看历史位置时，刷新不强制抢回底部；只显示“new activity”提示。
- 用户位于底部时，新 item 自动跟随。
- 切换 Session 保存各自滚动位置，仅作为 Webview UI state。

## 8. 空状态与异常状态

- 无 Session：主视图显示产品说明和 New Task，History 为空。
- Session 损坏：History 中显示 needs attention；打开后只显示可安全读取的诊断，不渲染伪造 timeline。
- 不完整 ModelStep：activity item 显示 interrupted/incomplete，不能显示成功勾选。
- 未知 Event：默认不进入主 timeline；在 raw diagnostics 中可见，避免内部新事件污染用户界面。
- RPC 断线：保留最后一次 read model，header 显示 disconnected；重连后按 `after_seq` 补读再投影。

## 9. 无障碍与安全

- History、activity disclosure、files 和 actions 均可键盘聚焦；icon button 提供 `aria-label/tooltip`。
- 不只用颜色表达 success/failure，必须有 icon 和文字。
- Markdown、tool output 和路径全部视为不可信文本，HTML escape/sanitize；Content Security Policy 保持关闭
  任意脚本和远程资源，只启用所需 nonce script 与本地资源。
- 外部链接、打开文件和 Review 动作经 `postMessage` 到 Extension host，再由允许列表处理。
- Activity 详情保持 Runtime 的有界输出，不从 diagnostics 任意读取大文件。

## 10. 当前实现边界

当前已经实现单一 `codeagent.chat` View、隐藏式 History、UserTurn timeline、Run/Continue、Stop/Approval、
Current Delivery、原生 Diff、Accept/Discard、消息复制和 composer 状态管理。Product Read Model 与 Extension
仍通过现有 RPC/notification 边界同步，Webview 不直接读取 Session 或 worktree。

仍不实现：全文历史搜索、pin/archive、在当前窗口打开其他 workspace 的 Session、原始 HTML/远程 Markdown
资源、通用前端框架和 proposed Agent Sessions API。运行中 course correction 仍要求先 Stop，再开启新的
UserTurn。

## 11. 验收标准

- History 平时完全收起，点击 header 按钮才覆盖展开；选择 Session 后回到 timeline。
- 页面没有固定的 Sessions Tree、Recent Activity、Changes、Validation 四块仪表盘式栏目。
- 每个 UserTurn 以右侧用户气泡开始，Agent 内容以 Markdown 正文呈现。
- Agent 文本与工具组严格按 Event seq 自然交错；同一调用不会出现三条重复 lifecycle 文案。
- 工具组默认折叠，失败/审批/恢复异常默认展开，展开内容仍有界。
- changed files 和 validation 显示在所属 Turn 末尾；历史 Turn 与当前 pending changes 语义分开。
- 中央 Editor 不被会话占用；源码和原生 Diff 保持主工作区。
- 刷新、切换和 RPC 重连不改变时间线顺序；`after_seq` 补读不产生重复项。
- UI 不显示推测的计划、下一步或“任务完成”判定。

## 12. 明确保留的技术债

- 历史 Turn 精确 `+/-` 行数缺少持久事实；首版只显示文件集合。
- command lifecycle 与 `run_command` tool call 的关联主要依赖当前 Event 顺序/turn/model step；如真实历史发现
  歧义，再补最小 correlation ID，不提前重做 Event schema。
- 当前 Product projector 与 Context projector 都需要理解 UserTurn；实现时应共享机械 grouping 规则，但不借机
  重构 Context 管理。
- 当前轻量 Markdown renderer 不支持完整 CommonMark、链接和 syntax highlighting；消息级复制已实现，代码块
  独立复制仍未实现。
- VS Code 不允许扩展默认直接贡献到 Secondary Sidebar；用户移动后由 VS Code 记住布局，本项目不绕过限制。
