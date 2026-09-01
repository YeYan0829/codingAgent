# 当前 Runtime 架构

本文只描述已经落地的代码事实。目标与取舍见 [Controlled Arbitrary Command + Sandbox TD](CONTROLLED_ARBITRARY_COMMAND_SANDBOX_TECHNICAL_DESIGN.md) 和 [DECISIONS](DECISIONS.md)。

## 1. 用户流程

```text
Session(source-only)
→ Search/Read
→ 首次 edit/run_command 时审批创建 Session worktree
→ Atomic Edit 或 Sandboxed Command
→ validation evidence
→ show changes
→ accept 或 discard
→ 保留同一 worktree 继续 Session
```

项目没有独立 Task 实体。worktree 是 Session 的 active workspace；HEAD 是最近 accepted baseline，working tree 是当前尚未处理的修改。

Session 不预先区分只读模式和写模式。初始 `source_only` 阶段允许探索；首次受保护编辑或命令经用户批准后动态创建 Candidate worktree。切换后的 Runtime Snapshot 每次都明确提供 `active_workspace` 与 `baseline_workspace`：所有工具默认以 active workspace 为根，baseline 只表示不含当前 Candidate 修改的原始基线。`workspace_activated` Event 同时记录两者路径，保留这次状态迁移的历史事实。

## 2. 模块边界

| 模块 | 职责 |
| --- | --- |
| `tools` / `ToolRegistry` | 闭合 Agent schema、工具查找与正式调用入口 |
| `safety` / repository search | PathGuard、SensitivePath、ignore、symlink 与搜索边界 |
| `atomic_edit` | 多文件 prepare、mutation、rollback、transaction recovery |
| `permissions` / `sandbox_policy` | 资源规范化、identity、ALLOW/ASK/DENY、ONCE/SESSION grants、Effective Policy |
| `bubblewrap` | trusted executable probe、deterministic MountPlan、argv 翻译 |
| `sandbox_executor` | 固定 bash、host `shell=False`、输出/timeout/进程树监督 |
| `command_service` | request freeze、审批、before/after audit、Candidate revision、结果与证据 |
| `candidate` / `current_changes` | 按需 patch、三方合并、accept/discard、accepted checkpoint |
| `session` | 当前状态与紧凑历史事件 |
| `context` | Event 投影、Runtime Snapshot、工具 residue 与 token budget |

不存在无沙盒 Agent command fallback。`LocalCommandExecutor`、Command Profile、pytest-only command compiler 和 Agent-facing `run_validation` 已删除。

## 3. Search / Read / Atomic Edit

只读工具支持目录树、文件枚举、严格 UTF-8 分段读取、literal/regex 搜索、文件查找及固定 argv 的 `git_status`、`git_diff`、`git_diff_stat`。`include_hidden=true` 仍遵守 ignore、SensitivePath、PathGuard 和 symlink 规则。

`read_file` 明确区分请求范围、实际返回范围和文件总行数，并返回 `has_more_before/after` 与完整文件 SHA。即使只请求文件中段，也会扫描到 EOF 确认总行数，避免模型把自己的 `end_line` 误认为文件末尾；历史 residue 保留这些边界事实而不重复保存源码正文。

`search_text` 默认按 literal 普通文本解释；正则必须显式传 `mode=regex`。schema 与结果 metadata 都说明实际模式，避免 `|` 等字符被 Agent 误当成已启用的正则语法。

`apply_workspace_edit` 只有 create/replace/delete/move。replace/delete/move 要求来自 `read_file.metadata.sha256` 的完整 bytes SHA-256。事务先完成冲突、安全和目录副作用 prepare，再 mutation；失败回滚所有文件和自动创建目录。无法确认恢复时进入 `recovery_required`。

## 4. `run_command` 调用链

```text
closed tool schema
→ CommandService.prepare（cwd/limits/permissions/before tree/request identity）
→ SandboxPolicy（base + Session grants + one-shot grants）
→ 必要时 Permission Approval
→ spawn 前 canonical resource identity 复检
→ BubblewrapBackend probe + MountPlan
→ SandboxedCommandExecutor
→ after workspace audit
→ candidate_revision / command summary / optional validation evidence
```

`command` 是单个 frozen shell string。host 使用 `subprocess.Popen(argv, shell=False)`；字符串只作为一个参数传给 sandbox 内固定 `/bin/bash --noprofile --norc -c`。因此 pipeline、redirect、glob、inline env、subshell 和命令链由 sandbox 内 bash 解释。

Runtime 不分析 shell AST 或工具名，不从 stderr 推断权限，不自动重放命令。漏报权限时命令在现有 Policy 中普通失败，并返回 diagnostics 与 Effective Policy 摘要。

`run_command.cwd` 只能是相对 active workspace 的路径，省略时为 Candidate 根目录。结果 metadata 明确返回 active/baseline workspace、实际初始 cwd 与 workspace revision。如果命令文本显式引用 baseline workspace，Runtime 不擅自改写命令，但返回非阻断警告，提醒该目录看不到 Candidate 修改。进程正常结束但 exit code 非零时使用 `command_exit_nonzero`，与进程状态 `completed` 分开表达。

## 5. Filesystem 与 Network Policy

MountPlan 的有效顺序是：host `/` 只读、private `/tmp`、允许的 writable roots、source/Git metadata 重新只读、确定敏感资源最后遮罩。Candidate 与 Runtime command cache 默认可写；额外 host write 需要显式申请。普通 host read 默认允许，但明确的 `~/.ssh`、`~/.aws`、`~/.gnupg`、Session metadata 与高权限 sockets 在存在时 hard deny，Approval 不能越过。

文件申请保存 requested path 和 canonical resolved target。文件 grant 精确覆盖该文件；目录 grant 递归；WRITE implies READ；不存在 READ 拒绝；不存在 WRITE 扩为最近既有父目录并展示后审批。spawn 前重新解析并复检 filesystem object identity。

网络默认为独立 namespace（OFF）；显式 `NETWORK` grant 后共享当前 WSL host network（HOST）。当前没有 domain、port、DNS 或 proxy policy。

Bubblewrap 每条命令创建一次 namespace。probe 检查 trusted absolute executable、必要 flags，并实际运行 namespace smoke command；任何失败返回 `sandbox_unavailable`，不降级。

## 6. Workspace audit 与状态

payload 真正启动的每条命令都有 before/after Git snapshot。命令 exit 0、非零或 timeout，只要进程树已停止且 after audit 完整，其 create/update/delete/rename 都是合法 Candidate changes；只有 audit 检测到 workspace tree 变化时才推进一次 `candidate_revision`。

- `changes_active`：边界可信，可继续 edit/command/validation/accept/discard；
- `workspace_tainted`：命令可能写入但 after boundary 无法建立；只允许 read 和 discard；
- `recovery_required`：进程树、保护 identity、rollback/discard 无法确认；最高优先级 fail closed。

非零退出和 timeout 本身不会 taint。discard 固定执行 reset/clean，并以 HEAD、status 和完整 tree 证明恢复 baseline；证明失败升级为 recovery。

## 7. Candidate revision 与验证

Atomic Edit 成功事务和产生 workspace tree 变化的命令推进统一 `candidate_revision`。validation evidence 绑定 exact command/hash、结束后的 revision、subject tree、workspace/base identity 和 Policy revision。当前有效性以 subject tree、workspace/base identity 为准；没有改变 tree 的后续命令不会使证据失效。

Accept 只要求存在 current evidence。`purpose=validation` 加 exit 0 的含义仅是“所选验证命令成功执行”，不是 Runtime 证明命令充分或一定是 pytest。

Accept 从 accepted baseline、Agent tree 和当前 source tree 做 Git 三方合并，冻结“当前 source → merged result”的 exact patch，用户批准后应用同一 bytes。冲突或 source identity 变化不写 source。成功后只在 detached worktree 创建 checkpoint，不提交用户 branch。

## 8. 持久化与诊断

长期权威数据：

- `session.json`：workspace state、revision、accepted baseline identity、Session grants；
- `events.jsonl`：消息、审批和有界操作摘要；
- Git worktree：当前内容。

正常命令事件保存 execution id、有限 command、cwd/purpose、时间/退出、before/after revision/tree、changed path 分类、Effective Policy 和最终 state。完整 fingerprints、before/after maps、MountPlan 和 environment values只在内存中。

成功命令删除 stdout/stderr、runtime HOME/TMP 和 mount masks。失败、timeout、sandbox setup、tainted/recovery 保留有界 diagnostics。没有独立 commands/grants/audit 日志家族。

## 9. Context Management

新事件显式记录 `seq/turn_id/model_step_id`，ToolCall 使用 provider `call_id`；旧 Session 由 JSONL 行序、UserMessage 边界和 call id 兼容投影，不改写历史文件。每次模型调用前，Context Manager 从 Event、当前 Runtime/Git/Validation 和 active workspace 重新构建临时视图。Runtime Snapshot 持久呈现 workspace kind、active/baseline 绝对路径以及默认 cwd 的解析目标；worktree 动态激活后，它明确要求后续读取、编辑和验证只针对 active workspace。该状态属于 mandatory 当前事实，不依赖历史 Tool Observation，也不会因历史降级而消失。

Context v2 Phase A 已删除 Active Code 与独立 code budget。源码正文只通过真实 Tool Observation 进入输入；Runtime Snapshot 不根据历史 read/search 再次读取或注入文件。Event 确定性投影为 `UserTurn → ModelStep → ToolExchange[]`，同一 assistant response 的 batch ToolCall 保持为一个 ModelStep；open、orphan 或 duplicate protocol fail closed。

单个 raw Observation 在 View 中最多 16 KiB并保留截断标志和 metadata。active UserTurn 默认尽量保留最近 4 个 closed ModelStep 的 bounded raw result，更旧步骤保留完整 assistant/tool 配对并把 payload 换成 typed residue；hard budget 下近期目标可继续退化。Context View、Snapshot、Residue 和 BudgetReport 都是内存派生对象，没有新增持久 artifact。

默认 32k context，预留 4k generation、4k continuation/tool 和 2k safety margin；无 tokenizer 时按 UTF-8 保守估算。超预算先缩减 recent raw，再从最老开始整轮淘汰 completed UserTurn；最低集合仍超限则 Runner 记录 `context_budget_exceeded` 并明确终止，保留 Session/workspace。Phase B 分类预算和 Phase C Semantic Condensation 尚未实现，契约见 [Context Management v2 TD](CONTEXT_MANAGEMENT_V2_TECHNICAL_DESIGN.md)。

一次 UserTurn 可以跨多个内部“执行切片”。默认每个切片最多 12 次模型调用，同一 UserTurn 总计最多 48 次；切片耗尽只产生控制事件，不伪造 Final Assistant Message，也不新增 UserMessage。交互 CLI 由用户选择是否继续，`/continue` 可在 Resume 后继续原请求；非交互 `ask` 在总预算内自动续切片。长轮次仍保留完整 tool-call/tool-result 配对，但只保留最近 4 个执行步骤的工具调用前说明，避免说明文字重复膨胀上下文。

System prompt 明确告知模型 Tool Observation 只在近期 Context 暂时保留；证据已经支持可验证修改时应立即行动，不得以获得不影响实现选择的额外确定性为由重复调查。这是行动收敛提示，不是 Runtime 对任务充分性的权威判断。

模型网关支持 Fake、DeepSeek 和 GLM。DeepSeek 与 GLM 共用 OpenAI Chat Completions 兼容 transport；GLM 默认 `glm-5.2` 和智谱标准 API endpoint，可通过 `GLM_BASE_URL` 切换到 Coding Plan endpoint。DeepSeek/Fake 暂用 32k context limit，GLM 使用保守的 128k Runtime 实验上限（118k usable input），避免错误复用 DeepSeek 的 22k 输入预算。两者当前均关闭 thinking，避免尚未持久化的 reasoning content 破坏多轮工具协议。

## 10. 当前限制

- Linux/WSL2 only；需要系统 `bwrap`；nested/container 环境由实际 probe 决定；
- 没有 cgroup CPU/内存/进程配额和复杂 seccomp；输出、timeout 与 `/tmp` 大小已有边界；
- host read 采用 broad read-only，hard deny 是确定路径清单，不是通用 secret classifier；
- bind mask 无法保证遮罩 sandbox 创建后才出现的未来路径；
- Candidate patch 仍限制为有界 UTF-8 文本 patch；
- 不评价模型工具选择能力或 validation command 质量。
- 当前执行切片使用固定配置值，不支持针对单个 Session 动态追加总预算；达到 48 次模型调用后明确终止当前 UserTurn。
- active UserTurn 的旧闭合工具协议会持续占用输入，同时旧正文已 residue 化；真实 Pro Session 在 35–40 个 ModelStep 触发 22k 输入预算上限；
- Phase A 已移除 Active Code；是否足以改善长程源码理解仍待 HTTPX 真实复测；
