# 设计决策

## 为什么默认 session 不再写入 workspace

会话历史是用户级运行记录，不是项目源码的一部分。默认把 session 写到用户级 `~/.codeagent/sessions/<workspace-key>/<session_id>/`，避免每个项目目录都出现 `.codeagent/`。

`workspace-key` 由 workspace 名称和路径 hash 组成，避免不同目录同名项目冲突。`meta.json` 仍记录真实 workspace 路径，因此全局 session 列表和选择器可以显示每条会话来自哪个项目。

## 为什么保留 `--session-root`

`CODEAGENT_SESSION_ROOT` 和 `--session-root` 的含义是“统一会话库的位置”，不是 workspace 过滤条件。它允许用户把 session 放到指定磁盘、加密目录或临时目录。CLI 参数优先级高于环境变量。

## 为什么兼容旧 workspace-local session

v0.2 之前的 session 存在 `<workspace>/.codeagent/sessions/<session_id>/`。新版 `SessionStore.load()` 会在统一 session root 找不到时回退到旧路径，避免历史会话突然不可恢复。

## 为什么标题先用第一条用户消息生成

session 需要在刚开始时就具备可读性，但不能让 session 创建依赖额外 LLM 调用。当前标题由第一条用户消息裁剪生成，稳定、便宜、可测试。后续可以增加 LLM-generated title，但应作为可选增强。

## 为什么 resume 支持选择器

完全依靠 session id 不适合人类使用。`list-sessions` 默认显示统一会话库里的所有 session，并展示 title、id、model 和 workspace。`resume` 不带 id 时默认从全部 session 里选择；传 `--workspace <path>` 时才过滤到某个 workspace。UTF-8 TTY 中使用上下键选择器；Windows GBK 等非 UTF-8 终端和非交互环境退回编号输入，避免中文显示乱码。

## 为什么默认仍然是 fake

默认 fake 能保证没有 API key 时仍可运行测试和 demo。真实 provider 通过 `--provider deepseek` 显式启用，避免把开发体验绑在外部网络和 key 上。

## 为什么引入 ModelTool

`ToolSpec` 是 runtime/tool 层的完整工具定义，包含 `permission_level` 和 `handler`。模型并不需要知道这些执行细节，它只需要工具的 `name`、`description` 和 JSON schema。

因此 v0.2.2 引入 `ModelTool` 作为 model gateway 的轻量内部工具描述：

```text
ToolSpec -> ModelTool -> provider-specific tool schema
```

`AgentRunner` 只把 `ModelTool` 放进 `ModelRequest`，不再调用 `as_openai_tools()` 或接触 OpenAI/DeepSeek tool schema。DeepSeekClient 负责把 `ModelTool` 转成 Chat Completions 的 `tools` 格式。

这个设计避免两种泄漏：

- Runtime 不知道 DeepSeek/OpenAI 的 provider schema。
- Model Gateway 不需要拿到完整 `ToolSpec.handler` 或权限信息。

## 为什么 DeepSeek 只接入 Model Gateway

runtime 是执行权中心。真实 LLM 只应该提出 tool call，不能直接读文件或执行工具。因此 DeepSeekClient 只负责模型请求和响应转换，不触碰 ToolRegistry、Policy、Approval 或文件系统。

## 为什么保留敏感文件 listing 标记

Safety 的核心边界是禁止读取 secret value，而不是让 agent 假装敏感配置文件不存在。默认显示敏感文件存在并标记 content blocked。
