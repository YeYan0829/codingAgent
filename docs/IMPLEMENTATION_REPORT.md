# 实现报告

## 本次实现概览

v0.2 在保持 v0.1 readonly runtime 稳定的基础上，新增 DeepSeek provider 和 Chat Completions tool calling 接入。默认 provider 仍然是 fake。

## 分模块说明

Interface：`codeagent.cli` 支持 `--provider` 和 `--model`。`ask` / `start` 可选择 DeepSeek，`resume` 默认读取 session meta，也允许覆盖 provider/model。CLI 的工具调用展示会打印工具参数，方便确认真实 LLM 读取了哪些文件。

Session：`meta.json` 新增 provider 字段，model 记录实际模型编号。新增 `assistant_tool_calls` 事件用于重建真实 tool-call 历史。session 仍然是 workspace-local 存储，暂不提供全局 index。

Runtime：`AgentRunner` 构造 `ModelRequest`，传入 messages、tools、model 参数。工具调用仍经过 ToolRegistry、Policy、Approval。

Model Gateway：新增 `ModelRequest`、`DeepSeekClient` 和 factory。DeepSeekClient 使用 OpenAI SDK，读取 `DEEPSEEK_API_KEY`，默认模型 `deepseek-v4-flash`，thinking mode 默认关闭。

Tool / MCP：`ToolSpec.to_openai_tool()` 和 `ToolRegistry.as_openai_tools()` 负责输出 DeepSeek/OpenAI-compatible tools 格式。未实现 MCP。

Context / Memory：`ContextBuilder` 根据 `assistant_tool_calls` 和 `tool_result` 重建合法的 assistant/tool message 配对。

Safety：真实 LLM 没有直接读文件或执行工具能力。所有工具仍受 path guard、敏感文件策略、policy 和 approval gate 约束。

Examples：新增 `examples/buggy-repos/tiny-sort-bug`，用于真实 LLM 只读 bug 分析。

## 核心数据结构

- `ModelRequest`：模型请求，包含 messages、tools、tool_choice、model、temperature、max_tokens。
- `LLMResponse`：内部统一模型响应。
- `LLMToolCall`：内部统一工具调用。
- `ToolSpec`：工具定义，负责导出 OpenAI-compatible tool schema。

## 测试结果

单元测试覆盖 FakeLLM、DeepSeekClient mock、tool schema、provider factory、CLI model options、tool-call history 配对等。

## 当前没有做什么

没有写文件能力，没有任意 shell，没有 Docker sandbox，没有 benchmark harness，没有真实 MCP，没有 patch 功能。DeepSeek strict mode 未启用。

## 下一步建议

先用 `tiny-sort-bug` 做真实 DeepSeek smoke test；随后可以加强 tool-call 错误恢复、全局 session index 和上下文压缩，再考虑受控 test runner。
