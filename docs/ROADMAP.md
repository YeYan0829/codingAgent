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

### v0.3 controlled pytest runner（开发快照）

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

当前包版本已标记 `0.3.0`，但 v0.3 尚未形成 release commit，真实 TTY/DeepSeek 验收记录也仍待完成。

## 下一阶段

### v0.3.1：稳定受控执行底座

在开放写能力前先处理：

- denial 也生成 provider-compatible tool observation；
- 敏感目录父路径检查；
- pytest cache 与 resume/cleanup 语义；
- 修正文件搜索误跳过 `codeagent/` 包；
- timeout 和进程终止异常兜底；
- Runtime 侧统一工具参数 schema validation；
- 完整自动化测试和真实终端人工验收；
- 整理并提交 v0.3 release。

验收目标：readonly 与 pytest-only execution 成为可重复、可恢复、审计完整的稳定基线。

### v0.4：候选工作区内受控写入

新增有限写工具，例如：

- `edit_file`
- `write_file`
- `apply_patch`

边界：

- 只操作 execution task workspace，不写 source；
- 所有路径继续受 `active_root` 限制；
- 记录 Agent edit journal 和每次写入后的 diff；
- 大规模删除、二进制、symlink 和敏感路径默认拒绝；
- 暂不实现自动 apply 到 source；
- 一个 session 仍独占一个 worktree。

验收目标：Agent 能在隔离任务工作区中产生可审查修改。

### v0.5：read-edit-test-retest 候选闭环

形成：

```text
读取 → 基线测试 → 编辑 → 重新测试 → 冻结 candidate → 展示 diff
```

Candidate 需要绑定：

- 初始 source/base identity；
- 固定 patch hash；
- changed files；
- 测试命令和结果；
- 未解释的 workspace side effects。

用户可以查看 diff、继续调整或放弃。正式 source 仍不自动修改。

验收目标：交付一份经过测试且证据完整的候选修改。

### v0.6：用户批准后安全应用到 source

新增独立 apply 流程：

```text
冻结 candidate
→ apply preflight
→ 检查 source identity/dirty 状态
→ 展示固定 diff 和测试证据
→ 用户批准
→ 再次检查 source
→ 应用
→ 记录 receipt
```

第一版只允许应用到未变化的预期 source。Source 在任务期间发生变化时中止，不自动 merge/rebase。正式 apply 不能复用普通写工具权限。

验收目标：用户明确批准的固定 candidate 可以安全进入正式项目，不覆盖并发变化。

### v0.7：dirty source snapshot 与任务迁移

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
| L1 技术原型 | 读取、模型工具调用、受控测试、日志 | v0.3 接近完成 |
| L2 任务闭环 | 候选编辑、测试、diff、接受或放弃 | v0.4–v0.6 |
| L3 可靠本地工具 | dirty source、恢复、变化检测、常见项目稳定工作 | v0.7 以后 |
| L4 成熟安全产品 | host sandbox、网络/资源限制、并发协作、多语言工具链 | 长期方向 |
