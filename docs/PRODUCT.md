# VS Code 产品使用流程

本文说明用户在 VS Code 中会看到什么，以及每个操作会产生什么结果。
底层实现见[当前架构](ARCHITECTURE.md)，权限边界见[安全模型](SECURITY.md)。

## 从任务到交付

```text
打开 Git 仓库
→ 配置模型并创建会话
→ 发送任务
→ 查看执行过程并处理审批
→ 查看 Diff 和测试状态
→ Accept 或 Discard
```

一次会话会一直绑定同一个源仓库。你可以在 Agent 完成后继续追问，之前的对话和未交付修改仍会保留。

Agent 的修改先保存在独立 Git worktree。本文把这份尚未交付的代码称为“待审查修改”。

## 1. 打开仓库并配置模型

Extension 使用 VS Code 中第一个 workspace folder。新建会话时，它会把这个目录作为源仓库。

齿轮页可以设置 provider、model、reasoning、temperature、最大输出 token 和步骤预算。
这些设置只影响之后新建的会话。

GLM-5.3 的 reasoning 档位为 `high`、`max`，默认选择 `high`。DeepSeek V4 的档位为
`low`、`high`、`max`，默认选择 `high`。关闭 reasoning 时，Runtime 不会发送 reasoning effort。

设置保存到 VS Code 的本地状态。新建会话后，标题栏会显示这次会话使用的 reasoning 状态。

API Key 保存在 VS Code SecretStorage。输入框不会回显已经保存的值。

## 2. 创建任务

点击 **Start a new task** 会创建一个新会话。发送任务后，用户消息立即出现在时间线中。

同一个会话同时只能运行一个任务。任务结束后，可以继续发送后续请求。

每个请求默认最多使用 48 次模型调用。Runtime 每运行 12 次会设置一个内部检查点，
然后在同一个后台任务中自动继续。

## 3. 查看执行过程

时间线会把连续的工具操作折叠成活动组。你可以看到工具名称、文件路径、命令摘要和执行结果。

工具失败时，活动组会自动展开。命令失败会显示退出状态，以及 stderr 中最关键的一行。

点击 **Show output** 可以查看这次工具调用的详细输出。长输出会保留开头和结尾，
中间部分会截断，避免撑满时间线。

文件变化和测试结果也会显示在时间线中。界面不会展示或声称展示模型的隐藏推理。

## 4. 处理权限请求

第一次修改文件或运行受保护命令时，CodeAgent 会请求创建独立 worktree。
源仓库必须有 Git commit，并且不能有 tracked 或 untracked 修改。

命令如果需要联网，界面会显示 **Allow command permissions?**。
命令如果要写入 worktree 之外的位置，也会显示这个审批卡片。

审批卡片会展示实际命令、申请的权限和使用范围。**Allow once** 只允许当前命令，
会话级批准可以供同一会话中的后续命令使用。

**Reject** 只拒绝当前权限请求，不会把待审查修改应用到源仓库。
要丢弃代码修改，请使用 **Discard**。

等待审批时，History、Refresh 和 Diff 仍可使用。Runtime 重启后，未处理的审批不会自动变成授权。

## 5. Stop 与步骤预算

运行中可以点击 **Stop**。Runtime 会停止启动新的模型调用和工具操作。

Stop 会在当前操作完成后生效。已经运行的命令不会被立即终止，仍会等待完成或超时。

请求达到步骤上限后，界面会显示 `Step budget reached`。原始请求和现有修改都会保留。

你可以增加当前请求的步骤上限，也可以结束这个请求。扩额只影响模型调用次数，
不会增加网络或文件权限。

## 6. 查看 Diff 和测试状态

存在待审查修改时，底部会显示 **Changes ready for review**。这里列出修改文件、增删行和最近一次测试状态。

点击 **Review Diff** 会在中央编辑区打开 VS Code 原生 Diff。Diff 是只读视图，关闭它不会改变代码。

测试通过只证明当时那份代码通过了测试。如果 Agent 后来继续改动，界面会把旧测试结果标记为过期。

重新运行测试后，新结果会对应当前修改。CodeAgent 只能确认该命令成功退出，不能判断测试范围是否充分。

## 7. Accept 与 Discard

**Accept** 会把待审查修改应用到源仓库。执行前，Runtime 会再次确认当前修改仍有有效的测试结果。

如果你同时修改了源仓库，CodeAgent 会尝试用 Git 三方合并保留双方改动。
出现冲突时，Accept 会失败，并保留现场供你检查。

Accept 不会自动创建 Git commit。并发修改合入后，建议在源仓库中再次运行必要测试。

Accept 后，Changes 卡片会变为 **Applied**。文件列表和 **Review Diff** 入口会继续保留，
Accept 与 Discard 按钮则不再显示。

Applied Diff 保存的是这次 Agent 实际应用的修改。用户之后继续编辑 source 时，
这些新改动不会混入该 Session 的历史 Diff。

Applied 卡片显示 **Tests passed at that time**。它只说明应用修改前的测试结果，
不表示当前 source 仍处于已验证状态。

CodeAgent `0.5.0` 不提供 Accept 后的一键撤销。直接反向应用旧修改可能覆盖用户之后的编辑，
因此当前版本只提供回看。

**Discard** 会清空 Agent worktree 中尚未交付的修改。它不会修改源仓库。

Accept 和 Discard 只在任务停止运行后可用。如果 Runtime 无法确认工作区状态，
Accept 会保持关闭，并提示人工检查。

## 8. History

History 默认显示当前仓库的会话。你可以按标题或仓库名称搜索，也可以按状态筛选。

切换 **All workspaces** 后，可以看到同一个 Session Root 中的其他仓库会话。
Session Root 是 Runtime 保存本地会话记录的目录。

其他仓库的会话只能查看摘要。要继续这些会话，需要先在 VS Code 中打开对应仓库。

重载窗口后，Runtime 会从本地记录和 Git worktree 恢复对话、修改与测试状态。
它不会恢复中途运行的模型请求、shell 进程或未处理审批。

## 9. 错误与断开

操作失败时，界面会显示原因。消息发送失败后，未发送的输入会回到编辑框。

找不到 Runtime 或 Runtime 退出时，界面会显示 **Runtime unavailable**。
你可以打开 **CodeAgent Runtime** Output 查看错误。也可以点击 **Retry Runtime**。

Retry 会启动新的 Runtime 进程并读取已经保存的会话。它不会自动继续中断的任务。

当前 RPC 请求没有超时。如果 Runtime 进程仍在但不再响应，界面不一定能自动发现。

## 主要界面状态

| 状态 | 含义 | 可以做什么 |
| --- | --- | --- |
| Ready | 可以发送新请求 | 输入任务、查看 History |
| Running | 模型或工具正在工作 | 查看时间线、Stop |
| Approval required | 等待权限决定 | Allow、Reject、Stop |
| Stopping | 已请求停止 | 等待当前操作结束 |
| Step budget reached | 当前请求达到步骤上限 | 增加预算或结束请求 |
| Changes to review | 有待审查修改 | 查看 Diff、Accept、Discard |
| Applied | 修改已经应用到源仓库 | 回看文件列表和当时的 Diff |
| Discarded | 尚未交付的修改已丢弃 | 继续任务或开始新任务 |
| Runtime unavailable | Runtime 无法连接 | Retry 或打开 Output |
| Recovery required | 工作区状态无法确认 | 保留现场并人工检查 |
