# Product RPC

VS Code Extension 通过 newline-delimited stdio JSON-RPC 2.0 与 `codeagent-rpc` 通信。协议版本当前为 `1.0`。
每行是一个完整 JSON object；stdout 只承载协议，Runtime 日志和启动错误通过 stderr 进入
**CodeAgent Runtime** Output channel。

本文记录当前对 Extension 有用的维护契约，不承诺作为通用远程 API。RPC server 没有网络监听能力。

## 阅读约定

**Session** 是绑定一个源仓库的持久会话。

**UserTurn** 是 Session 中的一条真实用户请求。

**ModelStep** 是一次模型响应。

**Candidate** 是 Agent worktree 中尚未交付的修改。

**baseline** 是上一次已经接受的代码状态。

`current` 表示记录仍对应当前修改。`stale` 表示修改后记录已经过期。

**identity** 是一组用于确认对象没有被替换的客观值。

**fingerprint** 是配置内容的哈希摘要。

## 初始化

### `initialize`

请求可带 `protocolVersion`，省略时按 `1.0`。版本不匹配返回 product error。响应包含 Runtime 版本和 capability：

```json
{
  "protocolVersion": "1.0",
  "runtimeVersion": "0.5.0",
  "capabilities": {
    "readOnlyProductShell": false,
    "protectedOperations": true,
    "sessionList": true,
    "sessionDetail": true,
    "sessionEventsAfterSeq": true,
    "liveNotifications": true,
    "execution": true,
    "changesReview": true,
    "changesResolution": true,
    "interactiveApproval": true,
    "stop": true,
    "increaseTurnBudget": true
  }
}
```

当前 `RpcClient.start()` 等待 initialize 成功后标记 connected，但忽略返回的 runtimeVersion、protocolVersion
和 capabilities，没有按能力启用/禁用功能。服务端拒绝请求中的不支持协议版本；这不是 Runtime/Extension 包版本
兼容校验。使用配套版本是当前安装要求。没有单独的 ready/initialized 消息，也没有服务端强制初始化状态门禁。
客户端请求没有 timeout；存活但不响应的进程可能一直等待。

## Session 方法

| 方法 | 关键参数 | 行为 |
| --- | --- | --- |
| `session/list` | 可选 `workspace` | 不传时列出当前 Session Root 全部 Session；传入时过滤 workspace |
| `session/get` | `sessionId` | 返回 Product Read Model、timeline、changes/validation、budget 和 available actions |
| `session/events` | `sessionId`、可选 `afterSeq`/`limit` | 分页读取有界 Event；`afterSeq >= 0`，`1 <= limit <= 1000` |
| `session/create` | `workspace`、provider/model、reasoning 与可选预算 | 创建 Session 并冻结配置，不自动运行 |
| `session/run` | `sessionId`、`message` | 异步启动新的 UserTurn；返回 job/state，不等待模型完成 |
| `session/continue` | `sessionId` | 继续仍有剩余预算的未完成 UserTurn；正常自动切片不需要 Extension 调用 |
| `session/stop` | `sessionId` | 请求协作式停止；可停止 active job 或结束 idle budget waiting |
| `session/increaseBudget` | Session/Turn identity 与新上限 | 明确扩额并异步继续同一个 UserTurn |

Timeline 中失败的工具项带有 `failure`。其中包含分类、面向用户的标题、关键错误行、可选 exit code、
有界 output 和截断标记。字段来自已有 ToolResult，不是另一套执行结果。

`session/create` 当前支持 `fake`、`deepseek`、`glm`。预算约束为：

- `1 <= maxStepsPerTurn <= 100`；
- `maxStepsPerTurn <= maxModelStepsPerUserTurn <= 1000`；
- `maxTokens` 省略或位于 1～131072；
- temperature 省略或位于 0～2。

`reasoningEnabled` 是布尔值，默认 `false`。`reasoningEffort` 是可选字符串。
GLM-5.2 接受 `high`、`max`；GLM-5.3 接受 `low`、`high`、`max` 且不能关闭 reasoning；
DeepSeek V4 Flash/Pro 接受 `low`、`high`、`max`。官方 Extension 当前只把 GLM-5.2 加入产品 reasoning 模型白名单。
启用 reasoning 时，不支持的 provider、model 或档位会使创建请求失败。

`session/list` 和 `session/get` 返回 `reasoningEnabled` 与 `reasoningEffort`。隐藏推理正文不进入 Product Read Model。

`session/increaseBudget` 必须携带当前 `turnId` 和 `expectedLimit`，并且只能二选一提供 `newLimit` 或
`additionalSteps`。过期 identity、重复提交、运行中提交、布尔/非整数或不增长的上限都会拒绝。

## Approval

### `approval/resolve`

参数：`sessionId`、`approvalId`、布尔值 `allow`。Approval 必须属于当前 active job，Stop 后或过期 approval
不能再批准。响应只表示该 decision 已被当前 supervisor 接收；工具仍会在执行前完成资源 identity 复检。

Pending approval 通过 `session/get.pendingApproval` 获取。其 `summary` 是有界展示数据，编辑内容、expected SHA
等字段不会进入 Approval 卡片。

## Changes 方法

| 方法 | 参数 | 行为 |
| --- | --- | --- |
| `changes/get` | `sessionId` | 返回待审查或最近已应用的文件、逐文件 stat 和 validation summary |
| `changes/file` | `sessionId`、`path` | 返回单个 UTF-8 文件的前后文本，供原生 Diff 使用 |
| `changes/accept` | `sessionId` | 要求 idle、current validation 和安全 workspace，执行 identity 复检与三方合并 |
| `changes/discard` | `sessionId` | 要求 idle，重置 Candidate；tainted 状态允许进入受控 discard |

`validation.appliesToCurrentChanges` 表示最近成功的 validation 命令记录的修改序号与当前序号相同。
它不是“测试充分”或 official grader 通过，也不是实时文件树证明。实际 Accept 会再次核对工作区、baseline 和文件树。

`session/get.turns[].items` 还可能返回 `contextSummary`：它由 `context_condensed` Event 投影，并保留覆盖事件数、
摘要估算和 condenser 等诊断字段；官方 Extension 只将它显示为一行 Runtime/System 提示，不展示摘要正文和诊断。
截断恢复使用 `notice(kind=truncationRecovery)`，同一 UserTurn 内会聚合计数；Extension 同样只显示一行自动继续提示。
旧 Session 没有这些 Event 时无需迁移，投影保持为空。

`changes/get.state` 区分 `pending`、`applied`、`discarded` 和 `none`。Applied 状态返回 Accept 当时保存的文件统计，
并把 validation 标记为历史结果。

Pending 状态的 `changes/file` 使用 PathGuard 读取 Agent worktree。Applied 状态读取 Session 中保存的只读快照，
不会读取 source 当前内容。二进制或超过预览上限的文件不提供 Diff。Extension 不应根据本地 Git 自行构造 Diff。

## Notifications

Runtime 可以主动发送无 `id` 的 JSON-RPC notification：

| 方法 | 主要字段 | 用途 |
| --- | --- | --- |
| `session/event` | `sessionId`、`seq`、`type` | 告知出现新 Event；Extension 随后刷新权威 Read Model |
| `approval/requested` | Session/job/approval identity、kind、summary | 立即显示当前审批卡片 |
| `session/executionStateChanged` | Session/job、`executionState`、可选 result/error | 更新 running/awaiting_approval/stopping/idle |

Notification 是刷新提示，不是完整状态副本。Extension 收到后重新调用 `session/get`/`changes/get`，以 Runtime
Read Model 为准。

## 错误

| code | 含义 |
| ---: | --- |
| `-32700` | JSON parse error |
| `-32600` | 非法 JSON-RPC request |
| `-32601` | method 不存在 |
| `-32602` | 参数缺失、类型或取值非法 |
| `-32004` | Product Service 拒绝，例如冲突、状态或 identity 不允许 |
| `-32603` | 未向客户端暴露内部异常细节的 internal error |

同一 Session 只能有一个 active execution。`session/run` 被拒绝时，Extension 会恢复未发送成功的输入草稿。
Accept/Discard、凭据变更和新建 Session 在 active execution 时被产品层阻止。

## 生命周期与凭据

Extension 的 `RuntimeConnection._configuration()` 将 `codeagent.rpcCommand` 转成 executable：`auto` 先检查
扩展目录父目录的 `.venv/bin/python` 是否存在，存在时附加 `-m codeagent.product.rpc` 并以该父目录为 cwd；
否则使用 `codeagent-rpc`，由 Node spawn 在 Extension Host 的 PATH 中查找。显式配置原样作为 executable，
不能含 shell 参数，不展开 `~`；cwd 为第一个 workspace folder。它不依赖 F5，也不按开发/安装模式分支判断。
`codeagent.sessionRoot` 非空时追加 `--session-root` 参数。

DeepSeek/GLM Key 从 VS Code SecretStorage 注入 RPC 子进程环境。正常凭据传递不使用 RPC 参数；连接配置签名只记录 Key 是否存在，不记录值。
日志没有通用脱敏，因此不能保证任意错误文本或工具结果不会包含 secret。
更改或清除凭据会重启 Runtime，因此 active execution 期间禁止操作。

spawn 使用三个 pipe、继承进程环境并注入配置的 Key，不使用 shell。子进程 error/exit 会拒绝所有 pending 请求，
将连接标记 unavailable。Retry 清理 client 后重新 spawn/initialize；后续 ensure 在进程退出后也可尝试启动。
没有后台定时重连或在新进程自动重放未完成请求。服务端对合法的无 id 入站 notification 直接忽略，
不提供 batch JSON-RPC；错误码详见上表。

协议实现见 [`codeagent/product/rpc.py`](../codeagent/product/rpc.py)，产品状态来源见
[`codeagent/product/service.py`](../codeagent/product/service.py) 与
[`codeagent/product/read_model.py`](../codeagent/product/read_model.py)。
