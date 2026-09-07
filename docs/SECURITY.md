# 安全模型与边界

CodeAgent 会执行模型生成的命令，因此不能把它当作普通的代码补全工具。
本页说明默认权限、用户审批和当前无法提供的保护。

这些机制用于降低误操作风险，不承诺能够抵御任意恶意代码或操作系统漏洞。

## 保护对象

项目主要保护用户正在使用的源仓库。Agent 的普通修改不应在审查前写入这个目录。

项目也会限制命令的写入范围和网络访问。需要扩大权限时，必须向用户说明原因并请求批准。

CodeAgent 信任宿主系统、Git 和 Bubblewrap。Python Runtime 与 VS Code Extension 也属于信任范围。
恶意管理员、被替换的 Runtime 和内核漏洞不在保护范围内。

## 源仓库为什么不会直接被修改

会话开始时，Agent 只能读取源仓库。第一次需要修改或执行受保护命令时，界面会请求创建 Git worktree。

这个 worktree 是 Agent 的独立工作目录。后续读取、编辑和测试都在其中进行。

因此，Agent 的普通文件变化不会立即覆盖用户目录。用户可以先查看 Diff，再决定是否 Accept。

创建 worktree 前，源仓库必须有 Git commit，并且工作目录必须干净。
Git ignored 文件不会自动复制到新的 worktree。

Git worktree 只隔离文件修改，不限制进程读取宿主文件。命令还需要 Bubblewrap sandbox。

## Bubblewrap 如何限制命令

每次运行命令时，Runtime 都会创建一个新的 Bubblewrap 进程。上一条命令的 `cd`、环境变量和 shell 函数不会保留。

默认情况下，Agent worktree 和 Runtime 缓存目录可以写入。源仓库和相关 Git metadata 会被重新挂载为只读。

宿主根目录默认只读可见。额外的宿主写入位置需要用户批准。

网络默认关闭。用户批准网络权限后，命令使用宿主网络；当前不支持按域名或端口限制。

每条命令使用独立的 HOME 和 `/tmp`。private `/tmp` 上限为 256 MiB。

Runtime 会检查 `bwrap` 的路径、所需参数和 namespace 能力。
任何检查失败都会拒绝命令，不会回退到宿主 shell。

## 默认权限

| 操作 | 默认行为 |
| --- | --- |
| 搜索、读取文件、只读 Git | 允许 |
| 创建 Agent worktree | 询问用户 |
| 在 Agent worktree 内编辑 | 创建 worktree 后允许 |
| 在 Agent worktree 内运行命令 | 允许，但经过 sandbox |
| 写入源仓库或 Git metadata | 拒绝 |
| 写入 worktree 之外的位置 | 询问用户 |
| 网络访问 | 默认关闭，询问用户 |

一次性批准只适用于当前命令。会话级批准可以在同一个会话中继续使用。

文件权限会绑定到实际路径和文件对象。批准后、命令执行前，Runtime 会再次检查目标是否仍是同一个对象。

增加模型步骤预算不会增加任何权限。

## 敏感路径与 secret

如果以下路径在命令启动时存在，Runtime 会把它们遮罩：

- `~/.ssh`
- `~/.aws`
- `~/.gnupg`
- 当前会话数据目录
- D-Bus、Docker 和 containerd socket
- `/run/user/<uid>`

文件编辑工具只接受 Agent worktree 内的相对路径。它还会拒绝符号链接和已知敏感路径。

Extension 把 API Key 保存在 VS Code SecretStorage。正常连接流程不会把 Key 放进 RPC 参数或会话配置。

当前没有通用 secret 扫描。源码、命令输出或模型回复如果包含 secret，仍可能出现在日志、Diff 或界面中。

路径遮罩只覆盖 sandbox 创建时已经存在的目标。它不能识别所有未来创建的敏感文件。

## Accept 前的保护

测试通过只证明当时那份代码通过了指定命令。代码变化后，旧测试结果不再代表当前修改。

CodeAgent 会记录测试时的代码状态。Accept 只接受与当前修改对应的成功测试结果。

Accept 还会检查 Agent worktree 和源仓库是否仍符合预期。然后它使用 Git 三方合并应用修改。

源仓库中不冲突的并发修改可以保留。发生冲突时，Accept 会停止并保留双方文件。

Accept 不会自动提交用户分支。Discard 只清理 Agent worktree，不修改源仓库。

测试命令成功不等于测试充分。合入用户的并发修改后，也不会自动重新运行测试。

应用前后会检查源仓库内容，但没有跨进程文件锁。其他程序仍可能在检查和写入之间修改文件。

## Stop 与命令超时

Stop 会阻止 Runtime 启动下一次模型调用或工具操作。它在当前操作结束后生效。

Stop 不会立即终止正在运行的命令。命令会继续运行，直到正常结束或达到 timeout。

命令 timeout 默认为 120 秒，允许设置为 1 到 1800 秒。超时后，Runtime 会尝试终止进程组。

stdout 和 stderr 分别最多保存 2 MiB。返回给模型的内容还会进一步缩短。

当前没有 CPU、内存、进程数或宿主磁盘配额。可写目录仍可能占用大量磁盘空间。

## 崩溃与恢复

Runtime 会保存会话信息、工具结果和 Git worktree 状态。重启后，它可以用这些信息恢复界面。

正在运行的模型请求、shell 进程和未处理审批无法从中点恢复。重启不会把未处理审批变成授权。

如果 Runtime 无法确认文件事务或命令进程已经安全结束，会话会进入恢复状态。
这时 Accept 保持关闭，用户需要保留现场并人工检查。

## SWE-bench 的安全边界

SWE-bench 命令运行在一次性 Docker container 中。容器默认断网，并把 Agent worktree 挂载到 `/testbed`。

这个 Docker 后端只用于评测。它不是本地 VS Code 产品的通用执行环境，也不经过人工 Accept 流程。

官方答案和测试信息只提供给评测准备与 grader。评测 harness 不会主动把它们加入 Agent 的模型输入。

## 当前不保证

- 不支持 macOS 或原生 Windows Runtime。
- 不阻止命令读取所有宿主文件；宿主根目录默认只读可见。
- 不提供 CPU、内存、进程数或完整磁盘配额。
- 不提供域名或端口级网络白名单。
- 不提供复杂 seccomp profile 或内核漏洞防护。
- 不判断第三方安装脚本是否可信。
- 不扫描所有源码、命令输出和模型回复中的 secret。
- 不自动判断测试覆盖是否充分。
- 不自动解决 Git 冲突。
- 不支持二进制文件和任意文件模式编辑。

发现边界异常时，应保留会话和诊断信息。不要通过关闭 Policy 或 sandbox 绕过失败。
