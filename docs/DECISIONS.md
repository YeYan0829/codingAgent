# 当前有效决策记录

本文记录近期实践中已经确认、且会约束后续实现的产品级决策。历史版本决策保存在 `archive/`，不自动继承为当前约束。

## D-2026-08-28-01：下一执行层改为 Controlled Arbitrary Command + Sandbox

状态：Accepted and Implemented
范围：Command execution、Validation、Policy、Approval 与 Linux/WSL2 sandbox

### 决策

- 不再继续增加 Command Profile；新执行层完成后整体删除该机制，不做旧 Session 兼容。
- Agent-facing 统一为 arbitrary shell command；是否允许不依据命令名字，而依据 `FILESYSTEM_READ(path)`、`FILESYSTEM_WRITE(path)`、`NETWORK` 等资源能力。
- Linux/WSL2 使用 Git Worktree + Bubblewrap + Resource-scoped Policy + Approval。Worktree 负责源码交付隔离，Bubblewrap 负责命令副作用边界。
- 默认 root 只读、Candidate 和隔离临时目录可写、source/Git/Runtime metadata 不可写、敏感读取隐藏、网络关闭；增量授权支持 ONCE 和 SESSION。
- Bubblewrap 不可用时 fail closed，不回退到宿主直接执行器。
- 详细契约见 [Controlled Arbitrary Command + Sandbox Technical Design](CONTROLLED_ARBITRARY_COMMAND_SANDBOX_TECHNICAL_DESIGN.md)；设计已经 review 并完成实现。

### 对旧决策的影响

D-2026-08-27-02 的“当前不建设 host sandbox”已完成 Command Profiles 阶段的范围控制，不再约束当前实现。D-2026-08-27-03 中“任意 shell 不开放”和 profile 级审批已由本决策取代。

D-2026-08-27-06 的旧 taint 触发条件已被取代：Bubblewrap 内对 Candidate 的 command changes 是合法当前修改；只有无法建立可信 command-after 边界才 tainted，无法确认进程、保护 identity 或恢复结果才 `recovery_required`。

## D-2026-08-27-01：`recovery_required` 当前只提供 fail-closed

状态：Accepted  
范围：Atomic Workspace Edit 首批实现

实现状态：2026-08-27 已完成显式 `WorkspaceState.RECOVERY_REQUIRED` resume gate，并覆盖 Git worktree 表面 clean 的回归场景。

### 背景

Atomic Edit 在 mutation 失败后会尝试把 Session 的 Agent worktree 恢复到 transaction 前状态。正常 rollback 或 resume recovery 成功时，事务进入 `rolled_back`，不会产生成功 edit revision，并可以继续使用 workspace。

如果 rollback、恢复后 identity 校验或未完成事务恢复无法确认成功，Runtime 将事务标记为 `recovery_required`。这表示 Agent worktree 的状态已经不能由 Runtime 可靠证明，而不表示 source workspace 已被修改。

### 决策

当前阶段只要求：

- `recovery_required` 可被确定性检测；
- transaction plan、状态和 rollback backup 保留用于诊断；
- Runtime fail closed，不再为该 Session 注册编辑、验证或采纳能力；
- 不允许通过修改 metadata、忽略 journal 或其他捷径重新开放受保护能力；
- 用户可以保留旧现场，并从可确认的 source 创建新 Session 继续。

实现约束：resume 必须在 Git clean/dirty、managed candidate changes 和 `READY` 判断之前检查并恢复 Atomic Edit transaction；只要 persisted transaction 为 `recovery_required` 或恢复过程无法确认成功，就直接进入独立的 `WorkspaceState.RECOVERY_REQUIRED`。该状态不允许被表面 clean 的 worktree 覆盖。

当前阶段不实现：

- `recover-edit --inspect/--rollback` 等人工恢复 CLI；
- 对同一 session 执行受控修复、完整性复检和 capability re-enable；
- 通用文件系统事务修复器、复杂 WAL、数据库级 durability 或多进程分布式锁。

### 理由

Stage 2 当前优先级是扩展 Coding Agent 的基础工具能力并证明 schema、执行、安全、错误和完整调用链正确。`recovery_required` 属于低频异常后的产品恢复体验；只要能够保留证据并 fail closed，它不阻塞 create/update/delete/move 等核心编辑能力交付。

过早实现人工解锁容易形成绕过 transaction identity 的第二条写入路径，也会扩大当前阶段范围。

### 后续触发条件

出现以下任一情况时重新设计受控恢复流程：

- CLI dogfood 或外部任务评估中实际出现 `recovery_required`；
- session 长任务价值提高，直接放弃 execution worktree 的成本不可接受；
- 产品开始承诺可恢复的长时间任务或面向非技术用户的完整恢复体验；
- Execution Backend 或 host sandbox 引入新的 workspace snapshot/restore 能力。

届时建议提供非 Agent-facing 的 inspect/rollback/recheck 命令。只有恢复后的文件 identity、Git 状态、base commit 和 transaction journal 全部一致时，才允许显式解除 fail-closed 状态。

## D-2026-08-27-02：Execution Boundary 保持最小并内嵌于 Command Profiles 设计

状态：Superseded by D-2026-08-28-01
范围：Command Profiles / Validation 与未来 SWE-bench adapter

### 决策

当前不单独建设通用 Execution Backend 或 sandbox 抽象。下一份 Command Profiles / Validation Technical Design 只定义现有 Local POSIX executor 和未来 SWE-bench adapter 必需的最小执行边界：不可变 request/result、workspace/cwd、executable/argv、environment、timeout、approval、artifacts、失败分类和 workspace audit。

接口不得假设任务 workspace 永远是当前 source/WSL 路径，并应允许评测 adapter 注入已经准备好的 workspace、解释器和执行环境。

当前不设计或实现 Docker 生命周期、remote executor、复杂 requirements/guarantees、network/resource/syscall isolation 或通用 sandbox framework。真正开始 host sandbox 或多个 execution backend 时再形成独立 Technical Design。

### 理由

Session / Workspace / Persistence 重构完成后，优先级将回到补齐 Agent 工具和验证能力。最小边界可以避免 pytest/ruff/typecheck/build 与具体 subprocess 代码重复耦合，也足以支持 SWE-bench 薄 adapter；更广泛抽象缺少实际 backend 和威胁模型依据。

## D-2026-08-27-03：Session 是唯一上层运行容器，不引入 Task 实体

状态：Accepted  
范围：Session、workspace lifecycle、CLI 与后续工具注册

### 背景

当前实现要求用户创建 Session 时选择 `readonly` 或 `execution`。`execution` Session 会立即创建独立 Git worktree，并把该路径称为 task worktree。这个实现便于早期验证安全边界，但混合了四件不同的事：连续对话、用户当前目标、实际读取的 workspace，以及 Agent 当前获准使用的能力。

在当前产品范围内，所谓 Task 没有独立于 Session 的 identity、生命周期、并行关系、预算或所有权。把它固化为数据结构只会增加用户术语、状态转换和持久化负担。

### 决策

- Session 是连续对话和运行状态的唯一上层容器；当前不建立 `task_id`、Task 目录或 Task 状态机。
- Session 创建时固定绑定一个 source workspace，初始直接从 source 提供受控只读能力。
- Session 创建时不预先选择 `readonly` / `execution` mode，也不预先创建 worktree。
- Agent 首次需要编辑或执行受保护命令时，Runtime 经过对应 Policy / Approval 后为该 Session 按需创建独立 Git worktree。
- worktree 是 Agent 在该 Session 中的当前隔离工作区，不属于 Task；创建后，读取、搜索、编辑和验证都应针对同一个 active workspace。
- worktree 的存在不等于获得任意权限。编辑只能经过结构化编辑工具。本文当时采用可信 profile、关闭任意 shell；该命令部分已由 D-2026-08-28-01 的 sandboxed `run_command` 与资源 Policy 取代。
- 采纳或丢弃结束的是当前一轮修改，不结束 Session。具体 worktree 生命周期已由后续 D-2026-08-27-05 收敛：两者都保留同一 worktree 并回到 `changes_active`，不再回到 source-only。
- 内部可以用单调递增的 `workspace_revision` 区分同一 Session 的多轮隔离工作区，但它不是用户需要理解的产品概念。
- 当前原型没有需要迁移的旧 Session 数据；本轮重构可以直接替换 metadata schema 和 CLI mode，不实现旧格式兼容层。

### 安全约束

从 source 切换到 worktree 不能只修改一个路径字段。Runtime 必须在确定 worktree identity 后统一更新持久化状态，并重建依赖 workspace 的 PathGuard、ToolRegistry、搜索/读取、编辑、命令和 Candidate/当前修改服务。失败或审批拒绝时保持 source-only，不得留下看似可执行的半初始化状态。

`recovery_required` 仍具有最高优先级。任何表面 clean、重新构造 registry 或 resume 都不能绕过该 gate。

### 后续触发条件

只有出现并行目标、多个 active workspace、多 Agent 分工、独立任务预算/负责人或 benchmark 需要稳定 task identity 时，才重新评估 Task 数据结构。

## D-2026-08-27-04：持久化按职责和生命周期收敛

状态：Accepted  
范围：Session、编辑事务、命令、验证、当前修改和诊断数据

### 背景

当前 audit-first 原型同时保存 events、transcript、命令 request/result/stdout/stderr/workspace patch、编辑 request/plan/state/patch/backup、Candidate manifest/patch/status 和 apply receipt。它们帮助验证早期安全不变量，但同一事实分散在多个文件中，用户难以理解，也会诱使后续每种工具建立新的 artifact 家族。

### 决策

产品层只暴露以下概念：

- **会话历史**：用户、Agent、审批和关键操作发生了什么；
- **当前修改**：active worktree 相对 source/base 的尚未采纳变化；
- **验证结果**：哪些受控检查针对哪一版当前修改运行以及结果；
- **采纳或丢弃**：当前一轮修改的最终处理。

内部数据按生命周期分为三类：

1. **Session 状态与历史**：Session identity、source/active workspace、状态转换、消息和关键事件；
2. **当前结果**：当前 diff、修改 revision、验证摘要和采纳/丢弃结果；
3. **诊断数据**：完整 stdout/stderr、事务 plan/backup、命令 workspace delta、runtime HOME/TMP 等仅用于失败恢复和调试的材料。

后续实现必须遵守：

- 每项业务事实指定唯一权威来源；其他记录只保存稳定引用或摘要，不复制可独立漂移的状态；
- 新增命令 profile 或工具不得新增顶层 artifact 体系，必须复用统一的命令结果、验证记录和诊断存储；
- 成功路径只长期保留产品所需摘要和最终结果；完整日志有大小上限，临时目录和已失去恢复价值的成功备份可清理；
- 失败、未完成事务和 `recovery_required` 可以保留更完整诊断证据，并继续 fail closed；
- 模型上下文和普通 CLI 默认消费摘要，不要求用户理解底层目录、journal、receipt 或 manifest；
- `Candidate` 的冻结 patch/hash/并发复检可以作为内部采纳安全机制保留，但用户概念统一为“当前修改”；
- retention、容量上限和清理策略必须集中定义，不能散落在各工具实现中。

### 非目标

本决策不要求保留所有原型 artifact，也不要求兼容旧 Session 目录。它同样不把持久化扩展为数据库、通用事件溯源系统或跨主机 durable transaction framework。

## D-2026-08-27-05：accept 保留 Session worktree，并以 Git checkpoint 表达 accepted baseline

状态：Accepted  
范围：当前修改、accept/discard、source 并发修改和多轮 Session

### 决策

- Session 首次需要受保护能力时创建一个专属 worktree；一次 accept 成功后不删除该 worktree，也不回到 source-only。
- worktree HEAD 是最近一次成功采纳的 accepted baseline；working tree 是 baseline 加当前尚未采纳的修改。
- accept 成功后，Runtime 只在 detached Agent worktree 内创建 checkpoint commit 并 reset 到该 commit。它不修改用户 source branch、不 push，也不作为用户概念展示。
- 当前修改以 `git diff HEAD` 按需计算，不持续维护另一份 `current.patch`。
- source 在首次 upgrade 时仍要求 clean；之后允许包含已采纳但未提交的内容及用户并发修改。
- accept 使用 Git 三方合并语义：accepted baseline、Agent working tree 和当前 source snapshot。无冲突时生成“当前 source → merged result”的固定 patch，展示后经用户批准应用完全相同 bytes；真实冲突时不修改 source 并保留 worktree。
- source 在 patch 冻结后或用户批准期间再次变化时，旧 frozen patch 失效并拒绝应用。
- discard 只清除 accepted baseline 之后的 pending changes，首版继续保留同一 worktree。

### 持久化与安全

accepted baseline 使用 Git commit/tree identity，不复制完整文件树或建立 baseline artifact hierarchy。正常冻结 patch 只存在于 accept 窗口，成功、拒绝或失败退出后清理；冲突详情进入有界错误/事件，Runtime 不建立冲突 artifact 系统。

Agent 不获得 Git 写工具。checkpoint、临时 index/tree/commit 和 merge-tree 都是 Runtime 固定 argv 的内部操作；PathGuard、Sensitive Path、Atomic Edit、Policy、Approval 和最终固定 patch 复检继续有效。

## D-2026-08-27-06：受限 pytest node id 与最小 workspace_tainted

状态：Superseded in part by D-2026-08-28-01  
范围：Command Profiles / Validation

- `pytest.targeted` 支持受限 `workspace-relative-file.py::node` 形式。PathGuard 检查文件部分，node segment 不得包含空白、控制字符或 flag-like 前缀；Agent 仍不能传 pytest flags。
- 验证命令产生已检测但未经 Atomic Edit 管理的 workspace 副作用时，Session 进入最小 `workspace_tainted`。
- Tainted 时禁止 edit、validation 和 accept，只允许只读检查和 `discard-changes`。
- Discard 必须以 HEAD、Git status 和完整 Git tree 证明 worktree 已恢复 accepted baseline；任一复检失败都进入 `recovery_required`。
- `workspace_tainted` 不引入修复向导、部分采纳或复杂状态机；它仅用于区分“已知副作用”与“无法证明状态”。

## D-2026-08-28-02：任意命令实现确认与持久化边界

状态：Accepted and Implemented
范围：命令执行、权限、验证、当前修改和恢复

- Agent-facing 执行工具统一为 `run_command`；Command Profile、`run_validation`、pytest allowlist 和裸本地 executor 已删除。
- 任意命令必须经过资源 Policy、必要审批、Effective Sandbox Policy 和 per-command Bubblewrap；不可用时 fail closed。
- Agent 显式申请文件写入或网络能力；Runtime 不分析 shell、不解析 stderr 生成权限，也不自动重放命令。
- Atomic Edit 与 sandboxed command 都可产生合法当前修改，并统一推进 `candidate_revision`。
- `workspace_tainted` 不再表示“非 Atomic Edit 修改”，只表示命令可能修改了 Candidate、但 after audit 无法建立可信边界；进程树或保护边界无法确认时进入更高优先级 `recovery_required`。
- validation evidence 只证明所选命令针对当前 Candidate 和 Policy 成功执行；accept 要求证据存在，不保证验证质量。
- 正常命令只长期保存紧凑事件摘要；完整输出和 runtime 目录仅在失败、超时或异常状态下作为有界 diagnostics 保留。

本决策取代 D-2026-08-27-06 中“命令副作用自动 taint”和 pytest Profile 相关执行语义；其 discard/recovery fail-closed 原则继续有效。

## D-2026-08-29-01：实时工具进度与一次性工作区升级确认

状态：Accepted and Implemented  
范围：交互 CLI、AgentRunner、首次 source-only → Agent worktree 升级

- 交互 CLI 必须在每个工具完成后立即显示工具名、参数摘要和结果，不等待整个模型轮次结束；读取内容仍保留在模型上下文和 Session 事件中，默认不把完整文件内容刷到终端。
- 模型在工具调用响应中附带的说明文字必须保留、写入工具调用事件并在执行工具前展示。
- 首次受保护操作的确认面向用户表达为“创建 Session 专属隔离工作区”，并展示触发它的命令或文件操作摘要；不直接以 `CANDIDATE_WRITE` 等内部权限枚举代替操作说明。
- 同一用户消息形成的模型轮次内，工作区升级最多询问一次。用户拒绝后，剩余受保护调用返回本轮未授权，但不得再次弹窗；下一条用户消息可以重新申请。
- 只读工具继续无需批准。worktree 建立后的额外宿主文件或网络能力仍使用独立、资源明确的 Permission Request。

## D-2026-08-29-02：对话轮次与执行事件分离

状态：Accepted and Implemented  
范围：模型上下文、Resume 摘要、验证有效性

- `events.jsonl` 继续保存审计与恢复所需事件，但模型上下文不得按最后若干原始事件直接截断。
- 上下文以 `user_message` 划分对话轮次：最近用户原话优先保留；已结束旧轮次省略重复工具生命周期，当前轮次保留完整 tool-call/tool-result 配对。
- Runtime 不建立“当前目标/仍有效约束”的权威语义对象，也不新增对应 artifact。
- Resume 的当前修改来自 active worktree 的 Git 状态；验证是否对应当前内容以 workspace tree identity 判断，不评价验证质量。
- 没有改变 workspace 内容的命令不推进 Candidate revision，也不使已有验证证据失效。

## D-2026-08-29-03：交互输入以编辑缓冲区提交完整消息

状态：Accepted and Implemented  
范围：交互 CLI 输入

- TTY 交互使用支持 bracketed paste 的 `PromptSession`：一次多行粘贴进入同一可编辑缓冲区，粘贴后可继续输入，只有用户随后按 Enter 才形成单个 `user_message`。
- 粘贴中的换行和代码块原样保留；提交后缓冲区清空，残留文本不得被后续审批读取。
- 不新增 `/paste` 产品接口。非 TTY 输入继续使用简单逐行路径，保持脚本和 CLI 测试兼容。

## D-2026-08-29-04：执行切片不等于用户对话轮次

状态：Accepted and Implemented
范围：AgentRunner、交互 CLI、Resume、benchmark/API

- `max_steps_per_turn` 作为单个内部执行切片的控制点，默认 12 个 ModelStep；切片耗尽不结束 UserTurn。
- 同一 UserTurn 默认最多 48 个 ModelStep。达到总预算才以明确的未完成状态终止，不写入伪成功的 Assistant Final。
- 交互 CLI 在切片边界询问是否继续；拒绝只暂停，之后可用 `/continue` 或 Resume 继续，不创建内容为“继续”的 UserMessage。
- 非交互 `ask` 在总预算内自动继续，便于 benchmark 完成较长任务。
- 执行切片只用已有 Event 记录一个紧凑控制事实，不建立 Slice 数据结构、目录或 artifact 家族。
- Context 始终保留该 UserTurn 的原始 UserMessage 和完整工具协议对；旧工具调用前说明可按确定性窗口省略，以限制长轮次增长。

## D-2026-08-30-01：Agent 提示聚焦任务行为，Read 结果明确范围边界

状态：Accepted and Implemented
范围：System Prompt、`read_file`、Context residue

- Agent 只需要知道完成任务所需的操作约束，不要求理解 Runtime、Policy、ToolRegistry 或 Candidate 等内部模块名词。
- “workspace 操作必须通过已注册工具”不等于“每个响应都必须调用工具”；信息充分时允许分析、编辑、验证或直接给出最终回复。
- Agent 应进行最小充分探索，避免重复读取同一 SHA 下已经覆盖的范围；信息不足时先说明具体缺口。
- `read_file` 始终区分 requested range、returned range 和 file total lines，并明确前后是否还有内容；部分读取不再用 `total_lines=null` 表示“尚未扫描到 EOF”。
- 这些字段属于工具结果和确定性历史 residue，不新增阅读进度 artifact 或语义任务状态。

## D-2026-08-30-02：Search 解释模式显式化，Context 容量失败正常终止当前轮次

状态：Accepted and Implemented
范围：`search_text`、AgentRunner、CLI、Resume

- `search_text` 默认 `literal`，schema 明确正则语法只有在 `mode=regex` 时生效；结果回显实际 `query_mode` 和解释方式。
- Context 最低集合超出单次模型输入预算时不再由未捕获异常退出。Runner 写入 `turn_terminated(reason=context_budget_exceeded)`，包含 estimated/usable tokens 和已采用的 reductions。
- 该状态只终止当前未完成 UserTurn，不删除 Session/workspace，也不谎称任务完成；CLI 明确区分 Context 输入容量与 Session 累计额度。
- 如何压缩 active UserTurn 内较旧、已闭合的工具交换仍需独立 Technical Design；本决策不提前引入语义摘要或新的持久化 artifact。

## D-2026-08-30-03：Working Context 优先于 SWE-bench adapter

状态：Accepted
范围：下一阶段顺序、Context、Agent orchestration

- 五次真实 HTTPX Session 已证明当前 Runtime 工具能执行，但 Agent 在长工具轨迹中无法维持稳定源码工作集和执行阶段；这不是单纯提高 step/token budget 可以解决的问题。
- 下一阶段先完成 Agent Orchestration / Working Context Technical Design 与实现验收，再进入 SWE-bench 薄 adapter。
- 设计必须把完整持久 Event、历史执行摘要、当前代码 Working Set 和短期 Execution Checkpoint 分开；不得继续让“最新工具结果”同时承担所有职责。
- 当前不决定引入 LangGraph、LLM semantic compactor 或新的 Task/artifact 体系；框架选择必须晚于状态语义。
- 真实模型用于人工验收和发现编排问题；自动化正确性仍由 Fake Model、fixture、Runtime/Git 状态和 pytest 证明。
