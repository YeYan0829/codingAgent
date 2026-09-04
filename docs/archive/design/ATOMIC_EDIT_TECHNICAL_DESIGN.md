# Atomic Workspace Edit Technical Design

状态：设计评审与首批实现已于 2026-08-27 完成。
适用范围：Stage 2 文件修改能力，Linux/WSL Session Agent worktree。
设计输入：[Stage 2 Research（历史快照）](../STAGE2_RESEARCH_2026-08.md)。当前关联契约见 [SEARCH_READ_TECHNICAL_DESIGN.md](SEARCH_READ_TECHNICAL_DESIGN.md) 和 [ARCHITECTURE.md](../../ARCHITECTURE.md)。

> 实现状态：Session / Workspace / Persistence 与任意命令沙盒重构已完成。本文早期段落中的 `execution task worktree` 现指 Session 按需创建的 Agent worktree；用户可见 `Candidate` 现统一为“当前修改/采纳或丢弃”。`CandidateService` 仅是采纳前固定 patch 的内部实现名。结构化编辑、expected SHA、原子提交、rollback、固定 patch 复检和 `recovery_required` 安全契约继续有效；成功事务的 journal/backup 已清理。本文旧 `edit_revision` 统一解释为当前实现的 `candidate_revision`，验证与命令语义以任意命令 TD 为准。

## 1. 目标

本设计把现有单文件编辑扩展为受控的多文件原子编辑事务。Agent 可以在一次调用中修改或创建 UTF-8 文本文件、删除文件和移动文件；Runtime 必须保证：

- 请求在 execution Git worktree 中执行，绝不直接修改 source；
- 全部操作先预检，任何已知错误发生时不写入 workspace；
- 提交期间任一文件或 artifact 写入失败时恢复事务前状态；
- 文件在读取与提交之间发生变化时拒绝事务，不覆盖并发修改；
- 成功事务具有单一 revision、journal、事件和可审查 diff；
- resume、validation evidence、Candidate freeze/apply 能正确理解 create/update/delete/move；
- PathGuard、敏感路径、symlink、Policy、ToolRegistry 和 Candidate 边界不被绕过。

“原子”在本设计中指 Runtime 正常运行期间的事务级 all-or-nothing，以及进程异常退出后的可检测、可恢复状态。它不宣称跨主机掉电具有数据库级 durability，也不依赖项目文件系统支持通用多路径原子提交。

## 2. 非目标

- 不开放任意文件写入、shell、Git 写命令或 source 目录直接修改；
- 不编辑 binary、非 UTF-8、symlink、submodule、device 或其他特殊文件；
- 不修改权限位、ownership、xattr、ACL 或 Git index；
- 不支持 copy、hardlink、递归目录删除、跨 workspace move 或 case-only rename；
- 不解析或执行 Agent 提供的任意 unified diff；
- 不解决多个 CodeAgent 进程同时写同一 execution worktree 的分布式锁问题；
- 不在本阶段实现 host sandbox、SWE-bench adapter 或新的 validation policy。

## 3. 关键设计决策

### D1. 一个 Agent-facing 工具，一个事务

新增 `apply_workspace_edit`，一次调用提交一个有序 operations 数组。它是唯一 Agent-facing 编辑工具；旧 `apply_text_patch` 不再注册到 ToolRegistry。Runtime 仍读取已有单文件 edit journal，以保证旧 session 可以 resume/freeze，但不继续暴露旧 schema。

所有写操作继续使用 `PermissionLevel.CANDIDATE_WRITE`。该权限只因为 Runtime 已经绑定独立 execution worktree 而默认 allow；readonly、source workspace、stale 或未知 dirty worktree不注册该工具。

### D2. 结构化操作，不接受原始 patch

Runtime 接收 `create_file`、`replace_text`、`delete_file`、`move_file`。结构化输入可以在执行前验证路径、hash、类型、冲突和最终拓扑；Runtime 根据 before/after bytes 生成 unified diff 供 journal 和用户审查。

### D3. SHA-256 是所有已有文件的强制并发前置条件

`replace_text`、`delete_file` 和 `move_file` 必须提供 `expected_sha256`，值来自 `read_file.metadata.sha256`。因此 Atomic Edit 实现时必须给 `read_file` 增加完整文件 hash；即使返回内容被分段或截断，hash 仍代表读取时的完整 bytes。

文本上下文仍是语义定位条件，但不是并发控制的替代品。`replace_text` 同时要求：

- 当前 bytes 的 SHA-256 等于 `expected_sha256`；
- `old_text` 经统一行尾适配后恰好匹配一次；
- replacement 产生实际变化。

任何 hash 不匹配返回 `edit_conflict`，不自动重读、merge 或重试。

### D4. 父目录由 Runtime 规划

Agent 不提交 mkdir operation。`create_file` 和 `move_file` 的目标父目录不存在时，Runtime 在 prepare 阶段计算完整目录链，逐层执行 PathGuard、sensitive、symlink、已有文件和 operation conflict 检查。通过后，这些目录作为 transaction internal side effect 创建、记录并在失败时从深到浅回滚。

空目录不会成为 Agent edit，也不会进入 Git Candidate；只有为本事务最终文件服务的缺失父目录才允许创建。

### D5. move 是一等操作

move 不降级为两个互不关联的 delete/create event。Runtime 直接移动 bytes，并在 journal 中保留 source/destination identity。Candidate patch 仍由 Git 从 base commit 与最终 workspace 生成；Git 是否展示 rename 只影响 patch 表示，不改变事务事实。

### D6. 单进程事务锁与提交前复检

编辑服务使用进程内互斥锁，并以唯一 transaction artifact 目录记录状态，防止当前 Runtime 正常路径重入。当前没有独立 active marker，也不宣称跨进程锁；提交前重新读取所有输入路径并复检 type、symlink 和 hash，因此多数外部并发修改会转化为 `edit_conflict` 或 `path_rejected`。

## 4. Agent-facing schema

工具名称：`apply_workspace_edit`。

顶层 schema 必须设置 `additionalProperties: false`：

```json
{
  "operations": [
    {
      "op": "replace_text",
      "path": "src/module.py",
      "expected_sha256": "64 lowercase hex chars",
      "old_text": "return old_value",
      "new_text": "return new_value"
    }
  ]
}
```

`operations` 必填，数量 1..50。使用 JSON Schema `oneOf` 区分以下四种 operation；每个分支均设置 `additionalProperties: false`。

### 4.1 `create_file`

```json
{
  "op": "create_file",
  "path": "src/new_module.py",
  "content": "UTF-8 text\n"
}
```

- path 在事务初始状态必须不存在；
- 缺失 parent 由 Runtime 在 prepare 阶段规划；
- content 可以为空；
- 不允许覆盖已有文件、目录或 symlink。

### 4.2 `replace_text`

```json
{
  "op": "replace_text",
  "path": "src/module.py",
  "expected_sha256": "...",
  "old_text": "unique context",
  "new_text": "replacement"
}
```

- path 在该 operation 执行前的计划状态中必须是普通 UTF-8 文件；
- `old_text` 必须非空且唯一匹配；`new_text` 可以为空；
- 同一文件允许多个按数组顺序执行的 replacement；每个 replacement 的 `expected_sha256` 都指事务开始时该文件的 bytes，而不是前一个 operation 后的 hash；
- mixed-newline 文件不做隐式适配；统一 CRLF/LF/CR 文件沿用现有适配规则。

### 4.3 `delete_file`

```json
{
  "op": "delete_file",
  "path": "src/obsolete.py",
  "expected_sha256": "..."
}
```

- 只删除普通 UTF-8 文本文件；
- 不删除目录、symlink、binary、submodule 或特殊文件；
- 删除不存在的路径是冲突，不是幂等成功。

### 4.4 `move_file`

```json
{
  "op": "move_file",
  "source": "src/old_name.py",
  "destination": "src/new_name.py",
  "expected_sha256": "..."
}
```

- source 必须是普通 UTF-8 文本文件且 hash 匹配；
- destination 必须不存在；缺失 parent 由 Runtime 在 prepare 阶段规划；
- source/destination 都必须通过 PathGuard、敏感路径和 symlink component 检查；
- destination 不得仅以大小写差异等价于 source。

所有 path 使用 workspace-relative POSIX 表示，长度最多 1024，不允许空值、NUL、绝对路径、`.`、`..` component 或反斜杠。所有文本字段合计最多 256 KiB；单文件 before/after 最大 512 KiB；单事务涉及最多 50 个文件，生成 diff 最大 512 KiB。

## 5. 结果与失败语义

成功内容保持简短，结构化事实进入 metadata：

```json
{
  "transaction_id": "...",
  "revision": 7,
  "operation_count": 3,
  "changed_files": ["src/a.py", "src/b.py"],
  "file_changes": [
    {"path": "src/a.py", "before_exists": true, "after_exists": true,
     "before_sha256": "...", "after_sha256": "..."}
  ],
  "created_directories": [],
  "moved_files": [{"source": "src/old.py", "destination": "src/b.py"}],
  "before_tree_sha256": "...",
  "after_tree_sha256": "...",
  "journal_artifact": "session-relative artifact id"
}
```

artifact reference 使用相对 session 的稳定标识。当前 receipt 为兼容既有 Candidate/session identity 仍包含 `source_root` 和 `active_root` 绝对路径，并会随 ToolResult metadata 返回；这是已知的展示层遗留，不应被下游当作 Agent-facing path 参数。后续应在不改变持久化 receipt 的前提下单独裁剪模型可见 metadata。

失败 `ToolResult` 使用：

| error_code | 语义 |
| --- | --- |
| `invalid_arguments` | schema 类型、数量、字段组合、非法 path 或 operation 冲突 |
| `path_rejected` | workspace escape、敏感路径、symlink 或不支持的文件类型 |
| `not_found` | 请求开始时要求存在的 source 不存在 |
| `already_exists` | create/move/mkdir destination 已存在 |
| `unsupported_text_encoding` | 已有文件不是合法 UTF-8 |
| `edit_conflict` | expected hash 不匹配、上下文不唯一或提交前状态变化 |
| `limit_exceeded` | operation、文本、文件或 diff 超过固定上限 |
| `no_changes` | transaction 最终没有文件内容或路径变化 |
| `rollback_failed` | 提交失败且无法完整恢复；workspace 被标记为需要人工检查 |
| `internal_error` | artifact/session I/O 或未分类 Runtime 基础设施失败 |

失败信息必须包含可行动提示，例如“重新 read_file 获取当前 sha256 后重新构造事务”，但敏感路径错误不得回显完整敏感名称或内容。失败结果不得返回看似成功的 partial changed files。

## 6. Runtime 模型

新增 `AtomicEditService`，建议内部使用不可变数据结构：

- `EditTransactionRequest`：规范化 operation 和 limits；
- `WorkspaceSnapshot`：事务涉及路径的 type、bytes、hash 和 parent identity；
- `EditPlan`：按 operation 模拟后的最终文件/目录状态；
- `EditTransactionReceipt`：提交后的 revision 与 before/after identity；
- `EditTransactionError(code, message)`：稳定失败分类。

服务层次：

```text
Tool handler
  → schema/argument validation
  → AtomicEditService.prepare
      → workspace capability check
      → normalize paths
      → PathGuard/sensitive/symlink/type check
      → read bounded snapshot
      → simulate operations and detect conflicts
      → build final bytes/diff/tree hashes
  → AtomicEditService.commit
      → acquire transaction marker
      → revalidate snapshot
      → stage artifacts and rollback backup
      → mutate workspace
      → verify final state
      → atomically publish committed journal
      → append one edit_transaction event
      → remove rollback backup
  → ToolResult
```

Tool handler 不直接操作文件。Candidate、CLI 和其他工具不得调用低层 mutation helper 绕过事务服务。

## 7. 冲突与操作排序

operations 按数组顺序模拟，但以下请求在预检阶段直接拒绝：

- 一个路径既作为 move source 又在 move 前被 delete；
- 两个 operation 创建同一 destination；
- move 形成 cycle，或 source/destination 互为父子；
- 请求直接操作目录，或自动规划目录与文件 path 冲突；
- path 作为文件使用的同时也作为目录使用；
- 操作最终抵消为与事务前相同的文件树；
- 同一文件出现不同的 `expected_sha256`。

允许的常见组合包括：同一文件多个顺序 replacement、自动创建 parent 后 create，以及 move 后对 destination replacement。move 后 replacement 仍使用 source 的事务初始 hash。

`before_tree_sha256` 和 `after_tree_sha256` 由排序后的记录计算：`relative_path NUL kind NUL content_sha256 NUL`。它们只覆盖本事务管理的文件路径，不冒充整个 Git tree hash。

## 8. 提交、回滚与 crash consistency

### 8.1 Artifact 状态机

每个事务目录：

```text
artifacts/edit-transactions/<transaction-id>/
├── request.json
├── plan.json
├── transaction.json       # prepared/committing/committed/rolled_back/recovery_required
├── changes.patch
└── backup/                # committed 后删除；不向 Agent 暴露内容
```

JSON/artifact 使用同目录临时文件、flush、`os.replace` 发布。`transaction.json` 是状态真相；session event 是 committed transaction 的索引，不是提交权威。

### 8.2 提交顺序

1. 创建唯一 transaction 目录；进程内互斥锁防止当前 Runtime 重入；
2. 写入 request、plan、diff 和所有受影响路径的 rollback backup；
3. 发布 `state=prepared`；
4. 重新验证 snapshot；
5. 发布 `state=committing`；
6. 先创建目录，再用同目录临时文件 + `os.replace` 写 create/update/move destination，随后删除 move source/delete target；
7. 校验最终路径、bytes 和 tree hash；
8. 发布不可变 committed receipt；
9. append 单一 `edit_transaction` session event；
10. 删除 rollback backup。

跨多个目录的 rename 不能被一次系统调用整体原子化，因此 backup + deterministic rollback 是必要边界。

### 8.3 失败与恢复

- `prepared` 前失败：workspace 尚未变化，清理未发布临时文件；
- `prepared/committing` 后失败：按 snapshot 反向恢复，验证 before tree hash，发布 `rolled_back`；
- rollback 或验证失败：发布 `recovery_required`，保留 backup，追加诊断 event，并使该 workspace 不再注册写入、验证或 freeze 能力；
- 进程启动/resume 发现非终态 transaction：在开放 execution capability 前运行同一恢复流程；不能把未知 dirty 状态自动归因给 Agent；
- committed journal 已发布但 event append 失败：resume 从 journal 补建幂等 event。事件以 transaction id 去重。

在 `committed` 前 Agent 不会收到成功结果。若成功结果因上层连接中断丢失，重复提交同一个显式 transaction id 不在首版 schema 中支持；Agent 应重新读取 Git/status 和文件状态。

## 9. Journal 与 edit revision

每个 committed transaction 产生单调递增的 session-local `revision`。receipt 至少包含：

- schema version、transaction id、revision、session/workspace/base identity；
- 规范化 operation 列表；
- 每个逻辑文件的 before/after kind、path、SHA-256；
- before/after tree hash；
- changed/created/updated/deleted/moved paths；
- diff artifact SHA-256；
- committed timestamp。

任何成功 transaction 都分配新的 `candidate_revision`，与 sandboxed command 共用 validation evidence 失效边界。验证证据绑定当前 revision、subject tree 和 Sandbox Policy。

旧 `edit_receipt` 在已有 session 中仍可读取并映射为 legacy single-file revision；新实现只写 `edit_transaction`。迁移不能改写或删除既有 session artifacts。

## 10. Candidate 与 resume 集成

Candidate 不再假设所有 managed path 在当前 workspace 都是普通文件：

- create/update：当前 bytes 必须等于最后 transaction 的 after hash；
- delete：路径必须不存在；
- move：source 必须不存在，destination bytes 必须等于 after hash；
- 同一路径跨多个 transaction 变化时，从 committed transaction 顺序归并最终状态；
- Candidate `changed_files` 包含最终 Git patch 涉及的 source 和 destination，另提供结构化 `file_changes`。

Candidate patch 继续由固定 Git argv 从 `base_commit` 与最终 workspace 生成，并执行大小、UTF-8、identity 和 `git apply --check` 边界。必须改用 porcelain v1 `-z` parser 收集 status path，不能继续解析 `line[3:]`。

resume 只信任 committed transaction journal：当前 managed paths 与归并后的最终 identity 完全一致，且其他 dirty path 能由 command audit 解释时，状态为 `candidate_changes`。发现 `prepared`、`committing`、`recovery_required`、hash 不匹配或未知 deletion 时，状态不可执行并给出恢复/人工检查提示。

Candidate apply 仍不暴露给 Agent，继续要求 source clean、HEAD/base identity、两次 `git apply --check`、用户明确批准并应用完全相同的 frozen bytes。

## 11. Policy、approval 与安全边界

- `apply_workspace_edit` 是 `CANDIDATE_WRITE`，只在已确认可执行的 isolated Git worktree 注册，不逐次 approval；
- transaction metadata 和 CLI tool output 必须清楚列出路径和操作摘要；最终 source apply 仍单独批准；
- 服务必须再次验证 workspace kind/root，不能只依赖 registry；
- source root 和 active root 的 identity 进入 receipt；
- sensitive path 的 source 与 destination 均拒绝；`.env.example` 沿用现有例外；
- 不跟随任何路径 component symlink，也不编辑 symlink 本身；
- permissions、ownership 等 metadata 不复制或更改；新文件使用受限普通文件 mode，具体受进程 umask 约束；
- 该边界只防止工具 API 越权，不把 execution worktree 视为针对恶意进程的主机 sandbox。

## 12. 测试计划

不接入真实 LLM。Fake Model 仅按确定序列驱动正式 tool-call loop。

### 12.1 Schema 与参数

- 顶层及所有 oneOf operation `additionalProperties=false`；
- required、enum、array/text/path/hash limits；
- 未知字段、空 operations、非法 op、非 POSIX path 和 operation 冲突；
- ToolRegistry 只在可执行 worktree 暴露工具，permission 为 `CANDIDATE_WRITE`。

### 12.2 操作能力

- 单/多文件 replacement、同文件多 hunk、LF/CRLF；
- create 空/非空文件、显式多层 mkdir；
- delete、跨目录 move、move 后 replace；
- Unicode、空格、换行等合法路径；
- 50 文件、文件大小、文本总量和 diff 上限边界。

### 12.3 安全与冲突

- source/readonly 拒绝、absolute/`..`/backslash/workspace escape；
- sensitive source/destination、symlink component/target、binary、非 UTF-8、目录和特殊文件；
- stale SHA、missing source、existing destination、重复路径和 move cycle；
- prepare 后外部修改文件、替换 parent 为 symlink、destination race。

### 12.4 原子性与恢复故障注入

- 每个 mutation 步骤失败后所有文件与目录恢复；
- artifact request/plan/backup/state/event 写入分别失败；
- rollback 成功产生 `rolled_back` 且不产生 edit revision；
- rollback 失败产生 `recovery_required` 并禁用 execution capability；
- 模拟进程遗留 prepared/committing transaction，resume 恢复或 fail closed；
- committed journal/event 补建幂等，不产生重复 revision。

### 12.5 Candidate 与完整流程

- create/update/delete/move 的状态归并、hash 校验、freeze patch 与 apply；
- 多 transaction 后只接受最后 revision 之后的 validation；
- managed deletion 与未知 deletion 的 resume 区分；
- journal/patch/manifest 篡改拒绝；
- Fake Model 经 ToolRegistry → Policy → AtomicEditService → tool result → validation → freeze；
- 正式 CLI 手工 `/call apply_workspace_edit`、resume、show/freeze/apply dogfood；
- 新增测试、全量 pytest、`compileall` 和 `git diff --check`。

## 13. 实现分解

### Phase A：事务核心

- immutable request/plan/receipt/error model；
- schema 与 Runtime 参数验证，确认只暴露四种 operation；
- snapshot、operation simulation、diff 与 limits；
- commit、rollback、artifact 状态机和故障注入测试。

### Phase B：工具与兼容入口

- 注册 `apply_workspace_edit`；
- `read_file.metadata.sha256`；
- 从 ToolRegistry 移除旧 `apply_text_patch`，保留 legacy journal 读取兼容；
- Runner/Fake Model/CLI 调用链测试。

### Phase C：Candidate、resume 与 migration

- revision/event/journal reader；
- create/delete/move 最终状态归并；
- Git `-z` status parser、Candidate patch/apply；
- legacy session 读取兼容和 crash recovery gate。

### Phase D：文档与 dogfood

- 更新 Architecture、Capability Inventory、Testing、Manual Test；
- 完整 pytest 与 execution CLI dogfood；
- 汇报实际能力、故障测试、已知限制。

实现顺序允许 Phase A/B 先落地，但在 Phase C 完成前不能宣称 delete/move 已具备完整 Candidate 交付能力，也不能对 Agent 注册这些 operation。

## 14. 与后续设计的接口

- validation evidence 必须绑定 `candidate_revision`；Runtime 不预设命令 profile 或验证质量分类；
- Execution Backend 不负责文件事务。未来 container backend 只改变 active workspace mapping，AtomicEditService 仍操作 Runtime 授权的 workspace root；
- SWE-bench adapter 只消费最终 Candidate patch，不读取或绕过 edit operations；
- 产品级 host sandbox 不改变本设计的 PathGuard、transaction journal 和 Candidate identity 要求。

## 15. 已冻结结论与已知限制

本设计已经决定：四种结构化 operations、Runtime 自动规划父目录、一等 move、强制 before SHA-256、多文件 rollback transaction、可恢复 journal、session-local edit revision，以及 Candidate 继续以 Git patch 为交付事实。

首版已知限制：仅 UTF-8 普通文本；不处理 mode、copy、case-only rename、目录删除和跨进程协同；正常失败可回滚，但极端掉电可能进入 `recovery_required`，此时必须 fail closed，不自动猜测用户文件状态。

`recovery_required` 的人工解锁与同 session 恢复体验已明确暂缓，当前只要求证据保留和 fail-closed；见历史 [DECISIONS](../DECISIONS_2026-09-04_PRE_CONSOLIDATION.md#d-2026-08-27-01recovery_required-当前只提供-fail-closed)。
