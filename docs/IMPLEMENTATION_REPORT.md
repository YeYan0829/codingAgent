# 实现报告

## 本次实现概览

本次实现了 CodeAgent Runtime v0.1，并在策略修正中调整了敏感文件列表展示方式。项目包含 Typer CLI、session 存储、ReAct-style runtime、FakeLLM、只读工具、安全策略、workspace path guard、事件日志和 pytest。

## 分模块说明

Interface：`codeagent.cli` 提供 `start`、`ask`、`list-sessions`、`resume`，交互命令放在 `interface.commands`。`resume` 会在进入交互前显示 session 概览和最近历史，方便确认确实加载了旧会话。

Session：`SessionStore` 创建 `.codeagent/sessions/<session_id>/`，维护 `meta.json`、append-only `events.jsonl` 和 `transcript.md`。

Runtime：`AgentRunner` 控制每个 turn，处理模型响应、工具请求、policy、approval、工具执行、事件写入和最大步数。

Agent / Skill：提供只读仓库探索角色和 repo exploration 策略片段。

Model Gateway：定义 `LLMResponse`、`LLMToolCall`、`BaseModelClient`，实现 FakeLLM，预留 OpenAIClient。

Context / Memory：`ContextBuilder` 读取 prompt、tool policy、最近事件和工具 observation，不做长期记忆。

Tool / MCP：本地 `ToolRegistry` 注册只读文件工具和只读 git 工具，没有真实 MCP。`list_dir`、`show_tree`、`find_files` 遵循 `SensitiveListingMode`，默认显示敏感文件名并标记 content blocked。

Safety：`PathGuard` 限制 workspace 内访问并拒绝敏感内容读取，`sensitive.py` 定义敏感文件名规则、模板文件例外和 listing 模式，`DefaultPolicy` 管控权限。

Workspace：`Workspace` 封装 root 与 path guard。

Observability：所有关键动作写入 events，并同步 human-readable transcript。

## 核心数据结构

- `SessionEvent`：包含 `ts`、`type`、`payload`。
- `LLMToolCall`：包含 `call_id`、`name`、`arguments`。
- `LLMResponse`：包含 `text` 和 `tool_calls`。
- `ToolSpec`：包含 `name`、`description`、`permission_level`、`schema`、`handler`。
- `ToolResult`：包含 `ok`、`content`、`error`、`truncated`、`metadata`。
- `SensitiveListingMode`：包含 `SHOW_MARKED`、`REDACT_NAME`、`HIDE`。

## 单元测试

已实现 session、path guard、sensitive files、filesystem tools、policy、FakeLLM runner 六组 pytest。敏感文件测试覆盖默认显示标记、读取拒绝、搜索跳过、模板文件可读，以及 REDACT_NAME / HIDE 的基本行为。

## 功能测试

建议运行：

```bash
python -m pip install -e ".[dev]"
pytest -q
codeagent ask . "帮我看看这个项目结构"
```

## 当前没有做什么

没有真实 LLM，没有写文件能力，没有任意 shell，没有 Docker sandbox，没有 benchmark harness，没有真实 MCP，也没有 secret scanner。当前敏感识别仍然基于文件名规则。

## 下一步建议

优先接 OpenAI function calling / tool calling adapter；然后加入受控 test runner；再考虑 patch draft、approval-based apply_patch 和 git worktree sandbox。
