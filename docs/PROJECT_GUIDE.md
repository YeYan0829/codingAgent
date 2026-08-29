# 当前设计上下文

本文是非归档文档的导航入口，只回答“现在已经有什么、下一步做什么、各文档管什么”。长期目标见 [系统目标](系统目标.md)，当前代码细节见 [ARCHITECTURE](ARCHITECTURE.md)。

## 当前状态

当前只支持 Linux/WSL2，不承诺 Windows 兼容。Session 绑定一个 source repository，初始只读；首次编辑或执行时经批准创建 Session 专属 Git worktree。之后的读取、编辑、命令、验证与当前修改都针对这个 active worktree。

交互 CLI 会逐项显示 Agent 已完成的读取、搜索、命令和编辑。首次需要 worktree 时，确认框会展示触发操作；一次拒绝对当前用户消息生效，Runtime 不会因同轮的多个工具调用重复弹窗。完整文件内容默认不回显，以免淹没终端，但仍会提供给模型并记录必要的事件摘要。

Agent 已拥有：

- 目录、UTF-8 文件、ripgrep 搜索、文件查找和固定 argv 的 Git 只读工具；
- 单一 `apply_workspace_edit`，以多文件事务完成 create/replace/delete/move；
- 单一 `run_command`，在 per-command Bubblewrap 中运行真实 shell string；
- 当前修改的查看、验证证据、采纳、丢弃和 resume。

命令默认只能写 Candidate 和 Runtime 临时缓存，host root 只读、source 与 Git metadata 重新覆盖为只读、`/tmp` 私有、网络关闭。Agent 可以显式申请额外文件写入或 WSL host network；Runtime 不推断权限、不自动重放失败命令。Bubblewrap 缺失或 probe 失败时命令 fail closed。

Atomic Edit 和 sandboxed command 产生的源码、lockfile、生成文件、formatter 修改等都是合法当前修改。`workspace_tainted` 只表示命令可能修改了 Candidate、但 after audit 无法形成可信边界；`recovery_required` 表示进程树、保护边界或恢复结果无法确认，并始终具有最高 resume 优先级。

Command Profile、Agent-facing `run_validation`、pytest-only executor 和裸执行 fallback 已从执行层删除。验证证据只证明 Agent/用户选择的一条 `purpose=validation` 命令针对当前 Candidate 和 Policy 成功，不保证命令质量。

## 持久化边界

正常状态只有三个权威来源：

- `session.json`：当前 Session/workspace 状态、Candidate revision 和 Session grants；
- `events.jsonl`：对话、审批、命令/验证/修改的有界历史摘要；
- active Git worktree：accepted baseline 与当前文件内容。

命令的 before/after hashes、fingerprints 和 mount inputs 在内存中计算。成功后删除 stdout/stderr、runtime HOME 和 mount 临时材料；失败、超时、tainted/recovery 才保留有界 diagnostics。冻结 patch 只存在于一次 accept 审查窗口。

## 下一步

本阶段进入人工验收。之后优先设计 SWE-bench 薄 adapter：注入 repository/issue/执行环境，复用正式 Runtime，导出最终 patch 给官方 harness；不复制 Agent 工具链，也不把 benchmark 容器替代普通 CLI 的 Bubblewrap 边界。

当前不做 Task 数据结构、多 worktree 并行、多 Agent、Docker/远程 executor、domain proxy、cgroup 或复杂 seccomp。

## 文档职责

| 文档 | 职责 |
| --- | --- |
| [系统目标](系统目标.md) | 长期产品目标、原则和非目标 |
| 本文 | 当前状态、下一步和导航 |
| [ARCHITECTURE](ARCHITECTURE.md) | 已实现模块、调用链、安全与持久化边界 |
| [DECISIONS](DECISIONS.md) | 跨阶段仍有效的产品决策；新决策可明确取代旧决策 |
| [Controlled Arbitrary Command + Sandbox TD](CONTROLLED_ARBITRARY_COMMAND_SANDBOX_TECHNICAL_DESIGN.md) | 已实现的命令、权限、Bubblewrap、audit 和 validation 契约 |
| [Session/Workspace/Persistence TD](SESSION_WORKSPACE_PERSISTENCE_TECHNICAL_DESIGN.md) | Session、按需 worktree、accepted baseline 和采纳语义 |
| [Atomic Edit TD](ATOMIC_EDIT_TECHNICAL_DESIGN.md) | 原子编辑事务与恢复契约 |
| [Search/Read TD](SEARCH_READ_TECHNICAL_DESIGN.md) | 搜索和只读工具契约 |
| [Command Profiles / Validation TD](COMMAND_PROFILES_VALIDATION_TECHNICAL_DESIGN.md) | 已被新执行层取代的历史设计，不是当前实现依据 |
| [TESTING](TESTING.md) / [MANUAL_TEST](MANUAL_TEST.md) | 自动化证据与正式 CLI dogfood |
| [NEXT_PHASE_PLAN](NEXT_PHASE_PLAN.md) | 已完成阶段与后续实施顺序 |

冲突时，“当前事实”以代码和测试为准；未来约束以最新 Accepted Decision/Technical Design 为准；归档调研只作历史证据。
