# 实现报告

## 当前实现概览

当前版本在 v0.2 readonly runtime 和 DeepSeek tool calling 的基础上，增加了 session UX / storage 优化：

- 新 session 默认写入用户级 `~/.codeagent/sessions/<workspace-key>/<session_id>/`。
- 支持 `CODEAGENT_SESSION_ROOT` 和 `--session-root` 指定 session 存储根目录。
- 兼容读取旧的 `<workspace>/.codeagent/sessions/<session_id>/`。
- `meta.json` 新增 `title`、`session_root`、`workspace_key`。
- session title 默认由第一条用户消息生成。
- `list-sessions` 和 `resume` 默认面向统一 session root 下的所有 session，并显示 workspace。
- `--workspace <path>` 用于过滤某个 workspace；UTF-8 TTY 使用上下键选择器，Windows GBK 等非 UTF-8 终端和非 TTY 使用编号 fallback。

## 分模块说明

Interface：`codeagent.cli` 支持 `--provider`、`--model`、`--session-root`。`list-sessions` 展示标题和 workspace，默认列出全局 session root 下的所有 session；`resume` 既支持按 id 恢复，也支持无 id 全局选择。

Session：`SessionStore` 负责用户级 session root、workspace hash 分桶、旧 session fallback、title 生成、events 和 transcript。

Runtime：`AgentRunner` 仍然构造 `ModelRequest`，传入 messages、tools、model 参数。工具调用仍经过 ToolRegistry、Policy、Approval。

Model Gateway：`FakeLLM` 和 `DeepSeekClient` 保持不变。DeepSeekClient 使用 OpenAI SDK，读取 `DEEPSEEK_API_KEY`，默认模型 `deepseek-v4-flash`。

Safety：真实 LLM 没有直接读文件或执行工具能力。所有工具仍受 path guard、敏感文件策略、policy 和 approval gate 约束。

## 核心数据结构

- `ModelRequest`：模型请求，包含 messages、tools、tool_choice、model、temperature、max_tokens。
- `LLMResponse`：内部统一模型响应。
- `LLMToolCall`：内部统一工具调用。
- `ToolSpec`：工具定义，负责导出 OpenAI-compatible tool schema。
- `SessionStore`：session 文件布局、meta、events、transcript、title 和 list/load。

## 测试结果

单元测试覆盖 FakeLLM、DeepSeekClient mock、tool schema、provider factory、CLI model/session options、tool-call history 配对、session root、全局 session listing 和 resume selection fallback。

## 当前没有做什么

没有写文件能力，没有任意 shell，没有 Docker sandbox，没有 benchmark harness，没有真实 MCP，没有 patch 功能。DeepSeek strict mode 未启用。Session title 暂不调用 LLM 生成。

## 下一步建议

进入 v0.3 前，可以先手动验证 `resume --workspace .` 的 TTY 上下键选择体验。随后再考虑 controlled test runner。
