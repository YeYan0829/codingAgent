# 架构说明

## 总体数据流

用户通过 Typer CLI 输入消息。CLI 只负责交互和展示，不直接执行业务逻辑。消息进入 `AgentRunner` 后，runtime 写入 `user_message` 事件，调用 `ContextBuilder` 组装 system prompt、tool policy、历史消息和工具 observation，然后调用 model gateway。

模型返回 `LLMResponse`。如果包含 tool calls，runtime 会逐个写入 `tool_requested`，再通过 `DefaultPolicy` 判断权限。如果是 READ 工具则直接执行；如果是 EXEC_READONLY 工具则进入 approval；如果是 WRITE、NETWORK、DANGEROUS 则拒绝。工具结果写入 `tool_result`，作为 observation 回到下一轮上下文。

当模型返回普通文本时，runtime 写入 `assistant_message` 并结束当前 turn。

## CLI Outer Loop 与 Agent Inner Loop

CLI outer loop 是用户交互循环：读取输入、处理 `/exit`、`/status`、`/tools`、`/log`、`/call` 等命令，并把普通消息交给 runtime。

Agent inner loop 是一次用户消息内部的 ReAct-style loop。它最多运行 `max_steps_per_turn=8` 步，负责模型调用、工具请求、安全判断、approval、工具执行和 observation 回填。

两者分离后，模型说“任务完成”只会结束当前 turn，不会退出 CLI。

## ReAct-style Loop 的落地方式

本项目没有让 LLM 直接执行工具。LLM 只返回结构化的 `LLMToolCall`，runtime 才是执行权中心。FakeLLM 用确定性规则模拟模型行为，先列目录，再根据 observation 决定是否读取 README，最后输出中文总结。

## Tool Call 执行链路

一次 tool call 的完整链路是：

1. `AgentRunner` 接收 `LLMToolCall`。
2. 写入 `tool_requested`。
3. 从 `ToolRegistry` 查找工具。
4. `DefaultPolicy` 根据 `PermissionLevel` 返回 allow、ask 或 deny。
5. ask 时调用 `ApprovalGate`。
6. 通过工具 handler 执行。
7. 写入 `tool_result` 或 `tool_denied`。
8. 将结果作为 observation 放回上下文。

这个设计保证 agent、model、tool 三者不会直接互相绕过 runtime。
