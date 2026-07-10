# 模块说明

## Interface

`codeagent.cli` 提供 `start`、`ask`、`list-sessions`、`resume`。CLI 支持 `--provider`、`--model`、`--session-root`。

CLI 不直接执行业务逻辑，只创建 workspace、session store 和 runner。工具调用展示会打印参数，例如 `read_file {"path": "sort_utils.py"}`。

`list-sessions` 和 `resume` 默认面向统一 session root 下的全部 session。传 `--workspace <workspace>` 时才过滤到某个 workspace。`resume <session_id>` 会从全局 session root 中按 id 找到 workspace；`resume <session_id> <workspace>` 作为旧用法仍保持兼容。

## Session

`SessionStore` 管理 `meta.json`、append-only `events.jsonl` 和 `transcript.md`。

默认路径：

```text
~/.codeagent/sessions/<workspace-key>/<session_id>/
```

可配置路径：

- 环境变量：`CODEAGENT_SESSION_ROOT`
- CLI 参数：`--session-root`

`meta.json` 记录 `session_id`、`title`、`workspace`、`session_root`、`workspace_key`、`provider`、`model` 等信息，不记录 API key。旧路径 `<workspace>/.codeagent/sessions/<session_id>/` 仍作为 load fallback。

## Runtime

`AgentRunner` 是执行权中心，负责一次用户 turn 内部的 ReAct-style loop。它只认内部模型抽象：`ModelRequest`、`LLMResponse`、`LLMToolCall` 和 `ModelTool`。工具调用仍必须经过 ToolRegistry、Policy 和 Approval。

## Model Gateway

`ModelRequest` 隔离 provider 请求结构。当前 provider：

- `fake`
- `deepseek`

`ModelRequest.tools` 是内部 `ModelTool` 列表，不是 DeepSeek/OpenAI schema。DeepSeekClient 使用 OpenAI SDK，`base_url=https://api.deepseek.com`，key 来自 `DEEPSEEK_API_KEY`，默认模型 `deepseek-v4-flash`。DeepSeekClient 内部负责把 `ModelTool` 转成 Chat Completions tools schema。

## Tool / MCP

本地 `ToolRegistry` 管理 `ToolSpec`。`ToolSpec` 包含 runtime 执行信息：权限、schema 和 handler。

转换链路：

```text
ToolSpec -> ModelTool -> provider tool schema
```

当前没有真实 MCP 协议，也没有写工具。

## Safety

实现 workspace path guard、敏感内容读取拒绝、敏感文件列表标记、权限策略和 approval gate。真实 LLM 不获得直接文件或命令执行能力。

## Context / Memory

`ContextBuilder` 读取最近 session events，并重建 assistant tool_calls 与 tool result 的配对历史。

当前不做长期记忆、向量库或复杂压缩。

## Workspace

`Workspace` 持有 root 与 `PathGuard`，所有文件访问必须 resolve 到 workspace 内。

## Observability

`events.jsonl` 用于程序恢复和审计，`transcript.md` 给人阅读。session title 提升 `list-sessions` 和 `resume` 的可读性。
