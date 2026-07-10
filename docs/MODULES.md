# 模块说明

## Interface

职责：提供用户入口和展示层。

v0.2 实现：`codeagent.cli` 提供 `start`、`ask`、`list-sessions`、`resume`，并支持 `--provider` / `--model`。默认 provider 是 `fake`。CLI 展示工具调用时会打印参数，例如 `read_file {"path": "sort_utils.py"}`。

当前边界：不直接执行业务逻辑，只创建 workspace、session、runner。

## Session

职责：管理会话状态和审计日志。

v0.2 实现：每个 session 存在 workspace-local 的 `.codeagent/sessions/<session_id>/`，包含 `meta.json`、append-only `events.jsonl` 和 `transcript.md`。`meta.json` 记录 provider/model。

当前边界：只做本地文件存储，不做并发锁、远端同步或全局 session index。因此跨 workspace 查看历史需要先知道 workspace。

## Runtime

职责：执行权中心，调度 ReAct-style loop。

v0.2 实现：`AgentRunner` 构造 `ModelRequest`，传入 messages、tools 和 model 参数。工具调用仍必须经过 ToolRegistry、Policy 和 Approval。

当前边界：只支持同步非流式调用。

## Model Gateway

职责：隔离 provider API 差异。

v0.2 实现：`FakeLLM`、`DeepSeekClient`、`ModelRequest`、`build_model_client`。DeepSeek 使用 OpenAI SDK，`base_url=https://api.deepseek.com`，key 来自 `DEEPSEEK_API_KEY`，默认模型 `deepseek-v4-flash`。

当前边界：OpenAIClient 仍是占位；DeepSeek strict mode 未启用，thinking mode 默认关闭。

## Tool / MCP

职责：封装本地能力。

v0.2 实现：本地 `ToolRegistry` 可以导出 OpenAI / DeepSeek-compatible tools 格式：

```json
{"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
```

当前边界：没有真实 MCP 协议，也没有写工具。

## Safety

职责：强制安全边界。

v0.2 实现：workspace path guard、敏感内容读取拒绝、敏感文件列表标记、权限策略和 approval gate。真实 LLM 不获得任何直接文件或命令执行能力。

当前边界：没有容器隔离，也没有系统调用沙箱；敏感检测仍是文件名规则，不是 secret scanner。

## Context / Memory

职责：构建模型上下文。

v0.2 实现：`ContextBuilder` 读取最近 session events，并重建 assistant tool_calls 与 tool result 的配对历史。

当前边界：不做长期记忆、向量库或复杂压缩。

## Workspace

职责：表示工作区根目录和路径隔离。

v0.2 实现：`Workspace` 持有 root 与 `PathGuard`，所有文件访问必须 resolve 到 workspace 内。

## Report / Eval / Observability

职责：记录可审计过程。

v0.2 实现：`events.jsonl` 记录 `assistant_tool_calls`、`tool_requested`、`tool_result` 等事件，`transcript.md` 给人阅读。
