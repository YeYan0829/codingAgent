# Roadmap

> **Historical roadmap：** 本文记录从 v0.1 到原 v0.5 VS Code Showcase 路线的阶段计划，不再表示下一开发里程碑。当前状态见[当前设计上下文](../PROJECT_GUIDE.md)；新的实践方向尚待调研确认。

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

该阶段发布时的包版本为 `0.3.0`。自动化回归和 Windows 真实 TTY/DeepSeek 核心端到端验收已经通过；历史基线位于 `wip/v0.3`。当前包版本已经进入 `0.4.0`，v0.3 只作为回溯基线。

## 当前任务闭环

### v0.4：最小完整候选修改闭环（当前实现）

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

当前功能分支已经实现这一纵向链路和确定性 fixture，包括 edit journal、不可变 Candidate、测试 receipt 绑定、拒绝/并发变化 fail closed、apply receipt 以及 candidate dirty resume。自动化回归和 Windows/DeepSeek 真实终端 1–7 人工验收已通过；合并、release/tag 仍需用户检查后单独执行。

### v0.4.x：发布前维护线

v0.4 已证明闭环成立。后续不再用一个独立体验版本长期打磨 CLI；只有影响公开技术预览可信度的问题才阻塞发布：

- source 数据破坏、路径越界或 Candidate 被替换；
- Policy/Approval 绕过或密钥泄露；
- resume/apply 的核心状态无法恢复；
- 真实 provider 无法完成最小端到端任务；
- 文档、包版本、安装方式或演示 fixture 明显不一致。

pytest cache、文件搜索跳过规则、timeout 进程树兜底、统一工具参数 schema validation 和 artifact retention 继续作为可靠性 backlog，随 v0.5 纵向用例触发修复。它们不默认阻塞客户端原型，但一旦触及上述安全不变量就立即升级。

## 下一产品里程碑

### v0.5：Showcase Alpha（Runtime + VS Code）

目标不是增加更多 Agent 名词，而是让 v0.4 已有闭环变得可见、可用、可演示：

> 用户在 VS Code 中描述一个任务，查看执行进度和必要审批，审查 diff 与测试证据，然后接受或拒绝修改；普通用户不需要理解 readonly/execution、worktree、receipt 或 session root。

#### 推进方式：按可演示纵向切片

v0.5 不先封闭开发完整协议再集中制作 UI。每个切片都必须产生可运行、可截图或可录屏的反馈：

1. FakeLLM：VS Code 发起任务并看到真实 Runtime 消息；
2. FakeLLM：完成一次工具审批和状态更新；
3. FakeLLM：展示 Candidate diff，并完成 Accept/Reject；
4. 持久化：重启后恢复待审查任务；
5. DeepSeek：完成真实端到端任务；
6. 发布准备：安装、CI、演示视频和静态展示页。

每个切片只扩展它实际需要的应用接口和事件，但新增名称与语义一旦进入客户端就必须有协议测试，避免 UI 依赖内部日志格式。

#### 1. 稳定客户端边界

从当前 CLI 组装逻辑中提取面向客户端的应用接口，至少覆盖：

- `start_task` / `send_message`；
- `approve_tool` / `deny_tool`；
- `get_task_state` / `resume_task`；
- `show_candidate`；
- `apply_candidate` / `reject_candidate`。

同时定义少量稳定事件，例如 `task_started`、`assistant_message`、`tool_requested`、`tool_finished`、`test_finished`、`candidate_ready` 和 `task_failed`。客户端不能通过解析终端文本、`events.jsonl` 或 artifact 目录推断业务状态。

第一版优先采用本地子进程与 JSON Lines/JSON-RPC 一类轻量协议，不先引入远程服务、账户系统或通用 Web 后端。Python Runtime 仍是工具、Policy、Approval、Workspace 和 Candidate 的唯一执行权威。

#### 2. 最小 VS Code 纵向闭环

插件第一版只提供：

- 任务输入与对话摘要；
- 当前阶段和折叠后的工具时间线；
- 风险操作的批准或拒绝；
- changed files、focused diff 和 pytest 结论；
- Candidate 接受或拒绝；
- VS Code 或 Runtime 重启后的任务恢复。

先用 FakeLLM 建立确定性离线演示，再接 DeepSeek 进行真实 provider 验收。插件不复制 Python Runtime 的安全逻辑，也不直接修改 source。

#### 3. 收敛用户心智模型

- 默认入口是“开始任务”，不是预选 readonly/execution；
- Runtime 在真正需要测试或编辑时申请 capability upgrade，并显式创建任务工作区；
- 默认只展示任务进度、修改、测试、风险和最终审批；
- Candidate hash、receipt、完整 stdout/stderr 和 edit journal 放入可展开诊断视图；
- session root 默认自动管理，高级用户仍可配置；
- 交付证据、任务历史和诊断副产物在数据模型上保持可区分，为后续 retention 做准备。

#### 4. 公开展示基线

- 提供可复现的安装与离线 Fake demo；
- README 首页展示一句话定位、完整闭环、架构图、当前限制和演示入口；
- 录制 60–90 秒的 VS Code 完整任务演示；
- 建立基础 CI、License、版本说明和 GitHub issue 模板；
- 产品展示网页先采用静态页面，聚焦问题、演示、两个技术亮点和真实边界，不建设独立复杂 Web 应用。

#### v0.5 验收场景

在 VS Code 打开确定性 bug fixture，用户输入修复任务；Agent 读取代码、申请运行测试、受控修改并复测；界面展示 diff 和测试通过；接受后修改才进入 source。还必须证明：

- 接受前 source 保持不变；
- source 变化时 Apply fail closed；
- Reject 不修改 source；
- 重启后可以恢复待审查任务；
- Fake provider 可离线演示完整流程；
- DeepSeek 至少完成一次真实端到端验收；
- 默认界面不要求用户理解内部 mode、worktree 和 receipt。

#### v0.5 非目标

- 通用 RAG、长期记忆或完整多语言索引；
- 多 Agent 自由协作；
- 任意 shell、依赖安装和大量新命令；
- 云端账户、团队协作和远程执行；
- 用 UI 绕过或弱化 v0.4 的安全不变量。

### v0.6：dirty source snapshot 与任务迁移

在产品入口稳定后，支持真实开发中的 `HEAD + 未提交修改`：

- staged/unstaged/untracked snapshot manifest；
- 创建时的 source snapshot identity；
- task workspace 精确复现用户现场；
- 区分用户初始修改、Agent edits 和测试副作用；
- source 变化后创建派生任务或新 workspace revision；
- 在最新代码上重新应用候选、重新测试和重新批准；
- 冲突发生在独立集成工作区，不直接写 source。

验收目标：开发者和 Agent 可以基于明确快照并行工作，同时不丢失修改归属。它是日常可用性的关键里程碑，但不是 v0.5 确定性展示闭环的前置条件。

### v0.7：可解释的仓库导航与上下文选择

第二个产品技术亮点优先解决“为什么 Agent 读取这些代码”，而不是先接入一个泛化向量数据库：

- repository map 和符号索引；
- 定义、引用、调用关系和相关测试导航；
- 基于任务选择有限上下文，并展示选择理由；
- 词法/结构检索作为基线，语义检索作为可选增强；
- 对上下文命中率、token 使用和任务效果建立可重复评测。

验收目标：在中型 fixture 中，Agent 不依赖遍历整个仓库即可定位实现和相关测试，用户能看到上下文来源与选择理由。

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
- context compression；
- 长期记忆；
- 成本与耗时统计。

## 成熟度判断

| 层级 | 标准 | 当前状态 |
| --- | --- | --- |
| L1 技术原型 | 读取、模型工具调用、受控测试、日志 | v0.3 已完成基线 |
| L2 任务闭环 | 候选编辑、测试、diff、接受或放弃 | v0.4 |
| L2.5 可展示产品 | 稳定客户端边界、VS Code 任务闭环、公开演示 | v0.5 |
| L3 可靠本地工具 | dirty source、恢复、变化检测、常见项目稳定工作 | v0.6 以后 |
| L4 成熟安全产品 | host sandbox、网络/资源限制、并发协作、多语言工具链 | 长期方向 |
