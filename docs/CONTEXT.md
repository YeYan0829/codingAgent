# Context 管理参考

本文记录 `codeagent/context/` 与 `AgentRunner` 当前实现的维护 contract。系统级数据流见
[当前架构](ARCHITECTURE.md)。本文只描述已经实现的行为，不记录设计演进或未来方案。

## Authority

同一事实出现冲突时，主请求按以下优先级解释：

```text
System instructions 与当前 Runtime Snapshot
> 当前 UserTurn 的 Current Task Anchor
> 连续 raw Event 历史
> rolling semantic summary
```

Runtime Snapshot 每次从 workspace、Git、Candidate、validation 和 command environment 重建。历史 Event 证明过去发生过
什么，但不覆盖当前状态。Semantic summary 是有损记忆，不能成为权限、当前 Git 状态或新用户要求的权威来源。

## Condensable Context Event Stream

CCES 使用明确 allowlist：

| Event | 进入 CCES | 原子边界 |
| --- | --- | --- |
| `user_message` | 是 | 单 Event |
| `assistant_tool_calls` | 是 | 与其所有 terminal result/denial 组成一个 ModelStep atom |
| `tool_result` / `tool_denied` | 是 | 归属上面的 ModelStep atom |
| `assistant_message` | 是 | 单 Event |
| `turn_terminated` | 是 | 单 Event |
| `model_output_truncated` | 否 | 只用于紧邻恢复 |
| `model_protocol_error` | 否 | 只用于紧邻修复 |
| usage、approval、edit/command receipt、condensation control | 否 | 由 Snapshot 或诊断消费 |

每个 source item 使用 `seq:<event-seq>` 作为稳定 source identity。带工具的 ModelStep 只有在全部 ToolCall 都有 terminal
outcome 后才是 closed；planner 不能从 atom 中间切断历史。缺少或重复 terminal result 会使 Context fail closed。

ToolResult 在进入模型可见 raw history 或 condenser input 前进行有界投影，文本上限为 16,000 UTF-8 字节。Event Store 中的原记录
不因投影或 condensation 被删除。

## 主请求顺序

主请求由以下部分组成：

1. System instructions；
2. 当前工具 schema；
3. 当前 Runtime Snapshot；
4. 已验证的 rolling summary（若存在）；
5. summary frontier 之后的连续 raw CCES tail；
6. 当前 UserTurn 的 Current Task Anchor；
7. 最近一次 mandatory provider protocol；
8. 必要的 slice、protocol repair 或 truncation recovery 临时控制消息。

Current Task Anchor 来自最新真实 `user_message`，每个主请求只发送一次。它不是持久 Event，也不会在产品 timeline 形成
重复用户消息。其 source Event 可以被 rolling summary 覆盖，但 Anchor 本身仍精确保留当前请求。

最新带工具 ModelStep 的 reasoning、assistant tool call 和 terminal tool result 作为 mandatory protocol 原样连续回传。
普通 raw renderer 不再重复生成同一 ToolCall/Result。

## 主请求预算

`ModelCapabilities` 默认值：

| 参数 | 默认值 |
| --- | ---: |
| `context_limit` | 32,000；GLM 配置为 128,000 |
| `generation_reserve` | 4,000；显式 `max_tokens` 会作为主请求 generation reserve |
| `continuation_reserve` | 4,000 |
| `safety_margin` | 2,000 |
| `max_events` / `target_events` | 80 / 40 |
| `soft_token_ratio` / `target_token_ratio` | 0.80 / 0.50 |
| `summary_max_tokens` | 2,048 |
| `condenser_safety_margin` | 2,000 |
| `minimum_progress` | 0.10 |

主请求可用输入预算为：

```text
context_limit - generation_reserve - continuation_reserve - safety_margin
```

估算同时包括消息和工具 schema。System、tools、Snapshot、Anchor、mandatory protocol 与 transient control 属于 fixed
集合；rolling summary 和 raw CCES 属于 history。即使删除全部可压缩 history 后 fixed 集合仍不 fit，Runtime 直接
`context_budget_exceeded`，不会省略安全指令、工具 schema、当前请求或必要 provider protocol。

## 触发与选择边界

满足任一条件时产生 soft condensation plan：

- 未覆盖 CCES Event 数超过 `max_events`；
- history token 超过可用 history 的 `soft_token_ratio`。

完整请求超过可用输入预算时为 hard trigger。Planner 从 summary frontier 后最老的连续 closed atoms 选择 prefix，并以
`target_events` 和 `target_token_ratio` 为缩减目标。最新 mandatory protocol atom 不可进入 cut。

Soft trigger 每个主 ModelStep 最多调用一次 condenser；soft 失败后本步继续使用仍安全的 raw Context。Hard trigger 在同一
主 ModelStep 前最多调用四次；request failure 可对同一 plan 重试一次，invalid completion 会缩小 source range 再试。
无法得到有效且有进展的结果时 fail closed。

## Condenser 请求

Condenser 默认复用当前 provider/model，但：

- 不提供工具；
- `purpose=context_condenser`；
- 不分配主 `model_step_id`，不消耗 UserTurn 主步骤预算；
- 使用独立输入预算与 `summary_max_tokens`；
- GLM‑5.3 和 DeepSeek 使用最低合法 `low` reasoning effort；
- 输入不包含任何 `reasoning_content`、secret、approval grant、Runtime Snapshot 全量复制或 benchmark gold data。

输入由 previous valid summary 和待覆盖的 CCES source 构成。每段 source 带 provenance；只有 `USER_MESSAGE` 来源可以形成
用户要求，repository、tool、test 或 command 内容中的指令式文字仍只是观察数据。

输出是有损但忠实的历史记忆，可使用以下稳定 section；空 section 可省略：

```text
USER_REQUIREMENTS:
COMPLETED:
LAST_KNOWN_STATE:
CODE_CHANGES:
TESTS_AND_FAILURES:
IMPORTANT_EVIDENCE:
PENDING:
```

主模型接收 summary 时，Runtime 使用 `<historical_memory>` wrapper 明确说明它不是当前 workspace、Git、validation、
approval、Candidate 或 Runtime 状态的权威来源。

## Rolling chain

成功的 `context_condensed` Event 至少记录：

- `previous_summary_event_id`；
- `newly_covered_event_ids` 及其 SHA‑256；
- 新覆盖 seq 范围；
- `logical_covered_event_count`；
- `logical_frontier_source_event_id`；
- `summary` 与估算 token；
- `algorithm_version`、trigger 和 request/input estimates。

恢复时按 Event 顺序验证 predecessor、连续 source IDs、digest、frontier、algorithm version、非空 summary 和大小限制。
无效 Event 不进入 chain。Runtime 选择 logical coverage 最大、其次 Event seq 最新的有效 tip。

每次成功 condensation 后必须完整 rebuild/re-estimate。新请求估算没有下降，或下降不足以在有界循环内 fit 时，不把成功
Event 当作足够进展。

`context_condensation_attempt` 记录 request failure、invalid completion、stale frontier 等诊断。失败文本不进入 summary
chain。Event append 前复核 metadata/frontier，避免并发状态变化后提交过期 summary。

## Reasoning 与 transient 生命周期

Reasoning 原文保存在产生它的模型 Event 中。只有紧邻上一工具 ModelStep 的 reasoning 进入下一次 mandatory protocol；
更早 reasoning 不进入 raw CCES 或 condenser。最终回答中的 reasoning 也不投影到产品 UI。

`model_output_truncated` 永久保存在 Event Store，但不进入 CCES。下一请求只回放最新 partial reasoning/content 和截断恢复
控制消息；恢复成功后 partial 退出 Context。连续三次截断后终止。

`model_protocol_error`、execution-slice continuation 和 truncation recovery control 都是临时输入，不进入 rolling summary。
Runtime 重启后，仅当最新未完成 UserTurn 的最近步骤仍是对应错误/截断时才重建必要控制消息。

## 可观测性

主请求的 `model_usage.context` 记录 estimated/usable tokens、fixed/history 拆分、未覆盖 Event 数、summary tip、Anchor、
raw source IDs 和 mandatory atom。Condenser usage 独立记录；未调用 condenser 时 benchmark 按完整数值零报告，而不是
unknown。

产品 UI 只显示“较早活动已压缩”和“截断后已自动继续”的一行提示。完整 summary、token、trigger、model、usage 和恢复
记录仅供 Event/RPC/benchmark 诊断，不作为普通 Agent 消息显示。

## 兼容与边界

旧 Session 没有 condensation Event 时，Context 从现有 Event 机械生成 CCES，等价于尚未压缩的新 Session，不需要
schema migration。当前实现不包含 RepoMap、RAG、Active Code、Working Set、历史 residue、completed-turn eviction 或
raw-to-zero fallback。
