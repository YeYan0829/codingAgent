# 设计决策

## 为什么叫 CodeAgent Runtime，而不是 SafeCode Agent

项目核心是 agent harness / runtime，而不是单一安全卖点。安全是基础设施，但 runtime 还要负责 session、工具调度、上下文、日志和扩展边界。

## 为什么 v0.1 不开放 shell

任意 shell 的风险和复杂度很高，会立刻涉及写文件、网络、系统状态和权限边界。v0.1 先验证只读 tool loop 和审计链路。

## 为什么使用 append-only events.jsonl

append-only 事件更适合恢复、审计和调试。`transcript.md` 可以给人读，但程序应依赖结构化事件。

## 为什么先实现 FakeLLM

FakeLLM 让项目在没有 API key 的环境中也能运行和测试。它保证 runtime loop 的行为可重复，适合单元测试和 smoke test。

## 为什么 Safety 必须在 runtime 强制

prompt 只能提醒模型，不能作为真正边界。真正拒绝敏感内容读取、路径逃逸和危险权限必须发生在 runtime / safety 层。

## 为什么默认显示敏感文件存在

v0.1 默认采用 `SensitiveListingMode.SHOW_MARKED`：目录列表可以显示 `.env`、`id_rsa`、`secret.json` 这类敏感文件存在，但会标记 `[sensitive, content blocked]` 或 `[sensitive-name, content blocked]`，并继续禁止读取内容。

这个选择是为了平衡 coding agent 的项目理解能力和 secret 内容保护。很多项目结构判断需要知道是否存在环境配置、密钥文件或凭据相关文件；但安全边界的核心是禁止读取 secret value，而不是默认让 agent 假装这些文件不存在。

更严格的名称隐藏可以作为 strict privacy mode 扩展：`REDACT_NAME` 会隐藏具体文件名，`HIDE` 会完全隐藏条目。它们不是 v0.1 默认行为。

## 为什么把 CLI outer loop 和 Agent inner loop 分开

CLI 负责持续交互，agent inner loop 只处理一次用户消息。这样模型完成任务不会导致 CLI 退出，也便于将来接入不同界面。

## 为什么先做 readonly exploration

仓库探索是 coding agent 的基础能力，风险低，能验证工具、上下文、session、策略和日志。patch、benchmark 和 shell 应在基础设施稳定后再扩展。
