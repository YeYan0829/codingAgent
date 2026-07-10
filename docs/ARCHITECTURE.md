# 架构说明

## 总体数据流

用户通过 Typer CLI 输入消息。CLI 负责交互和展示，不直接执行业务逻辑。消息进入 `AgentRunner` 后，runtime 写入 `user_message` 事件，调用 `ContextBuilder` 组装上下文消息，然后构造 `ModelRequest` 调用 model gateway。

模型返回 `LLMResponse`。如果包含 tool calls，runtime 写入 `assistant_tool_calls`，再逐个写入 `tool_requested`，通过 `DefaultPolicy` 判断权限。READ 工具直接执行；EXEC_READONLY 工具进入 approval；WRITE、NETWORK、DANGEROUS 被拒绝。工具结果写入 `tool_result`，作为 observation 回到下一轮上下文。

当模型返回普通文本时，runtime 写入 `assistant_message` 并结束当前 turn。

## Runtime 边界

`AgentRunner` 只认内部抽象：

- `ModelRequest`
- `LLMResponse`
- `LLMToolCall`
- `ModelTool`

它不接触 DeepSeek/OpenAI 的 Chat Completions tool schema，也不解析 provider response。工具执行仍由 ToolRegistry、Policy、Approval 和 tool handler 控制。

## Model Gateway

`ModelRequest` 隔离模型请求：

- `messages`：由 `ContextBuilder` 生成的上下文消息。
- `tools`：内部 `ModelTool` 列表，只包含 name、description、parameters。
- `tool_choice`：默认 `auto`。
- `model`、`temperature`、`max_tokens`：provider 参数。

DeepSeekClient 负责把 `ModelTool` 转成 Chat Completions `tools` 格式，并把 provider 返回的 tool_calls 转回 `LLMToolCall`。

## ContextBuilder

`ContextBuilder` 负责内部消息语义。真实 Chat Completions tool calling 需要 assistant tool_calls 与 tool message 通过 `tool_call_id` 配对，因此 `ContextBuilder` 会根据 session events 重建：

```python
{"role": "assistant", "content": None, "tool_calls": [...]}
{"role": "tool", "tool_call_id": "...", "content": "..."}
```

这属于上下文消息语义，不属于 runtime 工具执行逻辑。

## Tool Schema 链路

工具定义转换链路是：

```text
ToolSpec -> ModelTool -> DeepSeek/OpenAI-compatible tools schema
```

`ToolSpec` 保留 runtime 执行信息，例如权限和 handler。`ModelTool` 是给模型看的轻量描述。provider-specific schema 转换只发生在具体 client adapter 中。

## Tool Call 执行链路

一次 tool call 的完整链路是：

1. `AgentRunner` 接收 `LLMToolCall`。
2. 写入 `assistant_tool_calls` 和 `tool_requested`。
3. 从 `ToolRegistry` 查找 `ToolSpec`。
4. `DefaultPolicy` 根据 `PermissionLevel` 返回 allow、ask 或 deny。
5. ask 时调用 `ApprovalGate`。
6. 通过工具 handler 执行。
7. 写入 `tool_result` 或 `tool_denied`。
8. 将结果作为 observation 放回下一轮上下文。

这个设计保证真实 LLM 只提出请求，不直接执行工具。
