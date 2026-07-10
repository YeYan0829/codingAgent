# 架构说明

## 总体数据流

用户通过 Typer CLI 输入消息。CLI 负责交互和展示，不直接执行业务逻辑。消息进入 `AgentRunner` 后，runtime 写入 `user_message` 事件，调用 `ContextBuilder` 组装 system prompt、tool policy、历史消息和工具 observation，然后构造 `ModelRequest` 调用 model gateway。

模型返回 `LLMResponse`。如果包含 tool calls，runtime 写入 `assistant_tool_calls`，再逐个写入 `tool_requested`，通过 `DefaultPolicy` 判断权限。READ 工具直接执行；EXEC_READONLY 工具进入 approval；WRITE、NETWORK、DANGEROUS 被拒绝。工具结果写入 `tool_result`，作为 observation 回到下一轮上下文。

当模型返回普通文本时，runtime 写入 `assistant_message` 并结束当前 turn。

## CLI Outer Loop 与 Agent Inner Loop

CLI outer loop 是用户交互循环：读取输入、处理 `/exit`、`/status`、`/tools`、`/log`、`/call` 等命令，并把普通消息交给 runtime。

Agent inner loop 是一次用户消息内部的 ReAct-style loop。它最多运行 `max_steps_per_turn=8` 步，负责模型调用、工具请求、安全判断、approval、工具执行和 observation 回填。

## Model Gateway

v0.2 使用 `ModelRequest` 隔离模型请求：

- `messages`：上下文消息。
- `tools`：OpenAI / DeepSeek-compatible tool schema。
- `tool_choice`：默认 `auto`。
- `model`、`temperature`、`max_tokens`：provider 内参数。

默认 provider 是 `fake`。DeepSeek provider 使用 OpenAI SDK 和 `https://api.deepseek.com`，读取 `DEEPSEEK_API_KEY`。

## Tool Call 配对

真实 Chat Completions tool calling 需要 assistant tool_calls 与 tool message 通过 `tool_call_id` 配对。v0.2 新增 `assistant_tool_calls` 事件，`ContextBuilder` 会重建：

```python
{"role": "assistant", "content": None, "tool_calls": [...]}
{"role": "tool", "tool_call_id": "...", "content": "..."}
```

FakeLLM 也继续兼容这种结构。

## Tool Call 执行链路

一次 tool call 的完整链路是：

1. `AgentRunner` 接收 `LLMToolCall`。
2. 写入 `assistant_tool_calls` 和 `tool_requested`。
3. 从 `ToolRegistry` 查找工具。
4. `DefaultPolicy` 根据 `PermissionLevel` 返回 allow、ask 或 deny。
5. ask 时调用 `ApprovalGate`。
6. 通过工具 handler 执行。
7. 写入 `tool_result` 或 `tool_denied`。
8. 将结果作为 observation 放回上下文。

这个设计保证真实 LLM 只提出请求，不直接执行工具。
