# v0.5 用户故事与产品行为

> **Historical design：** 本文属于原 v0.5 VS Code Showcase 路线，不再是当前产品行为的上游权威。Task/Run 等内容可以用于历史参考，但不能直接作为新一轮规划依据。当前状态见[当前设计上下文](../PROJECT_GUIDE.md)。

本文定义 CodeAgent Runtime v0.5 的用户可观察行为，是 Application API、客户端协议和 VS Code Extension 设计的上游依据。

本文先回答“用户要完成什么、Runtime 必须保证什么”，暂不规定 Python 类、协议字段和界面组件。实现设计必须能够追溯到本文中的用户故事；没有用户故事支撑的通用能力不默认进入 v0.5。

## 1. 版本目标

v0.5 的目标是把 v0.4 已有的最小 Coding 闭环，通过稳定的本地 Runtime 边界提供给 VS Code：

```text
创建或恢复任务
→ Agent 只读查阅当前 source
→ 真正需要测试或编辑时申请受控能力
→ Runtime 按需创建 task workspace
→ Agent 在同一个 task workspace 中测试、编辑和复测
→ 用户查看待审查修改和测试证据
→ 用户接受、继续调整或放弃修改
```

Python Runtime 仍是 Session、Workspace、Policy、Approval、工具执行、修改交付和 Apply 的唯一权威。VS Code Extension 负责展示状态和收集用户操作，不直接执行 pytest、不直接修改 source，也不根据内部日志自行推断业务状态。

## 2. 用户心智模型与内部术语

### 2.1 主要用户

主要用户是在 VS Code 中处理本地、可信 Git 仓库的开发者。用户希望描述一个任务、查看进度并审查修改，不应被要求理解 `readonly`、`execution`、session root、detached worktree、receipt 或 Candidate hash。

### 2.2 用户可见概念

- **Task**：用户可创建、打开、继续和审查的一次 Coding 任务。
- **Run**：由一次用户消息或“继续调整”触发的一轮 Agent 执行。一个 Task 可以包含多个按顺序发生的 Run。
- **Source**：用户在 VS Code 中打开的正式项目目录。
- **Approval**：用户对一次固定风险操作或能力升级作出的明确决定。
- **待审查修改（Changes）**：Agent 已完成并交给用户查看的一组修改，包含 changed files、diff 和测试结论。
- **Resume**：Runtime 重启后重新加载 Task、检查底层状态，并确定哪些操作仍可安全执行。
- **Interrupted**：之前的运行操作没有可确认的完整结果，Runtime 不假装它已经成功，也不从未知执行点自动续跑。

### 2.3 Runtime 内部概念

- **Workspace Revision**：Task 第一次需要测试或编辑时，Runtime 基于已确认的 source identity 按需创建的隔离执行现场。当前实现可以使用 Git worktree。
- **Candidate / ChangeSnapshot**：支撑“待审查修改”的内部不可变快照，绑定固定 patch、测试证据和 source identity。
- **Receipt / Artifact**：用于恢复、审计和排障的底层证据。

Candidate 的不变性是 Runtime 的安全保证，不是要求用户管理的产品对象：

```text
用户看到的修改
= 测试对应的修改
= 接受时实际应用的修改
```

“不可变”“持久化多久”和“是否展示给用户”是三个不同问题。待审查修改必须能够跨重启恢复；旧版本修改和诊断 artifacts 的保留时间由 retention policy 决定，不要求永久保存或默认展示。

## 3. 核心产品原则

1. 用户从 Task 开始，不预先选择 `readonly` 或 `execution`。
2. Task 是长期、多轮上下文；Run 是一次可完成、停止或失败的执行。停止一个 Run 不等于结束整个 Task。
3. Task 默认直接从 source 进行只读探索，不在创建时预先创建 worktree。
4. Runtime 只有在真正需要执行仓库代码或编辑时才按需创建 Workspace Revision；创建 worktree 本身是内部准备动作，不单独要求用户 Approval。
5. Workspace Revision 一旦创建，后续读取、测试、编辑和复测都绑定同一 active workspace，不能混读 source 和 task workspace。
6. 从 source 切换到 Workspace Revision 前必须重新检查 source identity；发现变化时不能静默切换到另一份代码。
7. Agent 只有请求权，Runtime 持有执行权。
8. 接受修改前，source 保持不变。
9. 用户看到的待审查修改在内部由不可变 Candidate 支撑，不能被后续 worktree 变化静默替换。
10. Apply 前必须重新检查 source；发现变化时 fail closed。
11. 用户可以接受、继续调整或放弃待审查修改，不被限制为二选一。
12. VS Code 或 Runtime 重启后，任务历史和当前待审查修改仍可恢复。
13. 未完成的模型、工具、测试或 Apply 操作不得在重启后被推断为成功。
14. 插件通过稳定的任务状态和产品事件展示信息，不解析终端文本、`events.jsonl` 或 artifact 目录。
15. Runtime 始终以磁盘上已保存的文件为 source；VS Code 未保存 buffer 不是 Git dirty source，也不会被 Runtime 暗中读取。
16. Git worktree 只保护代码状态，不构成主机级 sandbox；执行可信边界必须向用户诚实展示。

## 4. v0.5 主用户旅程

### US-00：Extension 启动并进入可用状态

**作为** 打开 VS Code workspace 的开发者，  
**我希望** Extension 能启动或连接本地 Runtime，并清楚报告是否可用，  
**从而** 在创建或恢复 Task 前知道当前环境是否准备完成。

基本流程：

1. Extension 激活并识别当前 VS Code workspace。
2. Extension 启动或连接受其管理的本地 Python Runtime。
3. 双方完成版本与 capability handshake。
4. Runtime 检查 workspace 基本可用性和 provider 配置状态。
5. Extension 获取当前 workspace 的 Task 列表并进入 ready 状态。

验收标准：

- Runtime 尚未 ready 时，Extension 不允许发送可能执行业务的请求；
- Python 路径、Runtime executable 或启动参数错误时显示可操作的配置提示；
- Runtime 启动失败或意外退出时，当前 UI 显示 disconnected，不把运行中的 Run 推断为成功；
- workspace 不是受支持 Git repo 时，允许展示清晰限制；只读能力与需要 Workspace Revision 的能力是否可用必须明确区分；
- provider 未配置时仍可列出和查看本地 Task；只有真正发起对应 provider Run 时返回配置错误；
- protocol 不兼容时 fail closed；
- 本节只规定产品启动行为，不设计安装器、进程传输或协议字段。

### US-01：创建任务并进行只读探索

**作为** 在 VS Code 中工作的开发者，  
**我希望** 直接描述需要分析或修复的问题，  
**从而** 不必先理解 Runtime 的内部运行模式。

基本流程：

1. 用户在任务输入框中输入目标。
2. 插件请求 Runtime 创建 Task。
3. Runtime 持久化 Task identity、source identity、provider 和第一条用户消息。
4. Runtime 返回 Task 已创建，并直接基于 source 开始只读探索。
5. 插件显示任务标题、当前阶段和 Agent 输出。

验收标准：

- 用户无需选择 `readonly` 或 `execution`；
- Task 创建后立即具有稳定的 `task_id`；
- 只读任务不创建 task worktree，也不产生 worktree 恢复和清理负担；
- 插件关闭后，已创建 Task 仍能被列出；
- 创建失败时返回结构化原因，不遗留一个看似可用但无法加载的 Task；
- 未获得进一步能力前，Runtime 不执行仓库代码、不编辑文件，也不直接写 source。

### US-02：查看执行进度和结果

**作为** 正在等待 Agent 工作的开发者，  
**我希望** 看到清晰的任务阶段、关键工具活动和回复，  
**从而** 知道 Runtime 正在做什么，而不必阅读原始审计日志。

验收标准：

- 前端可以获取 Task 当前完整状态；
- 运行过程中可以收到增量事件，而不需要轮询原始 Session 文件；
- 插件可以区分 `exploring`、`preparing_workspace`、`testing`、`editing`、`verifying`、`reviewing` 和 `applying`；
- 事件丢失或插件重连后，可以用任务快照恢复当前界面；
- 原始 stdout、stderr、hash 和 receipt 默认折叠，但可以在诊断入口查看关键引用；
- 未知内部事件不会导致插件错误推断 Task 已成功或失败。

### US-02A：在当前 Task 中继续对话

**作为** 正在调查或修复问题的开发者，  
**我希望** 在同一个 Task 中继续提问、补充信息或要求下一步操作，  
**从而** 保留上下文，而不是把每条消息当成一个新 Task。

基本流程：

1. Task 当前没有 active Run。
2. 用户发送一条新消息。
3. Application 持久化用户消息并创建新的 `run_id`。
4. Runtime 使用该 Task 的历史消息、已确认工具结果和当前 Workspace Revision（如果存在）构建上下文。
5. 新 Run 可以继续只读探索，也可以在实际需要时触发现有 Revision 的使用或首次按需物化。
6. Run 完成后，Task 回到可继续状态，或进入 `changes_ready`。

验收标准：

- Task 不等于一次 Prompt、模型调用或 Run；
- 一个 Task 可以包含多个按顺序发生的 Run；
- Task 没有 active Run 时才允许发送普通新消息；
- 一个 Task 同一时刻最多有一个 active Run；
- 上一个 Run 的失败、取消或中断不会自动结束整个 Task；
- 未确认完成的工具结果不能作为成功历史进入下一 Run；
- 历史上下文由 Runtime 从持久化事实重建，客户端不提交拼装后的模型消息。

### US-02B：停止当前 Run

**作为** 发现 Agent 方向不对或不希望继续等待的开发者，  
**我希望** 停止当前正在运行的 Run，  
**从而** 保留 Task 历史并在之后继续，而不是放弃整个 Task。

基本流程：

1. 用户在模型生成、工具读取、Workspace Revision 创建、pytest、编辑或继续调整期间点击 Stop。
2. Application 把当前 Run 标记为正在取消，并向 Runtime 请求 best-effort 停止。
3. Runtime 在安全边界处停止继续调度新的模型或工具操作，并终止可终止的子进程。
4. 已确认完成的消息、receipt、Workspace Revision 和内部快照继续保留。
5. 当前 Run 结束为 `cancelled`；Task 回到停止前最近一个稳定产品状态。

验收标准：

- Stop 默认只取消当前 Run，不把 Task 标记为 abandoned 或 interrupted；
- `cancelled` 表示用户主动停止，`interrupted` 表示进程或运行环境意外中断，两者语义分开；
- 没有完整 receipt 的 pytest 不得标记为成功；
- 已创建的 Workspace Revision 不因 Stop 自动删除；
- 已完成的单次原子编辑不回滚为未知状态，未开始的新操作不再执行；
- 从 `changes_ready` 发起继续调整时，旧内部快照在新快照成功冻结前仍是当前稳定待审查修改；
- 如果调整 Run 被取消、失败或中断，旧待审查修改仍可查看、接受或放弃；
- 同一个 Run 的重复 Stop 请求具有确定结果，不重复终止或产生冲突状态。

### US-03：按需进入受控执行阶段

**作为** 希望 Agent 分析后再决定是否执行代码的开发者，  
**我希望** Runtime 只在确实需要 pytest 或编辑时创建隔离执行现场，  
**从而** 让纯分析任务保持轻量，并避免提前维护无用 worktree。

基本流程：

1. Agent 在只读探索中请求 pytest 或候选编辑能力。
2. Runtime 根据用户意图和实际操作执行 Policy 判断。创建 worktree 只是内部准备动作，不作为单独的用户风险请求。
3. 如果首次操作是执行 pytest，用户批准的是“执行这次固定的仓库代码”，批准后 Runtime 才重新检查 source、物化 Revision 并运行命令。
4. 如果当前用户消息要求修改且首次操作只是受控文本编辑，Runtime 可以在重新检查 source 后物化 Revision 并允许编辑，不额外弹出“创建 worktree”Approval。Application 不要求客户端额外提交 `edit_intent` 枚举；用户消息、Agent 请求和最终 Changes 审查构成这条产品路径。
5. Runtime 创建 Workspace Revision 后，重建绑定该 active workspace 的工具和 Runner。
6. 后续读取、pytest、编辑和复测都在同一个 Workspace Revision 中进行；每次执行仓库代码仍按 Command Policy 请求 Approval。

验收标准：

- 每个 Task 可以没有 Workspace Revision，也可以有一个当前活动的 Revision；
- 不是每次 pytest 或编辑都创建新 worktree；同一轮 Coding 工作复用同一 Revision；
- source 在只读探索后发生变化时，Runtime 不基于旧上下文静默创建 worktree；
- 切换完成后，读取工具不能继续读取 source 而写入或测试另一个 worktree；
- 创建 Workspace Revision 失败时，Task 保留只读历史并返回结构化失败原因；
- 拒绝首次 pytest Approval 时不创建 worktree，Agent 收到可继续推理的结构化拒绝结果；
- Agent 应遵循当前用户消息，不主动把明确的只读请求扩展为修改；即使模型错误请求了隔离编辑，正式 source 仍不会在 Accept 前变化，用户可以 Stop 或放弃 Changes。

### US-04：批准或拒绝风险操作

**作为** 对本地代码执行负责的开发者，  
**我希望** 在 Runtime 执行仓库代码或最终应用修改前看到固定操作和风险，  
**从而** 对一次明确操作作出批准或拒绝。

验收标准：

- Approval 必须有稳定的 `approval_id`；
- source 文本读取默认允许；
- 用户已明确要求修改时，隔离 Workspace Revision 内的受控文本编辑默认允许；
- 执行仓库代码（v0.5 为固定 pytest）需要针对固定命令的单次 Approval；
- 修改正式 source 只通过用户明确“接受修改”完成；
- 创建或恢复 Git worktree 不单独弹出 Approval，因为它是上述操作的内部准备或恢复步骤，不是独立用户风险；
- 批准后只执行已经展示且 identity 未变化的固定操作；
- 拒绝不会被解释为成功，也不会留下悬空的模型 tool call；
- 同一 Approval 的重复决定具有确定结果，不能执行两次；
- 过期、错误 Task 或已处理的 Approval 被拒绝；
- 插件断开或 Runtime 重启不会自动批准旧请求。

### US-05：查看待审查修改

**作为** 希望控制正式代码变化的开发者，  
**我希望** Agent 先在隔离工作区完成修改和验证，再展示固定的待审查修改，  
**从而** 在 source 改变前审查实际交付内容。

基本流程：

1. Agent 在 Workspace Revision 中完成受控编辑。
2. Runtime 要求最新编辑之后存在成功 pytest 证据。
3. Runtime 在内部冻结不可变 Candidate / ChangeSnapshot。
4. 插件只以“待审查修改”展示 Agent 修改摘要、changed files、完整 diff、最新验证测试、测试状态、workspace side effects 和重要 warning。
5. 用户可以关闭插件，之后继续审查相同修改。

验收标准：

- 内部快照绑定 Task、Workspace Revision、source identity、固定 patch hash 和测试证据；
- 快照冻结后，后续 worktree 修改不会改变用户已看到的修改；
- artifact 被篡改时读取失败；
- 接受或放弃前 source 保持不变；
- 重启后展示的修改与重启前具有相同内部 identity 和 patch hash；
- Candidate ID、hash 和 receipt 不作为普通用户必须理解的默认界面元素；
- 测试状态明确区分通过、失败和未运行，不能用 Agent 文本总结替代 receipt 事实；
- workspace side effects 与 Agent 交付文件分开展示；
- Accept、Continue 和 Abandon 都作用于整组 Changes；
- v0.5 不支持 partial file accept、hunk-level accept 或由客户端重新组合 Candidate。

### US-06：接受待审查修改

**作为** 已完成 diff 和测试证据审查的开发者，  
**我希望** 明确接受当前看到的修改，  
**从而** 只把这组修改应用到 source。

基本流程：

1. 用户点击“接受修改”。
2. 插件提交 Task 和当前待审查修改的内部 identity。
3. Runtime 重新加载并验证内部不可变快照。
4. Runtime 检查 source HEAD、clean 状态和 patch preflight。
5. Runtime 在应用前再次检查 source，并应用完全相同的 patch bytes。
6. Runtime 保存 Apply receipt，插件显示成功结果和 changed files。

验收标准：

- 插件不能直接写 source 或调用 `git apply`；
- source 变化、变脏或 patch 不再适用时 Apply 失败；
- 并发或重复操作不会把同一修改应用两次；
- 失败时保留待审查修改，且不自动 merge、rebase、stash 或覆盖用户修改；
- 成功后可以清理 task workspace；保留哪些完整 artifacts 由 retention policy 决定。

### US-07：继续调整待审查修改

**作为** 对当前实现方向基本满意但希望继续调整的开发者，  
**我希望** 在审查后继续向 Agent 提出修改意见，  
**从而** 不必在立即接受和完全放弃之间二选一。

基本流程：

1. 用户针对当前待审查修改发送调整意见。
2. Runtime 启动新的调整 Run；旧 Changes 及其内部 Candidate 仍是当前稳定交付版本。
3. Agent 继续使用同一个 Workspace Revision 编辑和复测。
4. Runtime 冻结新的内部快照。
5. 只有新 Candidate 成功冻结后，Runtime 才将旧 Candidate 标记为 superseded，并由插件展示新的待审查修改。

验收标准：

- 旧快照不可被新修改原地覆盖；
- 用户默认只看到当前最新修改，不必管理 Candidate 版本列表；
- 新快照必须重新绑定最新编辑后的测试证据；
- source 在整个调整过程中保持不变；
- 如果调整 Run `cancelled`、`failed` 或 `interrupted`，旧 Changes 继续可查看、接受或放弃；
- 旧快照可以按数量或 TTL 清理，但当前待审查快照在 Task 可恢复期间必须保留。

### US-08：放弃待审查修改

**作为** 不接受当前修改的开发者，  
**我希望** 放弃这次修改，  
**从而** 明确结束当前交付而不改变 source。

验收标准：

- 放弃不修改 source；
- Runtime 记录用户决定和最小结果摘要；
- task workspace 和完整 patch 可以按明确的清理/retention policy 回收，而不是因为内部快照不可变就永久保存；
- 重复放弃的结果确定，不产生互相冲突的状态；
- 放弃后 Task 进入 `abandoned` 终态；后续工作创建新 Task。

## 5. Workspace Revision 生命周期

Workspace Revision 是按需执行资源，不是用户创建 Task 时必须选择的模式。

```text
Task 创建
  ↓
只读 source，workspace_revision = none
  ↓ 首次需要执行仓库代码或进行受控文本编辑
  ├─ pytest 等执行仓库代码：用户批准固定操作 → 重新检查 source identity
  └─ 用户明确要求修改：受控文本编辑不单独 Approval → 重新检查 source identity
  ↓ Runtime 内部准备
创建 Workspace Revision（创建 worktree 本身不单独 Approval）
  ↓
读取、测试、编辑、复测、继续调整均复用该 Revision
  ↓
接受 / 放弃 / 明确 cleanup
  ↓
回收 worktree，保留规定范围内的 Task 历史和交付证据
```

重要边界：

- 按需创建减少纯分析 Task 的同步、Resume 和清理成本，但不能消除 source → task workspace 的切换检查；
- Revision 创建前若 source identity 已变化，Runtime 必须重新读取最新代码或要求用户确认新的基线，不能假装旧分析仍对应当前代码；
- Revision 创建后，用户继续修改 source 不会自动同步到 task workspace；
- v0.5 一个 Task 最多维护一个当前活动 Revision，不实现多个并行 Revision、自动 rebase 或自动迁移；
- 每次测试单独创建 worktree 会破坏编辑与测试的一致代码状态，因此不采用这种粒度。

## 6. Resume 用户故事

Resume 不是单一动作。v0.5 分别支持任务发现、界面恢复、安全接管和用户继续。只有已经物化 Workspace Revision 的 Task 才需要恢复 worktree。

### US-09：列出历史任务

**作为** 重新打开 VS Code workspace 的开发者，  
**我希望** 看到属于当前 workspace 的历史 Task，  
**从而** 找到未完成或待审查的工作。

验收标准：

- 列表不依赖旧 Runtime 进程仍然存活；
- 一个损坏的 Session 不阻止其他 Task 被列出；
- Task summary 能区分可继续、待审查、已完成和需要关注的任务；
- 列表能够表示 Task 是否曾经创建 Workspace Revision，但不要求用户理解 worktree；
- 不自动恢复或执行列表中的所有 Task。

### US-10：恢复任务界面

**作为** 打开历史 Task 的开发者，  
**我希望** 看到任务历史、当前状态、最近测试和待审查修改，  
**从而** 在错过实时事件后仍能理解当前现场。

验收标准：

- 获取快照本身不调用模型、不执行 pytest、不修改文件；
- 快照包含当前 Task 状态、阶段、允许操作和最后事件序号；
- `changes_ready` Task 可以恢复相同待审查修改；
- 前端不需要重放全部审计事件才能恢复正确界面。

### US-11：恢复未物化的只读任务

**作为** 想继续只读 Task 的开发者，  
**我希望** Runtime 在不创建 worktree 的情况下恢复历史并检查 source，  
**从而** 继续分析而不承担执行现场的生命周期成本。

验收标准：

- `workspace_revision = none` 时不执行 worktree inspection；
- source identity 未变化时，可以重建模型上下文并继续消息；
- source identity 已变化时，历史仍可查看，但继续前必须明确基于最新 source 重新读取上下文；
- Resume 本身不会隐式创建 Workspace Revision。

### US-12：恢复已物化的 Coding 任务

**作为** 想继续已有 Coding Task 的开发者，  
**我希望** Runtime 检查保存的 Workspace Revision 是否仍可信，  
**从而** 避免旧上下文在错误代码版本上继续运行。

验收标准：

- Runtime 检查 source、task worktree、Git 注册关系、base HEAD、dirty 状态和当前内部快照；
- journal 和 command delta 能解释的已知修改可以恢复；
- 未知 dirty worktree、丢失 worktree 或 base 不一致不会恢复执行能力；
- source 变化时不自动刷新、merge 或重新绑定旧证据；
- Resume 返回结构化状态、警告和允许操作，而不是只返回终端文本。

### US-13：恢复待审查修改

**作为** 在修改已准备好后关闭 VS Code 的开发者，  
**我希望** 重新打开后继续查看、接受、调整或放弃同一组修改，  
**从而** 不必重新运行整个 Agent 任务。

验收标准：

- 内部快照 identity、patch hash、changed files 和测试证据保持不变；
- task worktree 不存在时，只要固定快照 artifacts 完整，仍可以查看、接受或放弃修改；
- 缺少 worktree 时不能“继续调整”，除非未来显式支持重建 Revision；
- Apply 前仍必须重新检查 source identity；
- source 已变化时仍可查看历史修改，但不能把它标记为可安全接受。

### US-14：处理运行中断

**作为** 遇到 VS Code、Runtime 或主机进程意外退出的开发者，  
**我希望** 重启后看到诚实的中断状态和可选下一步，  
**从而** 不把未完成操作误认为成功。

| 中断前记录状态 | 重启后的最低保证 |
| --- | --- |
| 空闲、普通回复完成 | 恢复历史，允许在安全检查后继续消息 |
| 等待 Approval | 旧 Approval 不自动批准；恢复为需要重新确认或已失效 |
| 模型调用中 | 当前 Run 标记 `interrupted`，不推断模型已返回 |
| Workspace Revision 创建中 | 检查创建结果并清理或保留可诊断现场，不假装切换完成 |
| pytest 或其他工具执行中 | 当前 Run 标记 `interrupted`，无完成 receipt 就不视为成功 |
| 待审查修改已冻结 | 恢复为 `changes_ready`，允许继续审查 |
| Apply 进行中 | 检查 Apply receipt 和 source；不盲目重复 Apply |
| 已完成接受/放弃 | 从最终 receipt 恢复终态 |

验收标准：

- Runtime 能识别由旧进程遗留的非终态 Run；
- `interrupted` Run 展示最后一个已确认完成的步骤，Task 本身回到最近稳定状态；
- 用户可以根据允许操作显式重试、继续或放弃；
- v0.5 不承诺从模型调用或子进程的任意指令位置自动续跑；
- pytest、待审查修改和 Apply 的成功判断必须基于完整持久化证据。

## 7. Source 变化与失败用户故事

### US-14A：处理 VS Code 未保存文件

**作为** 在 VS Code 中仍有未保存编辑内容的开发者，  
**我希望** 在 Agent 基于磁盘代码工作前得到明确提示，  
**从而** 不会误以为 Runtime 看到了编辑器内尚未保存的版本。

产品规则：

- Runtime 始终以磁盘上的已保存文件为 source，v0.5 不读取 VS Code virtual document 或 unsaved buffer；
- unsaved editor buffer 是客户端内存状态，Git dirty source 是磁盘和 Git 状态，两者必须分别检测和说明；
- Extension 在创建 Task、为 `open` 且无 active Run 的 Task 发起新 Run、批准首次执行仓库代码以及接受修改前检查当前 workspace 是否有 unsaved buffer；
- 创建 Task 或发起普通 Run 时，用户可以选择“保存全部并继续”“仍使用磁盘版本继续”或“取消”；选择磁盘版本时必须显示持续 warning；
- 接受修改前如果存在任何 workspace unsaved buffer，Extension 默认阻止继续，只提供“保存全部后重试”或“取消”，避免随后保存旧 buffer 覆盖新 source；
- “保存全部”之后可能形成 Git dirty source，Runtime 仍必须执行独立的 source identity 和 clean Git 检查；
- Runtime/Application 不能把 Extension 的检测结果当作安全事实，真正执行时仍以磁盘和 Domain 检查为准。

验收标准：

- 用户能明确知道 Agent 使用的是磁盘版本；
- 选择使用磁盘版本继续的决定只影响本次 Task/Run 的提示，不会让 Runtime 读取未保存内容；
- Extension 无法检测 buffer 状态时，不虚假声明编辑器与磁盘一致；
- unsaved buffer 不被记录成 Git dirty，也不被写入 Candidate 或 source identity。

### US-15：首次物化或 Resume 时 source 已变化

**作为** 在 Task 期间继续修改项目的开发者，  
**我希望** Runtime 告诉我 Task 上下文与当前 source 已不一致，  
**从而** 避免旧分析或测试证据被错误用于新代码。

验收标准：

- Runtime 区分只读阶段 observed identity 变化、source HEAD 改变、source dirty、worktree stale 和 worktree missing；
- 插件展示用户可理解的影响和仍允许的操作；
- 历史消息和待审查修改仍可查看；
- 旧修改默认不可安全接受；
- v0.5 不自动迁移修改到最新 source。

### US-16：任务失败后查看和继续

**作为** 遇到模型、工具或 Runtime 错误的开发者，  
**我希望** 看到失败发生在哪个阶段及可执行的下一步，  
**从而** 可以重试、继续对话或保留现场排障。

验收标准：

- 失败包含稳定错误类别和用户可读说明；
- 内部堆栈和敏感信息不作为默认用户消息暴露；
- 失败不会自动删除 Task 历史、可恢复的 Workspace Revision 或当前待审查修改；
- 是否允许重试由当前 Task、Workspace Revision 和内部快照状态决定；
- 插件不能仅凭进程退出码推断修改或 Apply 成功。

### US-17：插件与 Runtime 版本不兼容

**作为** 使用不同版本插件和 Runtime 的开发者，  
**我希望** 在启动阶段得到明确兼容性提示，  
**从而** 避免任务运行中途才因协议字段不一致失败。

验收标准：

- 插件与 Runtime 在执行任务前完成版本和 capability 握手；
- 不兼容版本 fail closed，并给出升级或配置提示；
- 可选能力通过 capability 声明，不要求客户端猜测方法是否存在；
- 协议日志与普通 Runtime 日志使用不同输出通道。

## 8. 持久化与 retention

持久化服务于恢复和安全，不意味着所有内部对象永久保留：

| 数据 | v0.5 最低保留要求 |
| --- | --- |
| Task identity、消息和关键时间线 | Task 可发现和恢复期间保留 |
| 当前待审查修改 | 在接受、放弃或明确清理前必须保留 |
| 已接受修改 | 保留结果、changed files、测试摘要和 Apply 关联证据；完整 patch 的长期保留待定 |
| 已放弃修改 | 保留用户决定和结果摘要；完整 patch/worktree 可按策略清理 |
| 被继续调整取代的旧快照 | 不默认展示，可按数量上限或 TTL 清理 |
| stdout/stderr、edit journal、runtime HOME/TEMP | 诊断数据，必须有容量或时间策略，不能因 Candidate 不可变而无限保留 |

任何自动清理都不能删除当前待审查修改、未决 Apply 证据或用户仍依赖的恢复现场。具体默认 TTL 和容量限制可以晚于首个协议切片实现，但数据分类必须在 schema 设计时保留。

## 9. Task、Run 与内部状态

Task 表示长期产品上下文，只保留稳定生命周期：

```text
open            可继续发送消息，当前没有待审查修改
changes_ready   存在当前待审查修改；可接受、继续调整或放弃
completed       当前修改已经成功应用，Task 终结
abandoned       用户放弃当前 Task，Task 终结
```

Run 表示一次执行，承载短暂和失败状态：

```text
running
waiting_for_approval
cancelling
completed
cancelled
failed
interrupted
```

Run phase 只解释当前工作，不决定安全权限：

```text
starting
exploring
preparing_workspace
testing
editing
verifying
reviewing
```

内部状态独立维护，不全部暴露给客户端：

```text
Workspace Revision:
none / creating / active / stale / dirty_known / dirty_unknown / missing / cleaned

Candidate:
none / frozen / superseded / applied / rejected
```

关键关系：

- 一个 Task 包含多个历史 Run，但同一时刻最多一个 active Run；
- `waiting_for_approval`、`cancelled`、`failed` 和 `interrupted` 都是 Run 状态，不是 Task 状态；
- 普通 Run 完成后 Task 保持 `open`；成功产生新修改后 Task 进入 `changes_ready`；
- 从 `changes_ready` 启动调整 Run 时，Task 仍保持 `changes_ready`，旧快照继续是当前稳定交付，直到新快照成功冻结；
- 调整 Run 取消、失败或中断时，Task 继续保持 `changes_ready`；
- 没有旧修改的 Run 取消、失败或中断时，Task 回到 `open`；
- Task 状态不能替代底层 Workspace、Candidate、Approval 或 command status。

## 10. 状态与允许操作

| Task / Run 条件 | 主要允许操作 |
| --- | --- |
| `open`，无 active Run | 查看历史、Resume 检查、发送消息 |
| `changes_ready`，无 active Run | 查看修改、接受、继续调整、放弃 |
| active Run 为 `running` | 查看进度、Stop 当前 Run |
| active Run 为 `waiting_for_approval` | 查看 Approval、批准、拒绝、Stop 当前 Run |
| active Run 为 `cancelling` | 查看进度；重复 Stop 返回相同操作结果 |
| 最近 Run 为 `cancelled` / `failed` / `interrupted`，当前无 active Run | 按 Task 稳定状态继续消息或审查旧修改 |
| `completed` | 查看历史和已应用修改 |
| `abandoned` | 查看历史和按 retention 保留的摘要 |

Application 必须返回 `allowed_actions` 作为当前产品能力投影，Extension 不根据枚举自行拼装按钮。任何操作真正执行时仍必须重新通过 Workspace、Policy、Approval、内部 Candidate 和 source identity 检查，不能只凭 `allowed_actions` 放行。

## 11. 已收敛决策与剩余实现问题

以下问题会影响 Application 状态和 Approval 语义，应在协议字段冻结前确定：

本轮已经收敛的最小方案：

- 用户批准的是执行固定仓库代码，不为创建 worktree 单独弹 Approval；
- 隔离 Revision 内受控文本编辑默认允许，正式 source 仍只由 Accept Changes 修改；
- Runtime 重启后旧 pending Approval 失效，相应 Run 记为 `interrupted`，用户显式重试；
- Stop 只取消当前 Run；
- `completed` 和 `abandoned` 是终态，后续工作创建新 Task；
- worktree 丢失但当前内部快照完整时仍可查看和接受修改，不能继续调整；
- v0.5 每个 Runtime 进程同一时刻最多执行一个 active Run，不提供后台排队。

当前没有尚未解决、会阻塞 Application 或 Server 语义设计的产品决策。以下实现策略需要在对应代码切片前确定，但不应改变本文的用户行为：

1. Stop 模型调用时，当前 provider 是否支持主动取消；不支持时采用“忽略迟到结果并停止后续调度”。
2. Stop pytest 时 Windows 进程树终止的失败应如何向用户分级展示。
3. 旧内部快照、完整 patch、日志和 worktree 的默认 TTL、数量及容量上限。
4. Extension 默认展示多少历史时间线，以及诊断导出入口的最小范围。

在这些决策确定前，实现不得通过隐含默认行为造成不可兼容的协议承诺。

## 12. v0.5 非目标

- 从模型调用或 pytest 进程的任意指令位置自动断点续跑；
- dirty source snapshot 和待审查修改自动迁移；
- 自动 merge、rebase、stash 或冲突解决；
- 同一 Task 同时维护多个并行 Workspace Revision；
- 每次命令临时创建并迁移一个新 worktree；
- 任意 shell、依赖安装和通用网络工具；
- 多 Agent 自由协作；
- 云端账户、团队任务和远程执行；
- 多客户端共享同一个活跃 Runtime 进程；
- 把 Git worktree 描述为主机级安全沙箱；
- 把 Candidate、receipt 或 artifact schema 直接作为普通用户界面；
- 一次性暴露全部内部审计事件作为公共协议。

## 13. 版本验收场景

### 场景 A：纯只读任务

在 VS Code 中创建代码分析 Task，Agent 读取项目并返回分析。关闭并重新打开插件后可以恢复历史；整个过程没有创建 task worktree。

### 场景 B：按需进入完整 Coding 闭环

在确定性 bug fixture 中创建修复 Task。Agent 先只读探索，首次需要 pytest 时申请能力；Runtime 重新检查 source 并创建 Workspace Revision。Agent 在同一个 Revision 中测试、编辑和复测，插件展示待审查修改；接受后相同 patch 才进入 source。

必须证明：

- 用户没有选择 `readonly` / `execution`；
- worktree 在实际需要执行时才创建；
- 切换后所有读、测试和编辑使用同一 active workspace；
- 接受前 source 不变；
- 放弃不改变 source；
- 用户看到的修改在审查期间不可被替换；
- Apply 使用固定内部快照，并在 source 变化时失败。

### 场景 C：继续调整

任务进入 `changes_ready` 后，用户提出调整意见。Agent 复用当前 Workspace Revision 修改并复测，旧内部快照被标记为 superseded，插件只展示新的待审查修改；source 仍保持不变。

### 场景 D：待审查恢复

任务进入 `changes_ready` 后关闭 VS Code 和 Runtime。重新打开后，插件列出该 Task，恢复相同修改和测试摘要，并允许继续接受、调整或放弃。

### 场景 E：运行中断恢复

任务在 Workspace Revision 创建、模型调用、测试或等待 Approval 时终止 Runtime。重启后 Task 不显示为成功；Runtime 将未完成 Run 恢复为 `interrupted`，Task 回到最近稳定状态，并展示可执行下一步。

### 场景 F：source 并发变化

分别验证只读探索后、Workspace Revision 创建后和待审查修改冻结后 source 发生变化。Runtime 不静默同步旧 Task；历史修改仍可查看，但不覆盖、stash 或自动合并用户修改。

### 场景 G：真实 Provider

使用 DeepSeek 至少完成一次只读探索、按需创建 Workspace Revision、Approval、pytest、编辑、复测、修改审查和接受的真实端到端验收。失败时保留可诊断的 Task 历史和规定范围内的 artifacts。

### 场景 H：多轮 Task 与 Stop

在同一 Task 中连续发送分析问题、追问和测试请求，产生多个顺序 Run。测试 Run 期间点击 Stop；该 Run 结束为 `cancelled`，Task 历史、Workspace Revision 和此前稳定 Changes 均不丢失，随后可以继续新 Run。

### 场景 I：未保存编辑器内容

VS Code 存在 unsaved buffer 时分别尝试创建 Task、发起新 Run 和接受修改。Extension 明确提示 Runtime 使用磁盘版本；允许用户保存全部、在非 Apply 操作中确认使用磁盘版本或取消。Accept 在仍有 unsaved buffer 时被阻止，保存后 Runtime 再独立检查 Git/source 状态。

### 场景 J：Extension 启动失败与重连

分别模拟 Runtime executable 配置错误、provider 未配置、handshake 不兼容和 Runtime 意外退出。Extension 不把未完成 Run 显示为成功，能够保留本地 Task 发现入口，并给出与失败类别对应的可操作提示。

## 14. 后续设计产物

Task / Run 已在本文完成初步拆分；Application Use Case、View Model、事件和边界设计已经记录在 [v0.5 Application Layer Design](V05_APPLICATION_LAYER.md)。后续产物应基于这两份现有设计继续收敛，不重新定义其产品语义：

1. 为既有 Task / Run / Workspace Revision / Candidate 状态关系补充可执行的迁移测试表；
2. 细化 source identity 观察、Revision 物化和工具根目录切换规则；
3. 定义协议方法、事件序列化、版本协商和错误码；
4. 定义 Runtime 进程模型与 stdio framing；
5. 完成 VS Code 信息架构和交互原型；
6. 按现有用户旅程实施纵向功能切片。
