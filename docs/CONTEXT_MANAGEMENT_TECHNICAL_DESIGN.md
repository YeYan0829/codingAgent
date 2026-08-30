# Context Management Technical Design

状态：Accepted；Context v1 Phase 1–3 已实现，但真实长程 Agent 测试暴露 Working Context 缺口
日期：2026-08-29
范围：持续 Session 中 Event、Runtime State 到每次模型调用所需 Context View 的投影、压缩与 Resume
实现状态：Event Projection、Runtime Snapshot、deterministic reducers、Active Code、token budget、whole-turn eviction、execution slices、容量失败终止与 Resume 重建已实现。当前 Active Code 仍以同一路径最后 range 为主，active UserTurn 保留全部闭合工具协议对；这不足以维持稳定长程工作现场。下一轮以 [真实 LLM Agent 测试审计](REAL_LLM_AGENT_EVALUATION_2026-08-30.md) 为输入设计 Working Context，不直接把 Semantic Compaction 当作唯一解法。

## 1. 背景与问题定义

CodeAgent 已有 Session、append-only 关键事件、Git worktree、搜索/读取、Atomic Edit、Sandboxed Command、Validation、Approval 和 Resume。设计开始前的 `ContextBuilder` 直接读取 `events.jsonl`，按最近 8 个 `user_message` 划定窗口：旧轮次只保留 User/Assistant 文本，最新轮次重放完整 tool call/result。该实现现已被 Context Manager 取代。

该临时方案解决了“最后 40 条 event 把用户消息挤掉”，但仍有结构性问题：

- 一次 UserTurn 会产生大量审计事件，Event 不是模型上下文的正确容量单位；
- 旧轮次工具信息被整体丢弃，成功编辑、精确失败原因和未完成工作可能只剩一句“达到最大步骤数”；
- 历史 `read_file` 正文可能过期，不能充当当前代码事实；
- 当前 changed files、tree identity、validation 是否 current 应从 Runtime/Git 重算，而非从历史语言推断；
- 工具结果只有统一 `content` 字符串，没有“最新 bounded observation”和“历史 residue”的正式边界；
- CLI Resume、实时展示和模型消息分别临时解析 Event，容易出现三套不一致语义；
- `max_turns` 不能保证输入不超过模型 token limit；
- 现有事件没有显式 UserTurn/ModelStep identity，deny/error 事件也不总是标准 `tool_result`，协议恢复脆弱。

本设计增加独立 Context Projection Layer：完整持久状态不因 token budget 删除；每次 ModelStep 前从 Event、Runtime 和当前 workspace 重新构造一次临时 `ModelContextView`。

```text
SessionStore / Runtime / Git / Validation
                  ↓
          ContextProjection
                  ↓
          ModelContextView
                  ↓
     provider-specific MessageRenderer
                  ↓
               LLM API
```

## 2. Goals / Non-Goals

### 2.1 Goals

- 机械重建 `Session → UserTurn → ModelStep → ToolExchange`。
- Conversation History 与 Execution History 独立选择和压缩。
- 当前 ToolCall/ToolResult 协议永远完整配对。
- 为现有每类工具提供确定性 reducer。
- 当前 changed files、workspace state、tree 和 validation 来自权威 Runtime/Git。
- Active Code 从当前 active workspace 重读，不复用历史源码正文。
- 以 token budget 为最终容量约束，提供确定、可测试的降级顺序。
- Resume 与正常下一 ModelStep 使用同一 Context Manager。
- UI 与模型共享 projection 类型，但使用不同 renderer。
- Context Manager 独立于当前原生 ReAct loop，未来可由 LangGraph model node 调用。

### 2.2 Non-Goals

本轮不引入 Semantic Task、Task state machine、RepoMap、PageRank、dependency graph、RAG、embedding、LLM relevance ranking、跨 Session memory、Project Model、Planner/Executor、Multi-Agent、LangGraph integration 或复杂 UI 重构。

Context v1 不判断验证命令是否充分，不判断用户当前“真正目标”，不把模型摘要变成 Runtime 权威状态。

## 3. 当前实现调研

### 3.1 Agent loop

历史实现中，`AgentRunner.run_turn()` 把 `RuntimeConfig.max_steps_per_turn` 同时当作 UserTurn 终点和模型调用上限。当前实现已将它改为单个执行切片的控制点，并以 `max_model_steps_per_user_turn` 单独限制整个 UserTurn；每个 ModelStep 都通过 `ContextManager.build()` 重建 Runtime Snapshot、Active Code 和 token accounting。

Runner 当前写入：

- `user_message`；
- `assistant_tool_calls`（同一次模型响应中的 0..N calls 与可选 preamble）；
- `tool_requested`、`tool_result`、`tool_denied`；
- `approval_requested`、`approval_decision`；
- `model_protocol_error`；
- `assistant_message`（正常 final 或明确错误 final）；
- `execution_slice_exhausted`（切片控制点，不是 final）；
- `turn_terminated`（UserTurn 总 ModelStep 预算耗尽，明确标记未完成）。

Command、Atomic Edit、Workspace lifecycle 还会写入各自的事实事件。

### 3.2 Event 与 Session

`SessionEvent` 当前包含 `seq/turn_id/model_step_id/ts/type/payload`。`events.jsonl` 的顺序仍是事实顺序；tool calls 通过 provider `call_id` 关联，旧事件缺少 identity 时由 projector 按行序和 UserMessage 边界兼容投影而不改写历史。SessionStore 负责分配 identity、append 和 `session.json` 状态。

### 3.3 ContextBuilder

当前 Builder：

- 加载两个 system prompt；
- 保留最近 8 个 UserTurn 的 User/Assistant 文本；
- 仅对最新 UserTurn 重建 `assistant_tool_calls → tool_result`；
- 不计 token；
- 不读取 Git/Validation；
- 不生成 Active Code；
- 不保留旧工具 residue。

### 3.4 Runtime truth

- `session.json` 是 workspace state、workspace revision、Candidate revision、base identity 和 grants 的权威来源；
- active Git worktree 是当前文件和 changed paths 的权威来源；
- `snapshot_tree()` 生成 current subject tree；
- `validation_completed.subject_tree/workspace_revision/base_commit` 与当前值匹配时，证据 current；
- Atomic Edit receipt 已包含 changed files、transaction、revision 和 before/after identity；
- Command summary 已包含 command、purpose、exit、before/after tree、changed paths 和 workspace state。

### 3.5 Resume 与 UI

Resume 先恢复 Atomic transaction、检查 worktree 和 fail-closed state，再重建 Runner。CLI `_format_session_overview()` 另行读取 Event/Git/Validation；实时进度直接消费 Runner step。三者尚未共享正式 projection。

### 3.6 必须同步修正的旧文档事实

当前代码只有 workspace audit 检测到变化时才推进 Candidate revision；validation current 以 subject tree 为核心。调研时发现 `ARCHITECTURE.md`、Command/Sandbox TD 和 Session/Workspace TD 仍残留“每条命令推进、任何后续命令使证据失效”的陈旧描述，本轮已同步修正，使 Context 设计引用同一组 Runtime 事实。

## 4. 核心术语与生命周期

### 4.1 Session

一次可 Resume 的完整持续会话。Session 可以包含多个互不进行语义归类的 UserTurn。

### 4.2 UserTurn

从一条 `UserMessage` 开始，到下一条 Final Assistant Message或明确的总预算终止为止。单个 UserTurn 可以跨多个执行切片：切片只是 Runtime 控制点，不创建新 UserMessage、不冒充 Final，也不是新的持久化业务实体。进程中断或切片暂停而没有 Final 的 Turn 为 `incomplete`，Resume 时仍可机械识别并用 `/continue` 延续。

### 4.3 ModelStep

一次 LLM API 调用。响应可能是 Final Assistant Message，或包含 1..N 个 ToolCall 的 assistant response。每个 ModelStep 前必须重新 `ContextManager.build()`。

当前默认每个执行切片最多 12 个 ModelStep，同一 UserTurn 总计最多 48 个。CLI 在切片边界询问是否继续，API/benchmark 可在总预算内自动继续。继续时 Context Manager 仍从同一 UserMessage 和 Runtime 真相重建上下文，并加入非持久语义状态的控制提示；它不依赖用户发送“继续”。为控制同一长轮次的增长，所有 ToolExchange 协议对都保留，只有最近 4 个执行步骤保留工具调用前的自然语言说明，较旧说明可安全省略。

### 4.4 ToolExchange

一个 ToolCall 及其唯一 terminal outcome：success result、tool error、policy deny、approval deny 或 protocol error。一次 ModelStep 有 0..N 个 ToolExchange。

ToolExchange 是 projection 内部类型，不是新的 Agent-facing artifact。任何 terminal outcome 在 provider renderer 中都必须成为与 call id 配对的合法 tool result；不能因为 Event 类型叫 `tool_denied` 就遗漏协议结果。

### 4.5 ModelContextView

只服务一次 ModelStep 的不可变、临时视图。它不是 Session 真相，不回写工具或 workspace 状态。下一 ModelStep 重新构建。

## 5. Authority / State Ownership

优先级固定为：

```text
Runtime / Git / Validation objective state
        > persisted operation events
        > deterministic historical residue
        > semantic conversation summary
```

- Summary 说 tests passed，但 current tree 与 evidence tree 不同：显示 validation stale。
- 旧 `read_file` 说文件内容 A，但当前 workspace 是 B：Active Code 使用 B。
- Assistant 声称 edit succeeded，但没有 committed edit receipt/Git change：不得当成成功事实。

Context Manager 只投影权威来源，不拥有 workspace、validation、approval 或 Candidate 状态。

## 6. Persistent State 与 Model Context View

### 6.1 Persistent State

继续复用：

- `session.json`；
- `events.jsonl`；
- active Git worktree；
- 失败/恢复所需有界 diagnostics。

Context 压缩不删除原始 Event。成功命令的无限 stdout 当前本来就不持久化；本设计不推翻“正常成功路径只保留有界摘要”的产品决策。

### 6.2 Raw Tool Result 的准确含义

Context v1 中 Raw Tool Result 指“工具自身输出边界处理后的完整 `ToolResult`”，不是无上限 OS stream。`read_file/search/run_command` 必须先在工具层限制 bytes/lines，并记录 `truncated`、可用 total/limit reason。失败 diagnostics 仍按现有 retention 保存。

因此层次是：

```text
unbounded external stream
  → tool/service bounding
  → persisted Raw ToolResult（产品允许的有界原始结果）
  → Bounded Observation（当前模型步骤）
  → Historical Residue（旧步骤）
```

### 6.3 ModelContextView 组成

```text
ModelContextView
├── instructions
├── runtime_snapshot
├── conversation_prefix_summary?   # 低优先级、非权威
├── recent_conversation
├── recent_execution
├── active_code
├── current_protocol_tail
└── budget_report                   # diagnostics，不发给模型或仅开发模式
```

### 6.4 派生对象默认不持久化

`UserTurnView`、`ModelStepView`、`ToolExchange` projection、Bounded Observation、Historical Residue、Runtime Snapshot、`ActiveCodeCandidate`、`ActiveCodeSelection`、Active Code Body、Recent Conversation/Execution View、`ModelContextView` 和 `BudgetReport` 都是 in-memory、per-build 派生对象。

不得为了调试自动创建 `context_views/`、`runtime_snapshots/`、`active_code/`、`budget_reports/`、`tool_residues/` 或 `conversation_windows/`。测试直接断言对象；开发诊断优先使用现有 logging，必要失败信息进入已有 Event/error diagnostics。`context_budget_exceeded` 可在错误 detail 中携带 source token breakdown。可以从权威来源重算的视图，不持久化成第二套状态。

## 7. Context Sources

### A. Instructions

System prompt、tool policy、当前 ToolRegistry schemas。由 Runner/Registry 提供，低频变化；必须计入预算但不可通过历史压缩删除。动态执行环境事实放 Runtime Snapshot，不写进静态 prompt。

### B. Recent Conversation

仅选择 UserMessage 与 Final Assistant Message，保持自然语言引用连续性。Tool preamble 属于 ModelStep/Execution，不冒充 Final conversation。选择以 token budget 为准，recent turn 数只作为最低保留策略。

### C. Recent Execution

EventProjector 机械构造 ModelStep/ToolExchange；Reducer 将旧 observation 转为 residue。不让 Conversation 的窗口被工具 event 数量决定。

### D. Runtime Snapshot

每次 build 时动态查询：

- session/workspace state 与 reason；
- source/active workspace identity、workspace revision、base commit；
- Candidate revision 与 current subject tree；
- changed paths 及 create/update/delete/rename；
- current/stale validation evidence 的客观状态；
- pending lifecycle/approval/command（如果 Event 显示未闭合）；
- `recovery_required/workspace_tainted` 能力限制；
- command environment contract：default cwd 是 active workspace、HOME 为 private、`~` 不等于 host home、network mode。

Snapshot 不包含 `current_goal/next_step/important_findings`。

### E. Active Code

ActiveCodeSelector 从 evidence 选 path，再通过正式 PathGuard/read service 从当前 active workspace 重读。正文携带 current sha/tree，绝不复制历史 `read_file.content`。

## 8. Event Projection

### 8.1 目标内部类型

```python
UserTurnView(
    turn_id,
    user_event_seq,
    user_message,
    model_steps,
    final_message,
    status,  # active/completed/incomplete
)

ModelStepView(
    step_id,
    assistant_event_seq,
    preamble,
    exchanges,
    final_message,
)

ToolExchange(
    call_id,
    tool_name,
    arguments,
    requested_seq,
    approval,
    terminal_kind,
    raw_result,
    runtime_refs,
)
```

### 8.2 Identity

v1 新事件应显式写入 `turn_id` 与 `model_step_id`；Session event sequence 由 append 时单调分配并持久化。Tool 使用 provider call id。旧 fixture 可用 JSONL line number、UserMessage boundary 和 call id 机械投影，仅作为 migration/test adapter，不作为长期新写入格式。

### 8.3 配对规则

- 一个 call id 只能属于一个 ModelStep；
- 一个 call id 只能有一个 terminal outcome；
- provider 已产生合法 tool name 与 `call_id` 后，schema validation fail、policy deny、approval deny 和 runtime execution error 都归一为该 ToolExchange 的 terminal outcome，并渲染配对 tool result；
- provider 没有产生合法 ToolCall（malformed JSON、tool name/call id 不可解析或 response 违反协议）时属于 `ModelStepProtocolError`，不构造 ToolExchange、不伪造 call id；
- 多 calls 按 assistant response 中顺序稳定排列，结果可按执行顺序归并；
- active protocol tail 必须全量保留；任何裁剪以完整 ModelStep/ToolExchange 为边界。

### 8.4 不一致事件

缺 result、重复 result、unknown call id、跨 step call id 或无法闭合 approval 时，Projection 返回结构化 `projection_error`。当前 ModelStep fail closed，不把破损协议发送给 provider；Resume UI 显示需诊断。只有 Runtime workspace 恢复不可信时才升级 `recovery_required`，纯历史 projection 错误不应谎称 filesystem 损坏。

## 9. Tool Observation Lifecycle

### 9.1 Raw Tool Result

工具/服务产生、经过自身 output limit 的完整 ToolResult，进入 Event 或 diagnostics。用于 audit、debug、当前 exchange 和 UI drill-down。

### 9.2 Bounded Observation

当前/最近 ToolExchange 发给模型的有限表示。保留模型下一步直接依赖的正文，但继续遵守 per-tool bytes/line cap。不得由 Context Manager 接受无限字符串后才首次截断。

### 9.3 Historical Residue

Exchange 变旧或预算紧张时的确定性表示。保留 identity、目标、terminal outcome、关键错误和 workspace effect；删除可重取正文。

生命周期转换不修改 Event：

```text
Event Raw Result
  ├─ latest/current → Bounded Observation
  └─ historical     → Historical Residue
```

## 10. Deterministic Reducers

### 10.1 `read_file`

- 最新：path、line range、current sha256、total lines、truncated、bounded body。
- 历史：path、range、read sha256/tree、成功/错误；body omitted。
- 若 current sha 不同：标记 `changed_since_read=true`。
- 可重新获取：是；Active Code 必须重读。

### 10.2 `search_text` / `find_files`

- 最新：query/mode/path、bounded matches、match/file count、truncated/limit reason。
- 历史：query、filters、match count、selected matched paths、error；match bodies omitted。
- 可重新获取：是。

### 10.3 `list_dir` / `show_tree`

- 最新：root/depth、bounded entries/tree、entry count、truncated。
- 历史：root/depth、entry count、错误；listing body omitted。
- 可重新获取：是。

### 10.4 `git_status` / `git_diff` / `git_diff_stat`

- 最新：bounded content、path filter、entries/stat、truncated。
- 历史：command kind、path、changed paths/stat、tree identity；patch body omitted。
- 当前 diff 不从历史 residue 恢复，Runtime Snapshot/Git 重算。

### 10.5 `apply_workspace_edit`

- 最新成功：operation types/targets、transaction id、Candidate revision、changed files、before/after identity；不需要在 result 中重复完整 old/new text。
- 最新失败：operation index、operation kind（若可解析）、targets、error code、精确 field/path reason、mutation 是否开始、workspace changed/rollback/recovery 状态。
- 历史成功：targets、revision、transaction id、changed files。
- 历史失败仍保留结构化根因。例如：

```text
apply_workspace_edit failed before execution
targets: tests/test_itsdangerous/test_encoding.py
reason: operations[0].op missing; operations[1].op missing
workspace_changed: false
```

Reducer 不用自然语言猜错误；Atomic Edit validation 应提供 machine-readable `operation_errors[]`。在其落地前使用 error_code + 有界原始错误，不做正则语义推断。

### 10.6 `run_command`

- 最新：exact command/cwd/purpose、exit/status/timeout、bounded stdout/stderr、before/after tree、changed paths、workspace state、truncated。
- 历史：exact bounded command/hash、cwd/purpose、exit/status、关键失败尾部（确定性 first/last lines，不生成语义“key result”）、before/after tree、workspace_changed、validation id/currentness；stdout/stderr body omitted。
- `purpose=validation + exit 0` 只记录 evidence，不称为“测试充分”。
- 可重跑不等于可自动重放；Reducer 只描述，不执行。

### 10.7 Approval / Permission

- 最新：工具/命令、requested/resolved resource、capability/scope/reason、decision。
- 历史：批准/拒绝对象、scope 和 decision；冗长 policy detail omitted。
- 未闭合 approval 作为 pending state，不裁剪。

### 10.8 Malformed tool arguments 与协议错误

- 已有合法 tool name/call id、但 arguments 不满足 schema：属于 ToolExchange error。最新保留 tool name、call id、schema category、JSON path/field/index、attempt、`tool_executed=false`；历史移除 raw arguments，可保留 fingerprint。
- provider response 本身无法形成合法 ToolCall：属于 `ModelStepProtocolError`。保留 provider、parser/protocol category、line/column（如有）、attempt 和 `tool_executed=false`，但不伪造 ToolExchange。
- 两者都不得压缩成模糊 `tool failed`，也不得声称 workspace change。

## 11. Active Code Selection v1

> 已知实现限制：v1 对同一路径的 recent-read evidence 主要保留最后 range，而不是同一 SHA 下的稳定范围集合。真实 Session 已观察到后读小范围使此前互补源码正文退出醒目 Context，模型转用 `sed/cat` 重建视野。本文后续的选择规则仍记录 v1 契约；Stable Working Set 将由下一轮 Technical Design 取代或扩展，不能把当前行为宣称为已解决。

### 11.1 Evidence 与排序

每个 path 产生可解释 `ActiveCodeCandidate(path, reasons, score, latest_seq)`。只接受通过 PathGuard 且当前存在的 UTF-8 普通文件。

固定优先级：

1. current changed files：来自 Git Runtime Snapshot；
2. latest failed operation targets：最近 terminal failed edit/read/command diagnostics 中的显式 workspace path；
3. recently successful `read_file` paths：来自最新 ToolExchange；
4. recent error/search referenced paths：只使用结构化 metadata（matched paths、changed paths、明确 path 字段），不从任意 stderr 做文件名 NLP。

同级按 latest event sequence 降序，再按 POSIX path 排序，保证确定性。一个 path 合并所有 reason，例如 `changed_file + latest_failed_edit_target`。

### 11.2 正文读取

选择结束后从 `WorkspaceContext.active_root` 通过正式 read service 重读；记录 current sha、line range、tree identity 和 reason。历史读取 sha 只用于标记变化，不提供正文。

### 11.3 Slice range provenance 与独立预算

每个 Active Code slice 必须能解释 range provenance：recent `read_file` 使用其请求 range或确定性的有限邻域；failed edit 使用结构化 operation range；changed file 使用 edit receipt 的 changed range。没有 range evidence 时，不推断“相关代码”，只能选确定性 bounded prefix、只提供 path/metadata，或让模型再次调用 `read_file`。

Active Code 有独立 `active_code_token_budget`。changed file 获得高优先级，但不保证所有 changed files 的完整正文进入最低集合。最低集合只保证当前下一步直接依赖的 bounded changed-file slice 和 latest failed target 的必要 bounded slice；其他 changed files 仍必须在 Runtime Snapshot 中保留 path、变化种类和 identity，可以没有正文。不得因大量 changed files 或单个大文件挤掉 Runtime Snapshot、current user 或 protocol tail。

## 12. Runtime Snapshot

`RuntimeSnapshotProvider.build(session_store, workspace_context)` 是只读接口。失败语义：

- Git/tree 无法读取：Context build fail closed，不能用 summary 代替；若同时表明 workspace integrity 不可信，复用现有 state transition 进入 tainted/recovery；
- Validation 查询失败：标记 `validation_state=unknown`，accept 仍由正式 gate 自己复检；
- Active Code 单文件读取失败：保留 path/reason/error，继续选择其他文件，敏感/escape 拒绝不降级绕过。

Snapshot 示例：

```json
{
  "workspace_state": "changes_active",
  "workspace_revision": 1,
  "candidate_revision": 3,
  "base_commit": "...",
  "subject_tree": "...",
  "changed_paths": [{"path": "src/x.py", "kind": "update"}],
  "validation": {"state": "stale", "latest_command": "...", "evidence_tree": "..."},
  "execution_environment": {
    "default_cwd": ".",
    "home_kind": "private_runtime_home",
    "tilde_is_host_home": false,
    "network_mode": "off"
  }
}
```

## 13. Token Budget Algorithm

### 13.1 预算公式

```text
usable_input_budget = model_context_limit
                    - generation_reserve
                    - continuation_tool_reserve
                    - safety_margin
```

- `model_context_limit` 来自 provider/model capability 配置，不由模型自行声称；
- `generation_reserve` 至少覆盖 `max_tokens` 或 provider 默认输出预算；
- `continuation_tool_reserve` 为下一次 tool call/result 留余量；
- `safety_margin` 吸收 tokenizer 差异和 provider envelope。

Tool schemas 与 messages 都计入输入。v1 在 provider 没有 tokenizer 时使用独立 `TokenEstimator` 的保守 UTF-8/字符估算，并在 integration test 中校准；不能退回 event count 作为容量保证。

首版工程默认值为 context limit 约 32k、generation reserve 约 4k、continuation/tool reserve 约 4k、safety margin 约 2k；它们全部属于可配置的 `ModelCapabilities`，不是协议常量。首版以保守留余量为目标，不追求吃满 provider 最大 context。

### 13.2 最低保留集合

不可轻易裁剪：

- system/safety/tool schemas；
- current user message；
- Runtime Snapshot；
- active/incomplete ModelStep 的完整协议；
- latest failure 结构化 residue；
- 当前下一步直接依赖的 latest bounded observations；
- 当前下一步直接依赖的 bounded changed-file slice 和 latest failed-target 必要 slice；所有 changed path identity 已由 Runtime Snapshot 保留；
- 最近自然语言对话（至少上一 completed UserTurn，预算允许时更多）。

若最低集合仍超预算，Context build 返回 `context_budget_exceeded`，列出最大消费者；不得静默删除 safety、current user 或协议配对后调用模型。

### 13.3 完整降级顺序

1. 工具层 Per-Observation Bounding；
2. 旧 read/search/find/list/tree body → residue；
3. 旧 git diff body、command stdout/stderr → residue；
4. Active Code 淘汰低优先级、未修改且非 failed-target 文件，并缩短低优先级片段；
5. 更老 ToolExchange 保留 terminal outcome/identity，删除 arguments 中的大源码字段；
6. 从最老开始，以完整 completed UserTurn 为原子单位淘汰；不得拆散 UserMessage/Final，也不得淘汰 active/incomplete UserTurn；
7. 仍超预算则明确失败，不破坏最低集合。

每步后重新估算 token；BudgetReport 记录各 source tokens、采用的 reductions 和 omitted refs，进入开发 diagnostics，不默认成为用户 artifact。

## 14. 两种压缩问题

### 14.1 Per-Observation Bounding

发生在工具/服务边界。单次 pytest、diff 或 read 再新也不能无限进入 Event/Context。需要每个 ToolResult 提供 `truncated` 和尽可能的 `total_bytes/total_lines/limit_reason`。

### 14.2 Historical Reduction

发生在 Context Projection。随着 ToolExchange 增长，将旧 Bounded Observation 转 residue。两者不能由一个统一 `summarize(text)` 代替。

## 15. Semantic Compaction（Phase 4 Future Extension）

Semantic Compaction 不进入 Context v1 首次实现。v1 不创建 `context_compacted` Event、不修改 Event schema 支持 compaction persistence、不实现 LLM compactor，也不新增 compaction artifact/store/directory。v1 在 deterministic reduction 后从最老开始丢弃完整 completed UserTurn；只有真实 long-session/manual/benchmark 证明这种方式不足时，Phase 4 才单独 review。

以下仅保留未来算法位置：Semantic Compaction 只处理长期 Session 的老自然语言 Conversation 前缀；工具 stdout 先走 deterministic reducer。未来它把“old completed turns → drop”升级为“old completed turns → semantic summary”。

### 15.1 触发

- deterministic reduction 后仍超过 conversation allocation；或
- Resume/里程碑时已超过配置阈值。

不每轮调用，不在短 Session 提前付出额外模型调用。

### 15.2 完整前缀边界

只 compact 已 completed 的完整 UserTurn 前缀；不得覆盖 active/incomplete UserTurn，不拆 ToolExchange。保留最近自然语言 turns 原文。

### 15.3 持久化

未来 Phase 4 若经独立 review 启用，只考虑新增单一 `context_compacted` Event，不建立目录族：

```json
{
  "compaction_id": "...",
  "covered_event_start_seq": 1,
  "covered_event_end_seq": 420,
  "summary": "...",
  "created_at": "...",
  "provider": "...",
  "model": "...",
  "prompt_version": 1,
  "source_digest": "..."
}
```

原 Event 不删除。下一次只选择覆盖范围最大的有效 summary；新 compaction 覆盖“旧 summary + 后续 completed prefix”，避免反复总结同一原文。`source_digest`/range 不匹配则忽略 summary 并重建。

### 15.4 权威性

Summary 是辅助历史，只表达对话延续；Runtime Snapshot 永远覆盖 summary 中关于 files/tests/state 的陈述。模型 renderer 显式标注该边界。

## 16. Resume Flow

```text
load session.json + events.jsonl
→ 现有 Atomic/worktree recovery 与最高优先级 gates
→ EventProjector rebuild UserTurns/ModelSteps/ToolExchanges
→ RuntimeSnapshotProvider query Git/Validation/current state
→ ActiveCodeSelector select + reread current workspace
→ Reducers produce recent execution/residues
→ TokenBudgeter degrade deterministically
→ ModelMessageRenderer build next request
```

Resume 不创建另一份上下文状态。正常继续和 Resume 调用相同 `ContextManager.build()`；区别仅是 Resume 在此前执行现有 recovery/identity 检查。

## 17. UI / Model Rendering Boundary

共享：`UserTurnView`、`ToolExchange`、`RuntimeSnapshot`、`ActiveCodeSelection`。

Model renderer 需要 provider tool protocol、bounded code、identity、structured errors 和 summary authority marker。

CLI renderer 输出用户可读 timeline：

```text
✓ 读取 src/itsdangerous/encoding.py
✓ 修改 src/itsdangerous/encoding.py (revision 1)
✗ 修改 tests/.../test_encoding.py
  operations[0].op 缺失；未执行，workspace 未变化
当前修改：1 file
当前验证：无 current evidence
```

CLI 不展示完整 ModelContextView，也不默认展示模型冗长 preamble。实时 preamble 应有独立 UI 长度限制/折叠，不写回模型语义。

## 18. 数据生命周期表

| 数据 | 来源 | 持久化 | 生成/维护者 | 更新/失效/压缩 | 消费者 | 权威事实 |
| --- | --- | --- | --- | --- | --- | --- |
| UserMessage | CLI/API 用户输入 | Event | Runner/SessionStore | 每 UserTurn；原文不因 context 删除 | Conversation、UI、模型 | 对“用户说过什么”是 |
| Assistant Final | 模型 final/Runner terminal | Event | Runner | Turn 结束；老前缀可由 summary 替代进入模型 | Conversation、UI | 对“Agent 说过什么”是，不是 Runtime truth |
| ToolCall | 模型响应 | Event | Runner | 每 ModelStep；旧大 arguments residue 化 | Projector、audit | 对“模型请求什么”是 |
| Raw ToolResult | tool/service bounded result | Event/必要 diagnostics | Tool/Runner | 每 call terminal；不因 context 删除 | Reducer、audit、UI | 对工具返回是 |
| Bounded Observation | Raw Result projection | 否 | Reducer | 每 build 重建；exchange 变旧或超预算后降级 | 模型 | 派生 |
| Historical Residue | Event + reducer | 否 | Reducer | 每 build 重建；规则版本变化可重建 | 模型、CLI | 派生 |
| Runtime Snapshot | session/Git/validation | 否 | SnapshotProvider | 每 ModelStep 重算，立即随 workspace 变化失效 | 模型、CLI、Budgeter | 是（对应当前时刻） |
| Active Code Paths | Snapshot + structured recent evidence | 否 | ActiveCodeSelector | 每 build 重算 | Code loader、UI diagnostics | 派生可解释 |
| Active Code Body | 当前 workspace | 否 | ActiveCodeLoader | 每 build/sha 变化失效 | 模型 | 对读取时内容是 |
| Recent Conversation View | User/Final events | 否 | ConversationProjector | 每 build、预算变化 | 模型 | 派生 |
| Recent Execution View | Tool events | 否 | EventProjector/Reducer | 每 build、预算变化 | 模型、CLI | 派生 |
| Conversation Summary（Phase 4） | completed Event prefix | 未来可能为 `context_compacted` Event | Compactor | v1 不生成；未来达阈值后生成 | 模型 | 否，辅助 |
| Compaction Metadata（Phase 4） | compaction event fields | v1 不存在 | Compactor/SessionStore | 未来每次 compaction；原事件保留 | Projector、audit | 对覆盖范围/生成参数是 |
| BudgetReport | View/token estimator | 默认不持久化；失败可 diagnostics | TokenBudgeter | 每 build | tests/debug | 否 |

不新增 `current_goal`、`important_findings`、`active_files`、`progress_summary`、`next_step` 持久字段。

## 19. Failure Semantics

- `projection_error`：事件协议无法重建；不调用模型，保留原始事件供诊断。已有合法 call id 的 schema/deny/runtime error 是 ToolExchange terminal outcome；只有无法形成合法 ToolCall 的 provider response 才是 ModelStepProtocolError。
- `context_budget_exceeded`：最低集合超过预算；Runner 不调用模型，写入明确的 `turn_terminated`（estimated/usable tokens、已采用 reductions 和 dropped turns），CLI 说明这是单次模型输入容量而非 Session 累计额度。Session/workspace 保留，用户可检查状态并以新 UserTurn 继续；不静默裁 safety/current user，也不让未捕获异常把 Session 留在含糊的 incomplete 状态。
- `runtime_snapshot_unavailable`：当前事实无法建立；不以 summary 代替。
- `active_code_read_failed`：单文件失败可降级为 path/error，SensitivePath/escape 继续拒绝。
- `compaction_failed`：仅属于未来 Phase 4；v1 没有此状态。
- malformed model arguments 若已有合法 call id 则是 ToolExchange schema error；只有无法形成合法 ToolCall 才是 provider protocol error。两者的 retry messages 都只是当前 UserTurn 临时 source。
- Context failure 本身不修改 workspace state；只有现有 Runtime integrity 检查失败才进入 tainted/recovery。

## 20. 目标代码结构与接口

以下是逻辑职责划分，不是强制物理文件结构：

```text
codeagent/context/
├── models.py          # UserTurnView/ModelStepView/ToolExchange/ContextView
├── projector.py       # persisted Event → protocol-safe views
├── reducers.py        # per-tool deterministic reducers
├── runtime_snapshot.py
├── active_code.py
├── budget.py          # estimator、allocation、degradation
├── compaction.py      # Phase 4 才考虑创建
├── renderers.py       # provider model messages；不含 CLI
└── manager.py         # ContextManager.build(request)
```

核心接口：

```python
ContextManager(session_reader, runtime_snapshot_provider, tool_registry, ...)
ContextManager.build(
    session_id=...,
    model_capabilities=...,
    current_turn_transient_messages=(),
) -> ModelContextView
ModelMessageRenderer.render(view) -> list[dict]
```

稳定依赖优先在初始化时注入，每次 build 只传当前 ModelStep 真正变化的输入；结合现有代码选择最小方案，不引入 DI 框架、service locator 或巨型 request。上述职责较小时允许合并文件/函数，不为每个名词机械创建空壳类。Runner 只在每个 ModelStep 前调用 Context Manager；未来 LangGraph model node 可调用同一接口。

## 21. Migration Plan

### Phase 1：协议投影与客观 Snapshot（v1 必做）

1. 为新 Event 增加 sequence、turn id、model step id；提供旧 Event projector fixture。
2. 实现 UserTurn/ModelStep/ToolExchange 投影和 terminal outcome 归一。
3. 将 deny/error 也渲染为合法配对 tool result。
4. 实现 Runtime Snapshot 与 execution environment contract。
5. Runner 改为依赖 `ContextManager`；行为仍用现有模型 gateway。

### Phase 2：Reducers 与 Active Code（v1 必做）

1. 为当前工具逐类 reducer；补必要的 machine-readable error metadata。
2. 工具层补齐 total/limit metadata，不保存无限输出。
3. ActiveCodeSelector/Loader 从当前 workspace 重读。
4. CLI Resume/timeline 复用 projection 与 snapshot。

### Phase 3：Token Budget（v1 必做）

1. Provider model capability/config 与 TokenEstimator。
2. source allocations、最低集合和完整 degradation pipeline。
3. 替换 `max_turns` 作为最终容量约束；少量 recent windows 仅作优先级。

### Phase 4：Semantic Compaction（明确后置）

不属于 Context v1。只有长 Session fixture、人工测试或 benchmark 表明 deterministic reduction + whole-turn eviction 不足后，才单独 review `context_compacted` Event 和滚动 prefix compaction；v1 不为它预建 schema、文件或 store。

### 文档同步

实现时更新 ARCHITECTURE、TESTING、MANUAL_TEST、PROJECT_GUIDE 和 NEXT_PHASE_PLAN；旧 ContextBuilder 行为不再作为文档事实。

## 22. Test Plan

### 22.1 Projection

- UserTurn completed/incomplete/slice-paused/total-budget-terminated 划分；
- 执行切片暂停仍属于同一 incomplete UserTurn，继续与 Resume 均不新增 UserMessage；
- 单切片预算耗尽不产生伪 Final，总 ModelStep 预算耗尽明确终止；
- 每次 LLM call 是一个 ModelStep；
- 0..N parallel calls 稳定归并；
- success/error/policy deny/approval deny terminal outcome；
- duplicate/missing/unknown call id 明确失败；
- budget 裁剪不拆 call/result。

### 22.2 Reducers

- old read body cleared，path/range/sha/tree 保留并标记 changed_since_read；
- search bodies cleared，query/count/paths 保留；
- command stdout/stderr cleared，exact command/exit/tree/change/error 保留；
- malformed edit 保留 operation field error 与 `workspace_changed=false`；
- sensitive/escape error 不被弱化。

### 22.3 Runtime truth

- pytest passes @ T0，edit → T1：validation stale；
- pytest passes @ T0，无变化 utility command：validation current；
- summary 声称 passed 但 current tree 不同：Snapshot 胜出；
- tainted/recovery state 覆盖普通 summary。

### 22.4 Active Code

- read A/read B/edit A，预算不足时 A changed 保留、B 淘汰；
- failed edit target test file 优先于普通 recent read；
- 文件从 T0 修改后正文从当前 workspace 重读；
- deleted/binary/sensitive/oversize 文件给出确定 residue。

### 22.5 Multi-turn Conversation

- 同一 UserTurn 跨多个执行切片时原始请求和约束持续可见；
- 长轮次省略旧工具调用前说明，但不拆散 assistant tool-call/tool-result 协议对；
- 交互 `/continue` 与非交互自动续切片使用同一 Runner continuation 路径。

- Assistant 给方案一/二，长 Execution 后 User“第二个继续”：上一 Final 原文仍在；
- 用户后续改变要求时保留原话，不生成 Task 判断；
- 多行用户消息与 exact command 不被工具 event 挤掉。

### 22.6 Budget

- rendered messages + tool schemas <= usable budget；
- 每一级 degradation 次序确定；
- 最低集合超预算返回明确 error；
- estimator 对 DeepSeek integration 保守校准。

### 22.7 Resume

- 重启后 Snapshot、changed paths、validation currentness 一致；
- Active Code 重新读取；
- legacy event identity compatibility；
- incomplete ToolExchange 不发送破损协议。

### 22.8 Real-session fixtures

以已出现的 ItsDangerous Session 脱敏复制 Event fixture，覆盖：实现 edit 成功、test edit 缺 `op` 失败、step limit、resume、旧 baseline validation stale。断言 View 准确表达实际 changed file、失败根因、workspace unchanged by failed call、current validation 和所选代码。

测试不接真实 LLM；Fake Model 验证正式 Runner/Registry/Context 调用链。首轮测试不包含 Semantic Compactor。

## 23. Future Extensions

- Semantic Task Layer：作为新的 Context Source，对 UserTurn 分组；不改变底层 projection。
- Repository Explorer/RepoMap：作为 Repository Context Source；不替代 Active Code 当前 workspace truth。
- Structured Agent Notes：Agent-written、非权威 semantic source，需要独立 schema/生命周期 review。
- Session History Retrieval：索引原 Event，在用户引用久远细节时按需取回。
- LangGraph：负责 orchestration/checkpoint/interrupt/state transition；继续调用 Context Manager，不替代 Runtime/Event/Sandbox/Policy/Validation。

SWE-bench 是通常只有一个初始 UserTurn 的 Session 特例；复用同一 Context Manager，仅由 adapter 提供 repository、执行环境和评测命令事实。

## 24. Resolved Decisions

### Decision 1：model context limit 与 tokenizer

采用 `ModelCapabilities` 显式配置；首版可用约 32k/4k/4k/2k 的保守默认组合。有官方 tokenizer 时精确计数，否则使用 conservative estimator + safety margin，后续以 provider usage/integration telemetry 校准。event/turn count 不作为容量保证。

### Decision 2：Semantic Compaction 后置

首个实现只完成 Phase 1–3。Phase 4 单独 review 前，不创建 compaction Event、schema、artifact、store 或真实 LLM compactor。

### Decision 3：Event schema migration

采用 `new writes → new event schema; old reads → compatibility projection`。新事件只增加 `seq/turn_id/model_step_id`，ToolCall 继续使用 provider `call_id`；不增加 task/trace/parent identity。旧保留 Session/fixture 用 JSONL line order、UserMessage boundary 和 call id 机械读取，不原地重写、不创建 migration artifact，也不承诺兼容所有早期原型。
