# 长工具轨迹 Context 容量调研

> 本文记录 2026-08-30 的阶段性调研。其“稳定 Working Set”建议已由 D-2026-08-31-01 修正：Context v2 先移除隐式 Active Code，以 Recent Tool Context、residue 和合法边界 condensation 建立基线；Working Set 暂缓为有 benchmark 证据后的可选优化。

## 1. 问题

真实 HTTPX Session `a4dca0d1a266` 在一个未完成 UserTurn 中累计 40 次模型调用、53 组工具调用/结果。确定性 residue 已缩小旧正文，但当前实现仍把 active UserTurn 的全部闭合 provider 工具协议对列入最低集合，最终下一次请求估算 `23503 > 22000`，无法继续构建。修复错误路径后，`2f6ce17a6bde` 在 35 个 ModelStep、41 个工具调用时被明确终止为 `23024 > 22000`。

这不是 Session 累计 token 配额，而是单次模型请求的输入容量问题。完整 Event 与 Git/workspace 状态仍可长期保留；需要缩小的是每次临时生成的 Model Context。

## 2. 现有方案

### 2.1 Anthropic：优先清除旧工具结果

Anthropic Context Editing 将 tool-result clearing 明确用于工具密集型 Agent：超过阈值后按时间顺序清除最旧结果，默认保留最近 3 组 tool uses，并以 placeholder 告诉模型内容已移除；可选连工具输入一起清除。官方同时说明 server-side compaction 是长会话的主要策略，tool clearing 是更细粒度补充。

参考：<https://platform.claude.com/docs/en/build-with-claude/context-editing>

### 2.2 OpenAI Agents SDK / Responses：截断与 compaction 分层

OpenAI Agents SDK 暴露 `truncation=auto`，允许 Responses API 在溢出前丢弃最旧 conversation items；也支持 server-side `context_management` compaction。`OpenAIResponsesCompactionSession` 是另一条路径：在轮次之间调用 compaction，并用压缩后的 items 重写会话输入历史。持久 Session 与每次运行的 token usage 分开计算。

参考：

- <https://openai.github.io/openai-agents-python/models/>
- <https://openai.github.io/openai-agents-python/usage/>
- <https://openai.github.io/openai-agents-js/guides/sessions/>

### 2.3 LangGraph：trim、delete、summary 是不同策略

LangGraph 把短期记忆容量处理分为：按 token 裁剪 messages、从状态删除旧 messages、用滚动 summary 替换旧历史，以及自定义过滤。其示例强调裁剪必须保持合法消息边界，并通常保留最近消息；summary 用来弥补纯删除的信息损失。

参考：<https://docs.langchain.com/oss/python/langgraph/add-memory>

## 3. 共同模式

成熟方案没有把“完整持久历史”和“每次发给模型的上下文”视为同一份数据：

1. 完整历史可以留在 Session/trace；
2. 模型输入按 token 阈值动态构造；
3. 最近工具交换保持完整；
4. 旧工具正文最先清除或替换为 placeholder/residue；
5. 更长期自然语言历史再做 truncation 或 semantic compaction；
6. 所有裁剪保持 provider 所需的合法消息/工具边界。

## 4. 对本项目的建议（尚未进入实现）

后续测试证明仅做 closed-exchange reduction 仍不充分：模型需要稳定 Working Set，而不是依赖最新 tool result 暂存源码。优先统一设计 provider-neutral 的 deterministic reduction 与 Working Context，而不是立即加入 LLM summary：

- 未闭合 ModelStep/ToolExchange 永远完整保留；
- 最近若干（建议从 4 组开始用 fixture 校准）闭合工具交换保留原始 provider 形式；
- 更老的闭合交换成对移除 call/result，并转为已有 Historical Residue；不得只删一侧；
- read/search/command 等继续使用各自 deterministic reducer，旧源码和 stdout 不进入 summary；
- Runtime Snapshot、current UserMessage、changed paths、validation 和 latest failure 保持最低集合；
- reduction 发生在临时 Context View，不改写 `events.jsonl`，不新增 artifact；
- 在阈值前主动触发并记录 BudgetReport，而不是等到 build 已经无法继续；
- semantic compaction 只在长 Session 的自然语言历史经实测仍不足时单独设计。
- 同一 SHA 的相关 read ranges 应合并为有界稳定代码工作集；新读取不能无条件覆盖此前互补范围；
- command stdout 中用于代码观察的内容不能成为另一套长期工作集，read/search/command 都应由统一选择层决定下一步正文；
- slice continuation 需要稳定 checkpoint，至少区分 Runtime 客观状态、模型声明和下一步执行提示。

## 5. 待 Technical Design 决定

- “最近 N 组”应按 tool use、ModelStep 还是 token 数定义；
- provider Chat Completions 对移除旧闭合 call/result 后的合法消息边界测试；
- residue 聚合是逐调用 placeholder，还是按工具/path 合并成阅读与搜索覆盖摘要；
- 提前触发阈值、目标释放量与模型 capability 配置；
- Context reduction 后如何向模型明确“旧工具正文已省略但客观结果仍在 residue”。

本调研不建议直接提高 22k 输入预算掩盖增长问题，也不建议把所有工具正文持久保留在每次请求中。
