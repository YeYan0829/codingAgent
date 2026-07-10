# 实现报告

## 当前实现概览

当前版本保持 readonly runtime 和 DeepSeek tool calling 能力，并完成两项 v0.2.x 稳定性优化：

- v0.2.1：session 统一存储、全局 list/resume、session title、旧 session fallback。
- v0.2.2：引入 `ModelTool`，把 provider-specific tool schema 转换从 runtime 边界移到 model client adapter。

## 分模块说明

Interface：`codeagent.cli` 支持 `--provider`、`--model`、`--session-root`。`list-sessions` 展示标题和 workspace，默认列出全局 session root 下的所有 session；`resume` 既支持按 id 恢复，也支持无 id 全局选择。

Session：`SessionStore` 负责用户级 session root、workspace hash 分桶、旧 session fallback、title 生成、events 和 transcript。

Runtime：`AgentRunner` 构造 `ModelRequest`，传入 messages、`ModelTool` 列表和 model 参数。工具调用仍经过 ToolRegistry、Policy、Approval。Runtime 不接触 DeepSeek/OpenAI tool schema。

Model Gateway：`ModelRequest` 使用内部 `ModelTool` 描述工具。`DeepSeekClient` 使用 OpenAI SDK，读取 `DEEPSEEK_API_KEY`，默认模型 `deepseek-v4-flash`，并负责把 `ModelTool` 转成 Chat Completions tools schema。

Tool：`ToolSpec` 仍是工具执行定义，包含 permission level、schema 和 handler。`ToolRegistry.as_model_tools()` 导出给模型看的轻量工具描述。

Safety：真实 LLM 没有直接读文件或执行工具能力。所有工具仍受 path guard、敏感文件策略、policy 和 approval gate 约束。

## 核心数据结构

- `ModelTool`：模型可见工具描述，包含 name、description、parameters。
- `ModelRequest`：模型请求，包含 messages、tools、tool_choice、model、temperature、max_tokens。
- `LLMResponse`：内部统一模型响应。
- `LLMToolCall`：内部统一工具调用。
- `ToolSpec`：runtime 工具定义，包含权限和 handler。
- `SessionStore`：session 文件布局、meta、events、transcript、title 和 list/load。

## 测试结果

单元测试覆盖 FakeLLM、DeepSeekClient mock、tool schema adapter、provider factory、CLI model/session options、tool-call history 配对、session root、全局 session listing、resume selection fallback，以及非交互 approval 自动拒绝。

## 当前没有做什么

没有写文件能力，没有任意 shell，没有 Docker sandbox，没有 benchmark harness，没有真实 MCP，没有 patch 功能。DeepSeek strict mode 未启用。Session title 暂不调用 LLM 生成。

## 下一步建议

进入 v0.3 时优先考虑 controlled test runner。写操作、patch、sandbox、benchmark 和 MCP 仍应作为后续独立阶段推进。
