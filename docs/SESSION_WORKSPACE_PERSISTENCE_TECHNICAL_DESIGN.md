# Session、Workspace 与持久化 Technical Design

状态：设计 review 与实现已于 2026-08-27 完成。  
适用范围：下一轮 Session lifecycle、按需 worktree、动态工具能力和 artifact 收敛重构。  
产品决策见 [DECISIONS.md](DECISIONS.md) 的 D-2026-08-27-03 与 D-2026-08-27-04；当前实现事实仍以 [ARCHITECTURE.md](ARCHITECTURE.md) 为准。

> 后续迁移说明：本文的 `edit_revision` 已统一为 `candidate_revision`。Atomic Edit 与可信完成 workspace audit 的 sandboxed command 都推进该 revision；命令变化是合法当前修改，不再因为“非 Atomic Edit 产生”而自动 taint。当前命令状态语义以 [Controlled Arbitrary Command + Sandbox TD](CONTROLLED_ARBITRARY_COMMAND_SANDBOX_TECHNICAL_DESIGN.md) 为准。

## 1. 目标

本设计把当前“创建时选择 readonly/execution mode，并立即绑定固定工具集合”的原型调整为一个连续 Session 内的自然工作流：

```text
创建 Session 并读取 source
→ 首次需要受保护能力
→ 审批并按需创建 Agent worktree
→ 在同一 active workspace 中读取、编辑和验证
→ 查看当前修改
→ 采纳或丢弃当前 pending changes
→ 保留同一 Agent worktree，继续对话和下一轮修改
```

同时收敛持久化模型，使用户只需理解会话、当前修改、验证和采纳/丢弃；事务、备份、完整命令日志等保留为有边界的内部恢复或诊断材料。

## 2. 非目标

- 不建立 Task identity、Task 数据库、Task 状态机或 Session/Task 层级；
- 不支持同一 Session 同时维护多个 active worktree 或多组待处理修改；
- 不实现多 Agent、并行编辑、跨进程 workspace lock 或通用文件系统事务；
- 不在本轮实现 Command Profiles、完整 Validation policy、SWE-bench adapter 或 host sandbox；
- 不开放任意 shell、任意 argv、Git 写工具或直接 Agent 写 source；
- 不兼容或迁移当前原型 Session metadata/artifact 格式；
- 不把所有内部数据压缩为单个文件，也不牺牲 Atomic Edit 的 rollback 与 `recovery_required` 安全边界。

## 3. 用户概念与内部概念

### 3.1 用户可见概念

| 概念 | 含义 |
| --- | --- |
| Session（会话） | 针对一个项目的连续交流；采纳或丢弃修改后仍可继续 |
| 当前修改 | Agent 当前隔离工作区中尚未进入 source 的文件变化 |
| 验证结果 | 针对当前修改运行的受控检查及其结果 |
| 采纳 | 复检后把已审查的当前修改应用到 source |
| 丢弃 | 不修改 source，结束并清理当前修改 |

CLI 和普通错误信息不要求用户理解 Task、Candidate、journal、receipt、manifest 或 artifact。确需提到底层数据时，必须说明它用于恢复还是诊断。

### 3.2 内部概念

- `source_workspace`：Session 创建时绑定的真实 Git workspace，生命周期内不改变；
- `active_workspace`：当前 Agent 实际读取和操作的位置；无 worktree 时等于 source，有当前修改时指向受管 worktree；
- `workspace_revision`：同一 Session 每次成功创建 worktree 后递增，用于隔离多轮修改的 identity；
- `candidate_revision`：Atomic Edit 或已启动并完成 audit 的 command 的统一递增编号；
- frozen patch：采纳前固定并校验的 diff；它是内部安全机制，不建立独立用户心智模型；
- diagnostic data：失败恢复、审计或开发调试所需的详细数据。

## 4. Session 状态模型

### 4.1 状态

| 状态 | active workspace | 能力 |
| --- | --- | --- |
| `source_only` | source | 只读、搜索和 Git 只读 |
| `preparing_workspace` | source | 不开放写入/命令；正在创建并验证 worktree |
| `changes_active` | worktree | HEAD 为 accepted baseline；working tree 为当前 pending changes |
| `accepting` | worktree | 暂停新的 mutation；复检并应用固定修改 |
| `discarding` | worktree | 暂停新的 mutation；清理当前 worktree |
| `recovery_required` | 保留现场 | fail closed，只允许诊断型读取和创建新 Session 的提示 |

`accepted` 和 `discarded` 是事件结果，不作为 Session 的驻留状态。处理 pending changes 后 Session 回到 `changes_active`；accept 不结束 worktree 生命周期。

### 4.2 正常转换

```text
create → source_only
source_only → preparing_workspace → changes_active
changes_active → accepting → changes_active
changes_active → discarding → changes_active
source_only → closed
```

首次受保护操作被拒绝、worktree 创建失败或初始化复检失败时，`preparing_workspace` 必须回到 `source_only`，并清理已经创建且可以安全确认的中间路径。

任何 mutation/rollback/resume 无法确认 workspace identity 时进入 `recovery_required`。该状态优先于 Git clean、managed changes 和 registry 重建。

### 4.3 一次只允许一个当前修改

一个 Session 同时最多存在一个 active worktree。处于 `changes_active` 时再次请求编辑、命令、采纳或丢弃都复用该 worktree 和 `workspace_revision`。每次成功 accept 只用内部 Git checkpoint 前移 accepted baseline。

## 5. 按需创建 worktree

### 5.1 触发

只读工具永不触发 worktree。以下请求在 `source_only` 状态会触发 workspace capability upgrade：

- Agent 请求 `apply_workspace_edit`；
- Agent 请求一个只允许在隔离 workspace 中运行的受控命令；
- 用户通过 CLI 显式要求开始修改，但仍必须走同一 Policy/Approval/创建流程。

触发请求不是自动授权。Runtime 先把将获得的能力、workspace 隔离方式和命令风险交给 Policy/Approval；拒绝后原工具调用返回明确错误，Session 继续保持只读。

### 5.2 创建事务

workspace upgrade 必须是可恢复的 Runtime 操作：

1. 确认 Session 为 `source_only`，source 是 clean Git workspace，且不存在遗留 active worktree；
2. 持久化 `preparing_workspace` 和目标 revision；
3. 创建受管 detached Git worktree；
4. 校验 source identity、base commit、实际路径和 worktree clean 状态；
5. 原子更新 Session 当前 workspace identity；
6. 重建所有依赖 workspace 的 Runtime 组件；
7. 只有组件全部构造成功后进入 `changes_active` 并重新执行原受保护请求。

任一步失败都不得让 metadata 指向未验证 worktree，也不得注册写入或命令工具。

### 5.3 组件重建边界

以下对象不得跨 workspace transition 继续复用：

- `WorkspaceContext` 与 `PathGuard`；
- 文件读取、repository search 和 Git read service；
- Atomic Edit、命令执行、当前修改/采纳 service；
- `ToolRegistry` 中绑定路径或 capability 的 handler；
- Approval/Policy 中缓存的 workspace identity。

Model client、SessionStore 和与 workspace 无关的对话上下文可以复用。下一次 model request 必须收到重建后的工具 schema；不得在同一次工具 handler 内静默改变路径后继续调用旧 handler。

## 6. 能力与审批

worktree 是安全前置条件，不是全局 execution 权限。

| 能力 | `source_only` | `changes_active` |
| --- | --- | --- |
| 文件/搜索/Git 只读 | source，允许 | worktree，允许 |
| 结构化 Atomic Edit | 触发 upgrade | 按写工具 Policy 执行 |
| 可信命令 profile | 触发 upgrade | 按 profile/Policy 审批 |
| 任意 shell/argv | 拒绝 | 拒绝 |
| 修改 source | 拒绝 | 仅用户采纳流程可执行 |

ToolRegistry 在 `source_only` 时仍向模型提供受保护工具的稳定 schema，但 handler 只通过最小的 workspace lifecycle 协调逻辑执行 Approval 和 upgrade；它不是通用 Capability Dispatcher/Router。升级成功后构造新的 Runtime bundle，并把原始结构化参数仅一次转交给新 workspace-bound handler。审批拒绝或升级失败时返回稳定错误，不自动重试。

这样模型不需要先知道一个额外的“创建 worktree”工具，工具名称和参数在状态转换前后也保持一致。协调逻辑不接受 Agent 提供 workspace path、executable 或任意 argv，不能绕过原工具的 schema、Policy 和 Approval。升级后的下一次 model request 必须使用新 registry；旧 handler 和旧 workspace identity 立即失效。

## 7. 当前修改、验证与结束一轮修改

### 7.1 当前修改

当前修改的事实来源是 active worktree 相对其 `base_commit` 的 Git 状态和 diff。Atomic Edit journal 用于证明修改归属、并发前置条件和恢复，不再作为另一份用户可见文件清单。

每次成功编辑和每次已启动并完成可信 audit 的命令增加 `candidate_revision`。两类变化都统一进入可采纳的当前修改；只有 after boundary 无法确认才 taint。

### 7.2 验证

本轮只保留接口边界，完整规则由后续 Command Profiles / Validation Technical Design 冻结。每条验证结果至少绑定：

- Session id；
- `workspace_revision`；
- `base_commit`；
- 执行结束后的 `candidate_revision`；
- profile id/revision；
- 结果分类和简短输出摘要。

编辑或任意后续命令发生后，旧 `candidate_revision` 的成功验证不得被展示为当前修改仍已验证。

### 7.3 采纳

采纳流程保留当前 Candidate 机制中的关键安全性质，但对用户显示为“采纳当前修改”：

1. 暂停新的编辑和命令；
2. 以 worktree HEAD 为 accepted baseline，分别生成 Agent working tree 和当前 source 的临时 Git tree；
3. 使用 Git merge-tree 做三方合并；真实冲突时停止且不修改 source；
4. 冻结“当前 source → merged tree”的最终 patch 并计算 hash；
5. 展示实际将进入 source 的固定 diff，用户明确确认；
6. 再次复检 source snapshot，应用同一份固定 patch；
7. 在 detached Agent worktree 内为 merged tree 创建内部 checkpoint，并同步 HEAD/working tree；
8. 清理临时 frozen patch，Session 回到 `changes_active`。

source 可以包含用户并发修改，不能只因文件同时变化或 source dirty 就拒绝；冲突由 Git 基于共同 baseline 判断。失败时 source 不得出现部分修改。若 active worktree 仍可确认，Session 返回 `changes_active` 允许用户检查或重试；无法确认时进入 `recovery_required`。

### 7.4 丢弃

丢弃不生成或应用 source patch。Runtime 把 Agent worktree reset/clean 到 accepted baseline，记录丢弃摘要并回到 `changes_active`；source 和 worktree 都继续存在。如果无法安全恢复，不得伪造成功；保留现场并给出可行动提示。

## 8. 持久化模型

### 8.1 目录职责

目标目录只表达生命周期，不按每种工具无限分叉：

```text
<session>/
├── session.json                 # 当前 Session identity 与状态
├── events.jsonl                 # 消息、审批、关键状态转换和统一验证摘要
├── current/                     # 仅当前 workspace revision
│   ├── changes.json             # 修改摘要、revision 与身份引用
│   └── frozen.patch             # 仅 accepting 窗口临时存在
└── diagnostics/                 # 内部、有限保留
    ├── commands/<run-id>/
    └── transactions/<transaction-id>/
```

具体文件是否合并可以在实现中局部调整，但不得改变四项职责：Session 当前状态、append-only 关键历史、当前结果、可清理诊断数据。

### 8.2 唯一事实来源

| 事实 | 权威来源 |
| --- | --- |
| Session/source/active workspace identity | `session.json` |
| 消息、审批、状态转换和最终处理历史 | `events.jsonl` |
| 当前文件变化 | Git worktree HEAD 与 working tree diff；固定 patch 仅用于一次 accept |
| edit revision 与受管修改 identity | Atomic Edit committed record 的摘要索引 |
| 验证状态 | `events.jsonl` 中匹配当前 identity 的紧凑验证记录 |
| rollback/recovery 细节 | transaction diagnostics |
| 完整命令输出 | command diagnostics；不是验证状态本身 |

`transcript.md` 不再作为与 events 并行增长的权威记录。若保留人类可读 transcript，应在展示或导出时从 events 生成。

### 8.3 保存与清理原则

- JSON/日志使用 schema version、稳定相对引用和原子替换；不把 host absolute path返回给模型；
- 命令 stdout/stderr 有固定 byte 上限，ToolResult 和事件只保存摘要；
- command-local HOME/TMP 在进程和 workspace audit 完成后删除，除非失败诊断明确需要保留；
- committed Atomic Edit 在事件/摘要可靠写入后删除 rollback backup；
- rolled-back 成功的事务只保留错误摘要，详细 plan/backup 可立即清理；
- `prepared`、`committing`、rollback failure 和 `recovery_required` 保留恢复所需 plan、状态和 backup；
- 采纳、拒绝或冲突退出 accept 后清理临时 frozen patch，最终处理摘要进入事件历史；不默认永久复制 patch；
- retention、总容量、失败诊断期限和显式清理入口由一个集中 policy 定义，后续 profile 只能调用该 policy。

## 9. Resume 与 fail-closed

resume 顺序固定为：

1. 读取并校验 Session schema；
2. 检查未完成 workspace transition；
3. 检查 Atomic Edit 未完成事务和 persisted `recovery_required`；
4. 验证 source/worktree 路径、Git identity、base commit 和受管范围；
5. 根据当前状态重建完整 Runtime bundle；
6. 最后才对 Agent 开放对应工具。

`recovery_required` 检查始终早于 Git clean/dirty 和普通可恢复修改判断。任何缺失、损坏或互相矛盾的 identity 都不得通过“回退到 source”静默继续同一 Session；用户可以保留现场并新建 Session。

## 10. 代码改造边界

预计改动集中在：

- `session/store.py`：新 schema、状态转换和事件职责；
- `workspace/workspace.py`、`workspace/git_worktree.py`：revision、按需创建、采纳/丢弃清理；
- `cli.py`：移除 mode，统一 start/resume/current changes/accept/discard；
- `runtime/runner.py`：Runtime bundle 重建和受保护调用恢复；
- `tools/registry.py`：根据 Session 状态生成 registry；
- `runtime/atomic_edit.py`、`artifacts.py`、`command_service.py`、`candidate.py`：统一结果/诊断存储与用户术语；
- `context/builder.py`：只消费会话事件和结果摘要，不依赖完整诊断文件。

允许删除仅服务旧 Session 格式的兼容路径和旧单文件 edit journal 读取逻辑，但不得绕过 PathGuard、Sensitive Path、Policy、Approval、ToolRegistry、Atomic Edit rollback 或固定 patch 复检。

## 11. 测试与验收

### 11.1 状态与能力

- 新 Session 不创建 worktree，只注册 source 只读能力；
- 首次编辑和首次受控命令能分别触发同一 upgrade 流程；
- 审批拒绝、source dirty、worktree 创建失败和组件构造失败保持或恢复 `source_only`；
- upgrade 成功后所有读取和 mutation 都指向 worktree，旧 source-bound handler 不再可用；
- worktree 存在不允许任意 shell，也不跳过命令审批；
- `recovery_required` 在表面 clean 时仍关闭 mutation/validation。

### 11.2 多轮 Session

- 编辑、验证、采纳后 source 获得固定修改，Session/worktree 保持 `changes_active`；
- 丢弃后 source 不变，pending changes 清零且 worktree 保留；
- 同一 Session 连续 accept 复用 worktree，checkpoint 前移且 pending diff 只包含新修改；
- source 不同文件、同文件非冲突区域可三方合并；真实冲突和 review 期间再次变化都拒绝旧 patch；
- resume 在 `source_only`、`changes_active` 和 `recovery_required` 均恢复正确 registry；
- 采纳期间 source 并发变化、artifact/hash 篡改和 apply failure 保持 fail closed/无部分修改。

### 11.3 持久化

- 同一验证事实只有一个权威记录，事件和 CLI 只引用/摘要；
- 成功命令与事务不会无限保留 runtime 临时目录和 rollback backup；
- stdout/stderr 超限被确定性截断；
- 失败和 recovery transaction 保留恢复所需材料；
- 清理当前修改不删除 Session 历史，删除诊断数据不改变业务状态；
- 不接入真实 LLM；使用 Fake Model 验证正式 ToolRegistry/AgentRunner/CLI 调用链。

完成标准包括新增测试、全量 pytest、compileall、`git diff --check` 和正式 CLI dogfood。工具接入正确性由 schema、执行、错误、审批、安全和生命周期证据判断，不以模型是否聪明选择工具为门槛。

## 12. 实现顺序

1. 冻结 Session schema、状态转换、capability upgrade 交互和诊断 retention 默认值；
2. 实现按需 worktree 与 Runtime bundle 重建，保持现有工具行为；
3. 迁移 Atomic Edit、命令和当前修改流程到统一存储职责；
4. 将 Candidate CLI 术语替换为 current changes / accept / discard；
5. 补 resume、多轮 Session、清理和失败恢复测试；
6. 更新当前架构、测试、手工 dogfood 和 README；
7. 重构验收完成后再进入 Command Profiles / Validation 设计与实现。
