# Product RPC（v0.5 / Phase 5）

Product RPC 是 VS Code Extension 与 Python Runtime 之间的产品边界。它不替代 Session、Event、Git
worktree、Policy、Approval 或 ToolRegistry，也不允许 Extension 直接访问内部持久文件。当前协议覆盖只读
投影、新建、Run/自动切片与本轮扩额、live notification、Stop/Approval 和 Changes 交付。

## 从源码安装

在项目根目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

如果 `.venv` 已存在，建议仍重新执行第二条命令。原因是新增的 `codeagent-rpc` console script 只有在重新
安装项目后才会生成。确认安装结果：

```bash
.venv/bin/codeagent-rpc --help
```

如果使用系统或其他虚拟环境，也可以运行对应环境中的 `python3 -m pip install -e ".[dev]"`，然后用
`command -v codeagent-rpc` 确认入口在 PATH 中。

## 启动 RPC

开发安装后运行：

```bash
codeagent-rpc
codeagent-rpc --session-root ~/.codeagent/sessions
```

直接运行后进程会等待 stdin 中的 JSON-RPC 请求，看起来“没有输出”是正常现象；通常不需要人工单独启动，
VS Code Extension 会创建并监管这个子进程。

协议使用 JSON-RPC 2.0，每行一个完整 JSON 消息。stdout 只输出协议响应，诊断日志必须写 stderr。

请求示例：

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"1.0"}}
{"jsonrpc":"2.0","id":2,"method":"session/list","params":{}}
{"jsonrpc":"2.0","id":3,"method":"session/get","params":{"sessionId":"abc123"}}
{"jsonrpc":"2.0","id":4,"method":"session/events","params":{"sessionId":"abc123","afterSeq":40,"limit":200}}
```

当前支持：

- `initialize`
- `session/list`
- `session/get`
- `session/events`
- `session/create`
- `session/run`
- `session/continue`
- `session/increaseBudget`
- `session/stop`
- `approval/resolve`
- `changes/get`
- `changes/file`
- `changes/accept`
- `changes/discard`

`session/events` 的 `afterSeq` 是排他游标，`limit` 范围为 1～1000。响应提供 `lastSeq` 和 `hasMore`，客户端
应持续分页直到 `hasMore=false`。执行期间 Runtime 发布 `session/event` 和
`session/executionStateChanged` notification；notification 只是刷新提示，Extension 始终以 `session/get` 和
`afterSeq` 补读的持久 Event 为权威事实。`session/run`、`session/continue` 与 `session/increaseBudget` 只登记
后台 job 并立即返回，不阻塞 RPC loop。正常切片由 Product 自动调用内部 Continue；公开 `session/continue`
用于进程中断后仍有剩余预算的恢复场景。

`session/list` 不传 `workspace` 时返回 Session Root 下的全局本地历史，传入时只返回指定 workspace。Extension
使用全局列表实现任务标题、repository、provider/model 和状态的摘要搜索；只有摘要中的规范化 source workspace
与当前 VS Code workspace 相同时才请求 `session/get`。其他 repository 的 Session 只显示摘要，必须先打开对应
workspace 才能查看详情或继续执行。

`session/increaseBudget` 只接受当前处于 budget-waiting 状态的 UserTurn，并要求客户端携带当前 `turnId` 与
`expectedLimit` 防止过期操作。调用者必须且只能提供以下一个字段：

- `additionalSteps`：正整数，在当前本轮总上限上追加指定步数；
- `newLimit`：大于当前上限的正整数，将本轮总上限设为该值。

解析后的总上限不得超过 1000。服务端记录 previous/new limit、实际追加步数与输入模式，随后在同一个
UserTurn 中继续；不追加 `user_message`，也不修改 Session 的默认预算。

## Product Read Model

- `SessionSummary`：标题、workspace、`idle/running` execution fact、attention summary、最后活动时间和 changed file count。
- `SessionDetail`：按 UserTurn 组织的 `turns[]` 时间线、当前 Changes/Validation 摘要、`turnBudget`、
  AvailableActions 和 `lastSeq`。`turnBudget` 提供当前 turn、used、limit、waiting、closed 和 maxLimit；旧的
  分栏字段暂时保留为 RPC 兼容层，不再由当前 UI 渲染。
- Activity 的 raw detail 是持久 Event 的有界子集；默认 UI 只显示用户语言摘要。
- Validation 只说明命令结果以及 evidence 是否仍适用于当前 revision，不判断测试充分性或任务是否完成。

执行监管器保证同一 Session 同时至多一个执行 job。进程重启不恢复 Python 执行栈，持久 Session/Event
仍可重新打开；具体生命周期边界见 [VS Code 产品层 TD](VS_CODE_PRODUCT_TECHNICAL_DESIGN.md)。

`changes/file` 只接受当前 pending changes 列表中的 workspace-relative path，并返回有大小限制的
UTF-8 baseline/current 内容。Extension 将两侧内容注册为只读 virtual document，再调用 VS Code 原生
`vscode.diff`，不直接读取受管 worktree，也不实现自定义 Diff Viewer。`changes/accept` 仍要求当前 validation
evidence，`changes/discard` 不修改 source；两者都要求 execution idle，并复用 `CurrentChangesService` 的
identity、三方合并和 fail-closed 语义。

## 错误与安全

- malformed JSON：`-32700`
- invalid request：`-32600`
- method not found：`-32601`
- invalid params：`-32602`
- Session 等产品服务错误：`-32004`

RPC 不接受任意路径读取或 shell 命令。`sessionId` 必须通过 Runtime 的 SessionStore 定位；workspace filter
只影响列表范围，不改变任何持久状态。

产品层使用进程内 Approval bridge。需要 `ask` 的工具、workspace upgrade 和增量 command permission 会进入
`awaiting_approval`，Extension 只展示冻结的有界摘要，并通过 `approval/resolve` 返回 Allow/Reject。Runtime
重启、Reject 或 Stop 都不会留下授权；旧审批只投影为中断诊断。Stop 不强杀正在进行的模型 HTTP 或命令，
而是在其返回后的下一个安全边界确认，之后不得启动新的 model step/tool call。

## 产品配置与凭据

Chat header 的齿轮图标打开内嵌配置页，平时不占用会话空间。产品 UI 当前只提供 DeepSeek 和 GLM；`fake`
provider 仍保留给自动测试和 Runtime 开发，不作为用户选项。配置页支持 provider、model、temperature、最大输出
token、每个 slice 的 step 数和每个 UserTurn 的总 step 数。Temperature 默认值为 `0.2`。

普通配置保存到 Extension `globalState` 的产品 profile。API Key 使用 VS Code `SecretStorage`，不写入
`settings.json`、RPC 参数、Session metadata、Event 或日志。Extension 只把 Secret 注入其启动的 Runtime 子进程
环境。配置页只显示是否已经配置，留空保存表示保留原 Key，也可以显式 Clear。

点击 New task 时，模型与预算参数写入新 Session 的 `model_options` 和 `runtime_options` 快照。内部 slice
会自动衔接；达到本轮总 step 上限时才等待用户决策。用户可以输入 `+N` 追加 N 步，或输入 N 将本轮总上限
设为 N；输入框没有默认扩额值，取消不会调用 RPC。扩额只影响当前 UserTurn，不修改后续轮次的 Session 默认值。
修改产品 profile 只影响后续创建的 Session。Key 可以轮换，但 Session 永远不保存 Key。

## VS Code 人工测试

### 1. 启动 Extension Development Host

1. 使用 **Remote - WSL** 窗口打开整个 CodeAgent 仓库，然后选择根目录提供的
   **Run CodeAgent Extension** 启动配置。也可以单独打开 `vscode-extension/`，其目录内保留了等价启动配置。
2. 按 `F5`，选择 **Run CodeAgent Extension**。仓库已提供 `.vscode/launch.json`，无需 npm install 或编译。
3. 在新打开的 Extension Development Host 中打开 CodeAgent 仓库目录。
4. `Session Root` 仍属于基础设施设置；模型、API Key 和预算通过 Chat header 的齿轮配置页填写。
5. CodeAgent 默认出现在右侧 Secondary Side Bar，左侧 Explorer 保持可见。打开 CodeAgent 后，主视图默认显示
   最新 Session；点击 header 的 History 图标查看历史，History 内的刷新按钮会重新读取 Session。

修改 `Session Root` 或 `RPC Command` 后，Extension 会自动重启本地 Runtime 并刷新列表。手动点击 Refresh
时，状态栏会显示找到的 Session 数量；显示 `found 0 sessions` 表示连接正常，但当前 workspace 过滤结果为空。

`codeagent.rpcCommand=auto` 会优先使用项目根目录 `.venv/bin/python -m codeagent.product.rpc`，因此已有
虚拟环境即使尚未重新生成 `codeagent-rpc` 入口，也可以直接测试。如果 Extension Development Host 报告
Runtime 启动失败，请先确认 `.venv/bin/python` 存在；也可以把设置改成已安装入口的绝对路径，例如：

```text
/home/<user>/projects/code-agent/.venv/bin/codeagent-rpc
```

### 2. 验收清单

- History 平时隐藏；点击图标后按时间段显示 Session，选择后回到会话。
- 用户请求显示为右侧气泡，Agent 回复显示为普通 Markdown 文本。
- Agent 文本和工具活动按 UserTurn/Event 顺序自然交错；工具活动默认折叠且不重复显示 lifecycle。
- 每轮 changed files 和 validation（存在时）显示在该 UserTurn 末尾。
- 中央 Editor 区域继续保留给代码，不打开 Session Detail Editor。
- 点击 New Task 创建空 Session；输入请求后立即显示 running，并随持久事件自然刷新工具活动和回复。
- 非空请求提交后输入框立即清空且不能重复发送；RPC 拒绝时恢复原文。`Enter` 发送、`Shift+Enter` 换行，
  输入法 composition 阶段不误发送。
- 用户气泡与 Agent Markdown 的复制按钮复制原始纯文本，并通过 VS Code Clipboard API 返回可访问状态。
- 会话 header 显示该 Session 已冻结的 provider/model；配置页的选择不会改变已经存在的 Session。
- 齿轮配置页只显示 DeepSeek/GLM；保存后新建 Session 使用该配置，旧 Session 的 Continue 保持原快照。
- API Key 输入框不会回显既有 Key；清除后 Runtime 重启，Session/Event 文件中不应出现 Key。
- running 时不能重复提交；同一 Session 的第二次 execution 会被 Runtime 拒绝。
- slice exhausted 由同一后台 execution 自动续跑，不显示 Continue，也不新增用户气泡或重置总预算。
- 达到本轮总 step 上限后显示已用/上限和两个选择：明确扩额并继续，或 Stop 结束本轮。扩额输入支持 `+N`
  与新总上限 N，不预填 96 或任何默认值；错误、过期、重复和运行中的扩额请求均被拒绝。
- 请求编辑或受保护操作时进入等待审批；workspace upgrade 与 command permission 使用不同标题，network once
  直接展示 command、reason、scope 和 `Allow once`。Reject/Stop 保持 source 不变，Allow 后仍执行 identity 复检。
- running 时显示 Stop；停止请求与 acknowledgement 都写入 Event，重复 Stop 保持幂等。
- 对已有 pending changes，点击文件应在中央 Editor 打开原生 Diff，并显示 Git 增删行统计。
- 当前 validation stale/缺失时 Accept 禁用；Accept/Discard 均先显示模态确认，完成后重新读取权威状态。
- 修改 Session Root 后重新加载窗口，刷新仍能从持久 Session 恢复视图。

排错时打开 **View → Output → CodeAgent Runtime**。协议 stdout 不会出现在这里；该频道只接收 Runtime
stderr 和 Extension 启动错误。

CodeAgent 使用 VS Code 1.106 正式提供的 `viewsContainers.secondarySidebar` 贡献点，新安装默认位于右侧，中央
Editor 始终用于源码和 VS Code 原生 Diff。VS Code 会优先保留用户移动过的 View 位置；若从旧版扩展升级后
CodeAgent 仍在左侧，执行 **View: Reset View Locations** 恢复右侧默认值，或手动拖到 Secondary Side Bar。
