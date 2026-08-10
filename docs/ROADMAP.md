# Roadmap

## 产品目标

近期目标不是一次性实现通用自治 Agent，而是形成一个可信、可解释的本地 Coding Agent 闭环：

> 用户描述开发任务；CodeAgent 在独立任务工作区中理解、修改并测试代码，最终交付带验证结果的候选修改，由用户决定是否进入正式项目。

近期信任模型：用户信任本地项目代码；CodeAgent 保护 source 的代码状态，但在实现 host sandbox 前不承诺安全执行恶意仓库。

## 已完成基线

### v0.1 readonly exploration

完成本地 CLI、FakeLLM、文件与 Git 只读工具、session、基础 Policy/Approval、路径边界和测试套件。

### v0.2 DeepSeek readonly tool calling

接入 DeepSeek tool calling，建立 provider adapter 边界，完善用户级 session storage、全局列表、title、resume UX 和旧 session fallback。

### v0.3 controlled pytest runner（已完成基线）

主体实现包括：

- `WorkspaceContext` 和 clean Git detached worktree；
- readonly/execution CLI mode；
- frozen `CommandSpec`；
- pytest-only `CommandPolicy`；
- approve-once/deny；
- `LocalCommandExecutor` 和 `FakeCommandExecutor`；
- timeout、minimal environment 和完整 command artifacts；
- resume workspace 状态检查和保守 cleanup；
- 命令前后 Git side-effect audit。

当前包版本为 `0.3.0`。自动化回归和 Windows 真实 TTY/DeepSeek 核心端到端验收已经通过；当前基线位于 `wip/v0.3`，尚未合并到 `master` 或创建公开 release/tag。

## 下一阶段

### v0.4：最小完整候选修改闭环

不再把写入、测试、diff 和应用拆成多个长期版本。v0.4 以一条可实际使用的纵向链路为目标：

```text
读取 → 基线测试 → 编辑 → 重新测试 → 冻结 candidate → 展示 diff
→ 用户批准 → 检查 source identity → 应用固定 candidate
```

第一版主动收窄能力：

- 只提供一种受控文本 patch 能力，不同时扩展多个重叠写工具；
- Agent 只能修改 execution task workspace，不能通过普通工具写 source；
- 路径继续受 `active_root` 限制，拒绝越界、symlink、二进制和敏感路径；
- 每次编辑记录 journal 和 diff，pytest 继续走既有 Policy/Approval/Artifact 链；
- candidate 绑定初始 base、固定 patch hash、changed files 和测试证据；
- 正式 apply 是独立用户操作，不复用 Agent 写权限；
- apply 前后检查 source 仍 clean 且 HEAD/base 未变化，变化时立即中止，不自动 merge/rebase；
- 一个 session 仍独占一个 worktree。

Candidate 至少绑定：

- 初始 source/base identity；
- 固定 patch hash；
- changed files；
- 测试命令和结果；
- 未解释的 workspace side effects。

用户可以查看 diff、继续调整、放弃，或把经过批准的固定 candidate 应用到未变化的 source。

验收目标：在一个小型真实任务中，Agent 能完成读取、编辑、pytest、自我修正、展示候选 diff，并由用户明确批准后安全写入 source；拒绝或 source 已变化时不写 source。

### 不阻塞 v0.4 的可靠性待办

以下问题继续按风险处理，不再默认成为写入闭环的前置项目：

- 敏感目录父路径检查；
- pytest cache 与 resume/cleanup 体验；
- 文件搜索跳过目录规则；
- timeout 后进程树终止兜底；
- Runtime 侧统一工具参数 schema validation；
- artifact/runtime retention。

其中任何问题一旦涉及 source 数据破坏、路径越界、Policy/Approval 绕过、密钥泄露或真实 provider 阻断，立即升级为阻塞项；其余随 v0.4 纵向用例触发修复。

### v0.5：dirty source snapshot 与任务迁移

支持真实开发中的 `HEAD + 未提交修改`：

- staged/unstaged/untracked snapshot manifest；
- 创建时的 source snapshot identity；
- task workspace 精确复现用户现场；
- 区分用户初始修改、Agent edits 和测试副作用；
- source 变化后创建派生任务或新 workspace revision；
- 在最新代码上重新应用候选、重新测试和重新批准；
- 冲突发生在独立集成工作区，不直接写 source。

验收目标：开发者和 Agent 可以基于明确快照并行工作，同时不丢失修改归属。

## 后续能力

### Host sandbox 与 controlled network

将 sandbox 设计为 executor backend，而不是与 worktree 混为一谈：

```text
CommandExecutor
├── LocalCommandExecutor
├── ContainerCommandExecutor
└── platform-specific sandbox executor
```

评估文件系统挂载、默认关闭网络、CPU/内存/磁盘/进程数限制和进程树终止。Windows、Linux 和 macOS 可能需要不同后端。依赖安装必须是独立受控流程。

### 小范围命令扩展

在 pytest、写入和审计协议稳定后，再按独立 schema 和 Policy 逐项评估：

- `ruff`
- `mypy`
- `npm test`
- `pnpm test`

不使用任意 shell 作为统一入口。

### 多 session 与 workspace revision

先保证“一 session 独占一 worktree”。真实使用证明需要后，再引入：

- session lease/lock；
- source apply lock；
- logical session 下的 workspace revisions；
- parent/derived session；
- 多 candidate 比较；
- 自动迁移与冲突解决。

这些属于内部一致性机制，不应要求普通用户学习 worktree 和锁。

### Benchmark、适配器与长上下文

在 coding loop 稳定后再推进：

- benchmark-ready task harness；
- SWE-bench / Terminal-bench adapter；
- 项目索引；
- context compression；
- 长期记忆；
- 成本与耗时统计。

## 成熟度判断

| 层级 | 标准 | 当前状态 |
| --- | --- | --- |
| L1 技术原型 | 读取、模型工具调用、受控测试、日志 | v0.3 已完成基线 |
| L2 任务闭环 | 候选编辑、测试、diff、接受或放弃 | v0.4 |
| L3 可靠本地工具 | dirty source、恢复、变化检测、常见项目稳定工作 | v0.5 以后 |
| L4 成熟安全产品 | host sandbox、网络/资源限制、并发协作、多语言工具链 | 长期方向 |
