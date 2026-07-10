# 设计决策

## 为什么 v0.2 接 DeepSeek 而不改变 runtime

runtime 是执行权中心。真实 LLM 只应该提出 tool call，不能直接读文件或执行工具。因此 v0.2 只在 Model Gateway、tool schema 和上下文重建上扩展，不重写 ToolRegistry、Policy、Approval。

## 为什么默认仍然是 fake

默认 fake 能保证没有 API key 时仍可运行测试和 demo。真实 provider 通过 `--provider deepseek` 显式启用，避免把开发体验绑在外部网络和 key 上。

## 为什么 session 仍然存放在 workspace 内

v0.2 继续使用 workspace-local session store：`<workspace>/.codeagent/sessions/<session_id>/`。这样每个项目的审计日志、transcript 和工具历史都跟随项目目录，路径守卫和 session 绑定关系也更直观。

缺点是 session 会分散在不同 workspace 下，跨项目查看历史不够方便。后续可以增加全局 session index，只记录 session id、workspace、last_active_at 等索引信息；原始事件日志仍保留在 workspace 内，避免把项目上下文和审计数据混到一个全局目录。

## 为什么 provider 和 model 分开

provider 决定 API 协议、base_url 和 key；model 是该 provider 下的具体模型编号。DeepSeek provider 默认使用 `deepseek-v4-flash`，也可指定 `deepseek-v4-pro`。

## 为什么使用 DEEPSEEK_API_KEY

DeepSeek 官方示例使用 `DEEPSEEK_API_KEY`。key 不进入代码、不进入 session、不进入工具结果。

## 为什么不默认 deepseek-chat

DeepSeek 文档标注 `deepseek-chat` 和 `deepseek-reasoner` 将弃用。v0.2 默认使用 `deepseek-v4-flash`，文档示例也使用 v4 系列。

## 为什么不启用 strict mode

strict mode 对 JSON schema 有额外约束，属于后续加强项。v0.2 先使用普通 tool calling，降低真实 LLM 接入复杂度。

## 为什么关闭 thinking mode

第一版重点是验证只读 tool loop，而不是推理模式调优。DeepSeekClient 默认传入 `extra_body={"thinking": {"type": "disabled"}}`，如果 SDK 不兼容则回退到最小请求。

## 为什么保留敏感文件 listing 标记

Safety 的核心边界是禁止读取 secret value，而不是让 agent 假装敏感配置文件不存在。默认显示敏感文件存在并标记 content blocked。
