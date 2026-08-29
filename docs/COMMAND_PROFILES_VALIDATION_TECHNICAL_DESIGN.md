# Command Profiles / Validation Technical Design

> 历史定位：本文记录已被取代的 pytest Profile 阶段。当前执行层已经切换到 [Controlled Arbitrary Command + Sandbox](CONTROLLED_ARBITRARY_COMMAND_SANDBOX_TECHNICAL_DESIGN.md)，Command Profile、`run_validation` 和 pytest-only executor 已删除。本文不得作为当前接口或状态语义的依据。

状态：Implemented  
日期：2026-08-27  
适用范围：Stage 2 受控命令与验证能力，Linux/WSL；为未来 SWE-bench 薄适配保留最小注入边界。

## 1. 目标

本阶段把当前只支持 pytest 的 `run_check` 原型整理为可扩展但仍封闭的验证系统：Runtime 选择可信命令定义，Agent 只能填写明确的结构化参数；每次运行产生绑定当前修改版本的验证结果，采纳流程不再通过“最近一次编辑之后是否出现过 pytest 成功事件”进行模糊判断。

本设计需要保证：

- 工具 schema 清晰，Agent 不能提交 shell、可执行文件或任意 argv；
- pytest、lint、typecheck、build 等能力复用统一执行与结果协议；
- 命令定义、实际执行请求、审批展示和最终执行 argv 完全一致；
- 验证结果绑定实际接受检查的 workspace 内容，编辑后旧结果自动失效；
- 成功路径只持久化小型摘要，失败和副作用才保留有界诊断材料；
- Local POSIX executor 不被过度抽象，同时允许未来 SWE-bench adapter 注入已经准备好的 workspace、解释器和可信 profile；
- 测试证明工具接入、执行、错误、审批和生命周期正确，不依赖真实 LLM 的工具选择能力。

## 2. 非目标

本阶段不实现：

- 任意 shell、Agent 自选 executable、任意 flags 或命令拼接；
- 从被测仓库自动信任命令配置、`package.json` script、Makefile target 或 CI workflow；
- 自动安装依赖、联网下载工具或环境修复；
- Docker 生命周期、远程 executor、host sandbox、网络/CPU/内存/syscall 隔离；
- 通用 backend capability negotiation 或插件框架；
- 自动猜测每个仓库必须通过哪些检查；
- 根据验证结果判断模型是否“聪明”或任务语义是否完成。

命令仍会执行项目代码。Agent worktree 只隔离 Git 修改，不是针对恶意仓库的主机级 sandbox；普通本地 CLI 必须在审批提示中继续说明这一点。

## 3. 用户与 Agent 模型

用户只需要理解：

- **验证类型**：例如“运行指定 pytest”或“运行完整 pytest”；
- **验证结果**：通过、未通过、超时、无法启动、被拒绝；
- **适用版本**：结果是否仍对应当前修改；
- **诊断信息**：失败时可查看的有界输出。

`profile revision`、execution request、tree identity 等是内部校验字段，不要求用户记忆。

Agent-facing 保留单一工具：

```text
run_validation
```

工具 schema 由当前可信 Profile Registry 生成 `oneOf` 分支。每个分支用固定 `profile_id` 区分，并只暴露该 profile 允许的参数。例如首批：

```json
{
  "profile_id": "pytest.targeted",
  "targets": ["tests/test_api.py"],
  "timeout_seconds": 120
}
```

```json
{
  "profile_id": "pytest.full",
  "timeout_seconds": 300
}
```

约束：

- `profile_id` 必须来自 Runtime 注册的 enum/const；
- `targets` 始终是 active workspace 根目录下的相对路径，不以命令 cwd 为参照；
- Agent 不能传 `cwd`、executable、module、environment、shell 字符串或额外 flags；
- Agent 可以缩短 timeout，但不能超过 profile 的最大值；
- 不允许的字段由 JSON schema 和 Runtime 参数校验双重拒绝；
- 工具结果明确返回 profile、状态、exit code、是否适用于当前修改、输出摘要和诊断是否保留。

不为每个 profile 注册独立工具，避免工具数量和权限逻辑随 profile 增殖。

## 4. Command Profile

Command Profile 是 Runtime/host 信任的不可变命令定义，不是项目文件，也不是 Session artifact。

建议内部类型：

```text
CommandProfile
├── profile_id                 稳定名称，例如 pytest.targeted
├── revision                   定义版本；定义变化时必须变化
├── display_name / description
├── runner                     当前固定为 local_posix
├── executable_ref             可信解释器/工具引用
├── argv_template              Runtime 固定模板
├── parameter_schema           Agent 可填参数及边界
├── cwd                        可信、workspace-relative 固定目录
├── timeout_default / maximum
├── environment_policy         固定的最小环境策略 id/revision
├── approval_policy            当前为 always_ask
├── output_policy              摘要与诊断大小上限
├── side_effect_policy         当前为 detect_and_invalidate
└── evidence_kind              test / lint / typecheck / build / smoke
```

### 4.1 来源与信任

首版 Profile Registry 只接收：

1. Runtime 内置 profile；
2. 由宿主显式注入的可信 profile，例如未来 SWE-bench adapter 在已准备容器中提供的定义。

首版不从 active workspace 自动加载 profile。仓库内配置可以被具体工具读取，但不能决定 Runtime 启动哪个 executable、附加什么 flags 或降低审批级别。未来若允许用户项目定义 profile，必须新增信任/确认设计。

`revision` 是 profile 规范化定义的稳定 SHA-256 或显式版本加定义 hash。仅用 `profile_id` 不足以复用旧验证结果。

### 4.2 executable 解析

Profile 不保存未经校验的自由字符串。首版支持最小的可信引用：

- `runtime_python`：启动当前 Runtime 的 Python，并使用固定 `-m <module>`；
- `absolute_injected`：由受信宿主注入并在注册时解析为绝对普通文件；
- 后续如需 workspace virtualenv，必须作为宿主明确选择的路径注入，不自动执行仓库中的任意 shim。

注册时解析一次 executable；构造执行请求和真正 spawn 前分别复检 identity/可执行性。Agent 参数永远不能替换 argv 第一个元素或模块名。

## 5. 首批 profile 与分阶段范围

### Phase A：基础协议与 pytest

必须实现：

| Profile | 固定 argv 语义 | Agent 参数 |
| --- | --- | --- |
| `pytest.targeted` | `runtime_python -m pytest -p no:cacheprovider -- <targets>` | 一个或多个 workspace-relative target，timeout 可缩短 |
| `pytest.full` | `runtime_python -m pytest -p no:cacheprovider` | timeout 可缩短 |

`--` 用于终止 pytest options，target 不得以 `-` 开头；路径必须经过 PathGuard、存在性与 workspace 边界检查。是否允许 pytest node id（如 `file.py::test_name`）在实现前必须明确，见第 16 节。

### Phase B：静态检查与构建

在 Phase A 协议稳定后逐个加入：

- `ruff.check`；
- `mypy.check` 或 `pyright.check`，只在对应工具由可信宿主解析时注册；
- `python.build`，使用固定 `python -m build --no-isolation`，且仅在依赖已准备时注册；
- 项目特定 CLI smoke 只通过宿主可信注入，不由 Agent 拼 argv。

新增 profile 只增加定义、参数编译器和 profile-specific 测试，不建立新的 service、权限级别、结果文件或诊断目录。

## 6. 从 Agent 参数到不可变执行请求

链路：

```text
run_validation arguments
→ JSON schema validation
→ Profile Registry lookup
→ profile-specific parameter validation / PathGuard
→ executable resolution
→ argv template expansion
→ capture current workspace identity
→ immutable ExecutionRequest
→ Policy
→ Approval
→ Executor
```

建议执行请求：

```text
ExecutionRequest
├── execution_id
├── profile_id / profile_revision
├── workspace_revision / base_commit
├── edit_revision
├── subject_tree                 执行前 active workspace 的 Git tree identity
├── executable                   已解析绝对路径
├── argv                         完整固定 argv
├── cwd                          已解析绝对路径及 workspace-relative 展示值
├── environment_policy_revision
├── timeout_seconds
└── output_policy
```

该对象在 Policy、Approval 和 Executor 之间不可变。审批界面显示 profile、完整 argv、cwd、timeout、active workspace 和“无主机级 sandbox”警告；Executor 必须执行同一个 request，不重新解释 Agent 参数。

## 7. 最小 Execution Boundary

本阶段只抽取 profile 编译与进程执行之间的窄接口：

```text
CommandExecutor.execute(request, workspace_context, diagnostic_paths) -> ExecutionResult
```

Executor 负责：

- 复检 POSIX、active root、cwd containment 和 executable identity；
- 使用 `shell=False`、不可交互 stdin、独立 process group；
- 应用 request 中已经冻结的最小环境策略、timeout 和输出限制；
- 捕获进程状态；
- 执行前后 workspace audit；
- 返回事实，不判断该 profile 是否满足产品验证策略。

Executor 不负责：

- 查找 profile、解释 Agent 参数、决定 approval；
- 安装工具或依赖；
- Docker/远程任务生命周期；
- 判断任务语义完成或允许采纳。

Local POSIX executor 是首个实现。未来 SWE-bench adapter 仍可使用它；adapter 只需注入已经准备好的 active workspace、可执行文件映射和可信 profiles。只有真正出现第二种进程执行机制时，才重新评估更通用 backend interface。

## 8. Policy 与 Approval

“profile 可信”只表示 argv 的来源可信，不表示仓库代码安全。pytest、build 和项目 CLI 都可能执行任意项目代码。

在没有 host sandbox 的当前阶段：

- 所有 validation profile 每次运行均为 `require_approval`；
- source-only Session 的首次验证会触发 worktree upgrade；
- 首次调用应形成一次用户决定：审批内容同时说明将创建 Agent worktree 并运行冻结后的命令，不能为同一调用连续弹出两个实质相同的确认；
- 批准只适用于当前不可变 ExecutionRequest，不缓存为任意后续命令权限；
- 拒绝不得启动进程或留下假验证结果；Session 在 upgrade 尚未发生时保持 source-only；
- `recovery_required` 早于 profile lookup、upgrade、Policy 和 Approval，直接 fail closed。

未来 host sandbox 可以让特定 profile 改为不同审批策略，但必须重新做威胁模型和产品决策。

## 9. Workspace audit 与副作用

验证命令理论上只读，但工具本身或项目代码可能写文件。Executor 在执行前后对 active workspace 做 Git-aware audit，并比较内容 fingerprint，而不只比较 status path。

结果分类：

- **无副作用**：执行前后 subject tree 相同，可成为当前修改的验证证据；
- **检测到副作用**：保留 changed paths、patch 和诊断，结果标记 `side_effect_detected`，不得成为通过证据；
- **audit 失败**：标记 `audit_failed`，不得成为通过证据，并保留诊断；
- command 修改已 dirty 文件后又保持同一 status 行，仍必须由 fingerprint/tree identity 检出。

首版不自动 rollback 命令副作用，因为命令可能改变未跟踪文件、目录和外部状态，无法达到 Atomic Edit 的恢复证明。检测到 workspace side effect 后：

- Session 保持 worktree，但受保护的继续验证和采纳应 fail closed；
- 用户可以查看当前修改和诊断，并选择 `discard-changes` 回到 accepted baseline；
- 已按第 16 节 review 结论引入最小 `workspace_tainted` 状态。

主机外部副作用在没有 sandbox 时无法完整检测；审批提示必须如实说明。

## 10. Validation Record 与当前性

每次批准并进入 Executor 的运行产生一个紧凑 `validation_completed` event。Policy/Approval 拒绝只记录请求与拒绝，不产生验证记录。

建议字段：

```text
validation_id
execution_id
profile_id / profile_revision / evidence_kind
workspace_revision / base_commit / edit_revision
subject_tree
status                         passed / failed / timed_out / spawn_failed /
                               executor_rejected / side_effect_detected / audit_failed
exit_code / duration_ms
started_at / finished_at
output_summary                 有界；失败优先保留尾部
output_truncated
diagnostic_ref                 仅实际保留诊断时存在，使用 session-relative id
```

`passed` 必须同时满足：进程正常完成、exit code 为 0、workspace audit 成功、执行后 tree 与 `subject_tree` 相同。

验证结果“适用于当前修改”必须同时满足：

```text
record.workspace_revision == current.workspace_revision
record.base_commit == current.base_commit
record.edit_revision == current.edit_revision
record.subject_tree == current.subject_tree
record.profile_revision == registry 当前同名 profile revision
record.status == passed
```

任何 Atomic Edit、discard、accept 后 checkpoint 前移、未记录文件变化或 profile 定义升级都会让旧结果失效。旧记录仍是历史，但不能显示为当前修改已通过。

`subject_tree` 使用 Git tree identity 表达完整 active workspace 内容，包含 tracked 与允许交付的 untracked 文件。生成方式应抽成当前修改冻结和验证共享的内部 Git snapshot helper；不得各自实现两套不一致的 untracked/sensitive/symlink 规则。

## 11. 采纳策略

验证事实与“采纳需要哪些验证”分离：

- `ValidationRecord` 只陈述某个 profile 对某版内容的运行事实；
- `ValidationPolicy` 决定 accept 前需要哪些 profile 通过；
- Profile 本身不写 `required=true`，避免命令定义和产品策略耦合。

首版内置策略建议：当前修改至少存在一个仍有效的 `pytest.targeted` 或 `pytest.full` 通过记录。它保持当前 pytest gate 的行为，但把判断从事件顺序升级为确定性 identity 匹配。

未来宿主可显式注入 required profile 集合；SWE-bench adapter 可以要求其任务指定的测试 profile，但不能伪造 passed record。普通仓库的 required set 不从仓库内容自动猜测。

`show-changes` 应展示当前有效验证摘要。`accept-changes` 在冻结 patch 前重新计算 current identity 并执行 ValidationPolicy；用户审批 patch 期间 source 变化仍按现有三方合并复检处理。

## 12. 状态与错误分类

工具层错误码建议：

| error code | 含义 |
| --- | --- |
| `invalid_arguments` | schema、target 或 timeout 不合法 |
| `unknown_profile` | 当前 Registry 未注册该 profile |
| `profile_unavailable` | 可信 profile 存在但 executable/环境不可用 |
| `policy_denied` | Policy 拒绝 |
| `approval_denied` | 用户拒绝 |
| `workspace_upgrade_failed` | 首次 worktree 创建失败 |
| `execution_timed_out` | timeout 后已执行终止流程 |
| `spawn_failed` | 进程无法启动 |
| `executor_rejected` | Executor 防御性复检拒绝 |
| `validation_failed` | 命令正常完成但检查未通过 |
| `workspace_side_effect` | 检测到 workspace 写入 |
| `workspace_audit_failed` | 无法证明执行前后 workspace identity |
| `recovery_required` | Session 已 fail closed |

命令非零不是 Runtime failure。ToolResult 应让 Agent 看见有界 stdout/stderr 和可操作提示；错误不得只返回“command failed”。

## 13. 持久化与容量

权威来源保持最少：

```text
session.json                         Session/workspace 当前状态
events.jsonl                         validation_completed 历史与摘要
diagnostics/commands/<execution-id>/ 仅失败、timeout、副作用、audit/spawn 异常
```

本阶段不新增 `validations.jsonl`。验证摘要已经是 Session event；另建文件会复制 profile/status/revision 并产生同步问题。此前 Session TD 中的该文件改为被本设计取代。

成功且无副作用：

- event 保留紧凑 ValidationRecord；
- 完整 stdout/stderr、request/result、workspace patch 和 runtime HOME/TMP 全部删除；
- event 不保存已删除路径、完整环境或完整 argv 的重复副本；执行身份通过 execution/profile id 和必要摘要表达。

失败、timeout、spawn/audit error 或 workspace side effect：

- 保留统一 command diagnostics；
- stdout/stderr、patch 和 metadata 均有单文件和 Session 总容量上限；
- event 只保存 session-relative `diagnostic_ref`；
- 继续复用集中 cleanup/retention policy，不按 profile 建目录。

## 14. Resume 与上下文

Resume 不恢复运行中的子进程。若 Runtime 中断时只存在 command diagnostic 但没有完成 event：

- 该 execution 记为 `interrupted` 诊断事实；
- 不产生 passed evidence；
- 若 workspace identity 仍可证明且无副作用，可以继续 Session；
- 无法完成 audit 时按 workspace taint/fail-closed 决策处理。

模型上下文只注入最近、相关的验证摘要：profile、状态、exit code、是否仍适用当前修改、简短失败输出。完整日志不进入普通上下文。

验证历史不改变 Session 顶层状态；只有 workspace 无法确认时才进入已有 `recovery_required` 或待决定的 tainted 状态。

## 15. 测试与验收

不接真实 LLM。Fake Model 只驱动正式 ToolRegistry / AgentRunner 调用链。

### 15.1 Profile 与 schema

- `run_validation` 的 `oneOf`/const schema 清晰且无任意 argv 字段；
- unknown profile、额外参数、空 targets、absolute/escape/symlink/sensitive target、flag-like target、timeout 越界拒绝；
- profile revision 对规范化定义稳定，定义变化后 revision 变化；
- executable 不存在、不是普通文件或 spawn 前 identity 改变时 fail closed。

### 15.2 执行与审批

- Agent 参数编译为预期固定 argv，Policy/审批/Executor 接收同一不可变 request；
- 首次验证只出现一次组合审批，创建 worktree 后原调用恰好执行一次；
- 拒绝不创建 worktree、不 spawn、不产生 passed record；
- `shell=False`、stdin、process group、minimal env、HOME/TMP、timeout 与二次终止；
- completed nonzero、timeout、spawn failure、executor rejection 分类清晰；
- stdout/stderr 摘要和错误提示完整、有界。

### 15.3 验证 identity

- passed record 绑定 workspace revision、base commit、edit revision、subject tree 和 profile revision；
- 新编辑、手工 dirty、accept checkpoint、discard 或 profile revision 变化后旧证据失效；
- 同一内容重复验证可产生历史记录，但当前状态只需投影最新有效结果；
- targeted/full pytest 均能满足首版策略；failed/timeout 不满足；
- current changes 展示有效/过期验证，accept 只消费当前有效证据。

### 15.4 副作用与持久化

- 新增、修改、删除及“已 dirty 文件内容变化但 status 行不变”均可检测；
- side effect/audit failure 不产生 passed evidence并保留诊断；
- clean success 删除 command diagnostics 与 runtime HOME/TMP，event 无 stdout、环境清单和失效路径；
- failure/timeout/side effect 有界保留，cleanup 遵守集中 policy；
- 中断 resume 不误判为 passed，无法确认 workspace 时 fail closed。

### 15.5 完整链路与 dogfood

- Fake Model：read → edit → targeted validation failed → edit → targeted/full validation passed；
- source-only：run validation → approval → lazy worktree → execute → tool result；
- 正式 CLI：两种 pytest profile、失败诊断、编辑后证据失效、show/accept；
- 全量 pytest、compileall、`git diff --check`；
- 不以真实模型工具选择或任务解决率作为完成条件。

## 16. Review 结论

### Q1：pytest node id

决策：按推荐方案支持受限 node id。只允许 `workspace-relative-path.py` 后接一个或多个不含控制字符、空白或 `-` 前缀的 `::segment`；PathGuard 只校验 `::` 前的文件路径，argv 使用完整 node id。

### Q2：命令 workspace side effect

决策：按推荐方案新增最小内部 `workspace_tainted` 状态。它只表示已检测到、但未经 Atomic Edit 管理的 workspace 副作用。禁止 edit/validation/accept，只允许只读检查和 discard。discard 必须验证 workspace 已完整恢复 accepted baseline；无法证明时进入 `recovery_required`。不为 tainted 建立复杂修复工作流。

## 17. 实现顺序

1. Profile/Registry、revision 和动态 `run_validation` schema；
2. 通用不可变 ExecutionRequest/Result 与 Local POSIX executor 迁移；
3. pytest targeted/full、组合 upgrade/command approval；
4. Git subject tree helper、ValidationRecord 和当前性投影；
5. ValidationPolicy、show/accept 集成；
6. side effect/interrupt/resume 与统一 diagnostics/retention；
7. Fake Model、全量测试和正式 CLI dogfood；
8. 协议稳定后再逐项增加 ruff/typecheck/build profile。

上述 1–7 完成前不宣称 Command Profiles / Validation 基础协议已交付。第 8 步的各个扩展 profile 可以分别实现和验收，不阻塞基础协议落地。

## 18. 已知限制

- 本地命令仍能访问当前用户权限允许的主机资源和网络；
- 只保证 Linux/POSIX，未适配 Windows；
- 不自动准备依赖或发现项目验证策略；
- Git workspace audit 不能证明没有 workspace 外部副作用；
- 首版是顺序执行，不支持命令并行、取消 UI 或后台作业；
- build/typecheck/CLI smoke 的可信 executable 与参数需要 Runtime/宿主明确注册；
- SWE-bench adapter 只获得注入边界，本阶段不实现官方 harness 或容器编排。

## 19. 结论

本设计选择：单一结构化 `run_validation`、可信不可变 Command Profile、窄 ExecutionRequest/Result 边界、每次执行审批、Git tree 级验证 identity、独立 ValidationPolicy，以及 events 为唯一验证摘要来源。

它扩展的是 Agent 的受控验证能力，不是任意命令能力；它为 SWE-bench 提供可信 profile/workspace/executable 注入点，但不提前建设 sandbox/backend 框架。第 16 节两项已经 review 确认并实现。
