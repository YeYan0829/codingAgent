# 模块说明

## Interface

职责：提供用户入口和展示层。

v0.1 实现：`codeagent.cli` 使用 Typer 提供 `start`、`ask`、`list-sessions`、`resume`，用 rich 展示工具请求、结果和 assistant 回复。

当前边界：不直接执行业务逻辑，只创建 workspace、session、runner。

未来扩展：更好的交互 UI、命令历史、流式展示。

## Session

职责：管理会话状态和审计日志。

v0.1 实现：每个 session 存在 `.codeagent/sessions/<session_id>/`，包含 `meta.json`、append-only `events.jsonl` 和 `transcript.md`。

当前边界：只做本地文件存储，不做并发锁和远端同步。

未来扩展：session 索引、恢复检查、事件迁移。

## Runtime

职责：执行权中心，调度 ReAct-style loop。

v0.1 实现：`AgentRunner` 控制每个 turn，统一处理 model、policy、approval、tool registry 和日志。

当前边界：只支持同步调用，最大步数由配置控制。

未来扩展：流式事件、取消、并发工具、错误恢复策略。

## Agent

职责：定义智能角色和行为边界。

v0.1 实现：只读仓库探索 agent 的角色文本。

当前边界：不把执行权放入 agent，agent 只描述意图和边界。

未来扩展：bugfix、test generation、patch review 等角色。

## Skill

职责：沉淀任务方法。

v0.1 实现：`repo_exploration` 描述探索仓库的优先级。

当前边界：只是轻量策略片段，不是独立执行器。

未来扩展：技能注册、技能选择、技能参数化。

## Model Gateway

职责：统一模型响应结构。

v0.1 实现：`BaseModelClient`、`LLMResponse`、`LLMToolCall`、`FakeLLM`，并预留 `OpenAIClient`。

当前边界：默认不需要 API key，不访问真实网络模型。

未来扩展：OpenAI tool calling adapter、多模型路由、重试与速率限制。

## Context / Memory

职责：构建模型上下文。

v0.1 实现：`ContextBuilder` 读取最近 session events、system prompt、tool policy 和 observations。

当前边界：不做长期记忆、向量库或复杂压缩。

未来扩展：context compression、project index、long-term memory。

## Tool / MCP

职责：封装本地能力。

v0.1 实现：本地 `ToolRegistry`，工具包含 schema、description、permission_level 和 handler。文件工具支持 `SensitiveListingMode`，默认在列表中显示敏感文件存在并标记 content blocked。

当前边界：没有真实 MCP 协议，也没有写工具。

未来扩展：MCP adapter、工具版本、工具 trace。

## Safety

职责：强制安全边界。

v0.1 实现：workspace path guard、敏感内容读取拒绝、敏感文件列表标记、权限策略和 approval gate。`.env.example`、`.env.sample`、`.env.template` 作为模板文件默认允许读取。

当前边界：没有容器隔离，也没有系统调用沙箱；敏感检测仍是文件名规则，不是 secret scanner。

未来扩展：strict privacy mode、secret scanner、git worktree、Docker sandbox、策略配置文件。

## Workspace

职责：表示工作区根目录和路径隔离。

v0.1 实现：`Workspace` 持有 root 与 `PathGuard`，所有文件访问必须 resolve 到 workspace 内。

当前边界：单 workspace，无 worktree 管理。

未来扩展：多 workspace、临时 worktree、项目索引。

## Report / Eval / Observability

职责：记录可审计过程。

v0.1 实现：`events.jsonl` 给程序恢复和审计，`transcript.md` 给人阅读。

当前边界：没有 trace viewer 或 benchmark report。

未来扩展：benchmark harness、eval report、可视化 trace。
