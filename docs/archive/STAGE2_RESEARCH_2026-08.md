# 阶段二统一调研：基础工具与执行边界

> 文档性质：2026-08-26 的设计前研究快照。本文保留代码审计、外部证据和当时的方案比较，不再代表当前实现状态、最终决策或实施顺序。已解决问题以对应 Technical Design、[DECISIONS.md](../DECISIONS.md) 和 [NEXT_PHASE_PLAN.md](../NEXT_PHASE_PLAN.md) 为准。

本文记录阶段二 Technical Design 前的统一调研结论。它用于确认现状、外部事实、共同约束、建议边界和开放问题，不直接冻结工具 schema，也不授权实现 Runtime 变更。

当前实施计划见 [NEXT_PHASE_PLAN.md](../NEXT_PHASE_PLAN.md)，v0.4 已实现边界见 [ARCHITECTURE.md](../ARCHITECTURE.md)。后续应基于本文分别形成搜索与读取、原子编辑、命令与验证、Execution Backend 四份 Technical Design。

## 1. 调研范围与方法

本轮覆盖：

- 当前 `ToolRegistry`、文件/Git 读取工具和 `ToolResult`；
- `TextPatchService`、edit journal、Candidate freeze/apply；
- `CommandService`、`CommandPolicy`、`LocalCommandExecutor` 和 command artifacts；
- ripgrep、Git、pytest 的官方行为；
- Docker 的挂载、网络、资源和安全边界；
- SWE-bench 官方 Docker harness 与 prediction patch 交接方式。

证据分为三类：

- **代码事实**：当前仓库已经存在的行为；
- **外部事实**：官方文档或官方仓库明确说明的行为；
- **设计推断**：为保持现有安全不变量并支持未来扩展作出的建议，仍需 Technical Design 确认。

本机可用版本为 ripgrep 14.1.0、Git 2.43.0、pytest 9.1.1。当前 WSL 环境没有 Docker，因此本轮没有进行容器原型验证；Docker 和 SWE-bench 结论仅作为设计输入。

## 2. 当前实现审计

### 2.1 工具与结果模型

当前工具通过 `ToolSpec` 向模型暴露 name、description 和 JSON schema，handler 留在 Runtime。`DefaultPolicy` 根据 `PermissionLevel` 决定 allow、ask 或 deny，`AgentRunner` 负责把工具结果写入 session event 并反馈给模型。

当前 `ToolResult` 只有：

- `ok`；
- `content`；
- `error`；
- `truncated`；
- 自由结构的 `metadata`。

这足以支持 v0.4，但不能稳定区分 invalid arguments、path rejected、tool unavailable、timeout、output limited 和 backend failure。命令执行已有更细状态，但普通工具没有共享的失败分类。

### 2.2 搜索与读取

当前 `search_text` 使用 Python `rglob` 顺序读取文件：

- 仅支持字符串包含，不支持正则和大小写策略；
- ignore 依赖硬编码 `SKIP_DIRS`，与 repository 的 `.gitignore` 不一致；
- 每个文件整体读入内存；
- 敏感路径通过 `PathGuard` 二次过滤；
- 最多收集 100 条，但 metadata 无“命中上限”与“扫描不完整”区分；
- 输出是面向人类的 `path:line:text`，不是稳定的内部结构。

`find_files` 使用 `Path.glob`，与搜索工具的 ignore、hidden 和 symlink 语义并不统一。`read_file` 支持行范围，但 UTF-8 解码使用 replacement，模型无法区分合法文本和发生过解码替换的内容。

Git 读取只有 `git status --short` 与 `git diff --stat`。两者直接返回文本，没有稳定解析路径，也没有实际 patch、staged/unstaged 选择、base revision 或输出上限协议。

### 2.3 编辑与 Candidate

当前编辑以一次唯一文本替换为最小事务：

- 只允许 execution worktree；
- 路径必须是 workspace-relative 且不能经过 symlink；
- 支持修改已有 UTF-8 文件或在已有父目录中新建文件；
- 不支持目录创建、删除、移动、重命名和多文件原子提交；
- journal 在单文件写入后持久化，journal 失败会回滚该文件。

Candidate 通过 edit events 重建“由 Agent 管理的路径集合”，并以每个路径最后一次 edit 的 hash 校验当前内容。该模型无法直接表达：

- 一次事务包含多个文件；
- 文件已删除，因此没有 after content hash；
- rename 的 source/destination 身份；
- 事务中途失败后的整体回滚；
- 多次事务之间的明确 parent revision。

Candidate patch 最终仍由 Git 生成并由 `git apply --check` 验证，这是可以保留的可靠交付边界。

### 2.4 命令、验证与执行器

当前 `run_check` 同时隐含了三层概念：

1. 模型选择 `kind=pytest` 和 targets；
2. Runtime 构造固定 `sys.executable -m pytest` argv；
3. `LocalCommandExecutor` 在 Linux worktree 中启动进程。

这导致 profile、解释器和 backend 绑定在同一条路径。未来若直接增加 ruff、mypy、build 或 SWE-bench 容器，很容易产生大量 `if kind == ...` 分支。

现有安全不变量必须保留：

- 模型不提交 shell 字符串；
- Policy 前先构造不可变的执行请求；
- approval 看到的必须是最终执行对象；
- executor 再次检查 workspace、argv 和 cwd；
- stdout/stderr 与 workspace audit 位于 worktree 外；
- 被测命令失败与 Runtime 启动失败分开表达。

### 2.5 验证证据

Candidate 当前硬编码要求最后一次 edit 之后存在成功 pytest receipt，并将其写入 `test_receipts`。扩展 command profiles 后会出现以下问题：

- 纯 typing 或 build maintenance 是否必须运行 pytest；
- targeted test、full test、lint、typecheck、build 哪些是 required；
- 多个 profile 中一个失败、随后另一个成功时怎样判定；
- 新编辑是否会使此前全部证据失效；
- backend、环境和 profile revision 是否属于证据身份。

因此后续需要从 `test_receipts` 提升为通用 `validation_evidence`，但不能在统一调研阶段直接决定最终 schema。

## 3. 外部事实

### 3.1 ripgrep

ripgrep 默认递归搜索并遵守 `.gitignore`、`.ignore` 和 `.rgignore`，同时跳过 hidden、binary，并且默认不跟随 symlink。每类过滤都可以用显式参数改变。[ripgrep User Guide](https://github.com/BurntSushi/ripgrep/blob/master/GUIDE.md)

`--json` 输出 JSON Lines，包含 begin、match、context、end 和 summary 等消息；相比解析普通 `path:line:text`，它能可靠处理冒号、特殊路径和匹配文本。`--files` 可以复用相同过滤语义列举候选文件。[ripgrep README](https://github.com/BurntSushi/ripgrep/blob/master/README.md)

设计推断：首批搜索后端可以使用固定 argv 调用 `rg --json`，但模型不应直接提供任意 rg flags。literal/regex、case、path、include/exclude、context 和 limit 应由结构化参数映射。Runtime 仍需在解析后执行 PathGuard、敏感路径和结果上限检查，不能把 ripgrep 当作安全边界。

需要在 Technical Design 中决定：

- `rg` 缺失时 fail closed，还是提供能力较弱但语义明确的 Python fallback；
- 默认是否包含 hidden tracked files；
- `.ignore`、`.rgignore` 是否允许改变产品层搜索范围；
- regex 是否只使用 ripgrep 默认引擎，是否开放 PCRE2；
- 文件列举与文本搜索是否共享一个 ignore policy。

### 3.2 Git status、diff 与 apply

Git 官方说明 `--porcelain=v1` 是面向脚本的稳定格式，`-z` 使用 NUL 分隔路径并避免特殊字符 quoting；rename 在 `-z` 模式下有不同字段顺序，必须按格式解析，不能继续依赖 `line[3:]`。[git-status](https://git-scm.com/docs/git-status)

Git patch 格式能够表示 create、delete、mode、rename/copy 和 binary diff。rename/copy 不是一系列可以按显示顺序独立应用的单文件修改。[git-diff](https://git-scm.com/docs/git-diff)

`git apply` 默认要求 unified diff 至少包含上下文，并提供 `--check` 预检；无上下文 patch 会降低安全性，官方不建议默认使用。[git-apply](https://git-scm.com/docs/git-apply)

设计推断：

- Git status/audit 应迁移到 `--porcelain=v1 -z` 和共享 parser；
- 模型提交的编辑请求不等同于可直接交付的 Git patch；Runtime 应先验证并原子执行文件操作，再由 Git 生成 Candidate patch；
- rename 应作为结构化文件操作进入 edit transaction，Candidate patch 仍以 Git 事实为准；
- apply 的两次 identity check 和固定 patch bytes 不应改变。

### 3.3 pytest 与项目验证

pytest 会自动加载环境中通过 entry point 注册的第三方插件，除非显式禁用 autoload；项目也可能依赖这些插件。因此不能为了可复现性无条件关闭全部插件，否则可能改变项目的真实测试入口。[pytest plugin loading](https://docs.pytest.org/en/stable/how-to/writing_plugins.html)

设计推断：command profile 必须把 executable、固定 argv、允许参数、cwd、environment 和 timeout 作为项目/环境提供的可信配置，而不是由 Agent 拼接。当前 `-p no:cacheprovider` 可以保留为 pytest profile 的固定副作用约束，但其他插件策略应由 profile 明确声明。

### 3.4 Docker 不是自动成立的 sandbox

Docker bind mount 默认可写，容器进程能够修改对应 host path；只读挂载需要显式 `readonly`。bind mount 绑定的是 Docker daemon 主机路径，远程 daemon 不能直接访问 client path。[Docker bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)

Docker container 默认没有 CPU 和内存上限，需要显式资源约束；否则容器可以使用 host scheduler 允许的资源。[Docker resource constraints](https://docs.docker.com/engine/containers/resource_constraints/)

`--network none` 才会创建只有 loopback 的隔离网络栈。默认网络配置不能被记录为“无网络”。[Docker none network](https://docs.docker.com/engine/network/drivers/none/)

Docker 默认 seccomp 会阻止一部分系统调用，但只是中等强度的兼容性默认；官方也强调应尽量移除不需要的 capabilities。Rootless mode 可以让 daemon 和 container 都运行在 user namespace 中，以降低 daemon/runtime 漏洞影响。[Docker seccomp](https://docs.docker.com/engine/security/seccomp/)、[Docker Engine security](https://docs.docker.com/engine/security/)、[Docker rootless mode](https://docs.docker.com/engine/security/rootless/)

设计推断：未来 `ContainerExecutionBackend` 必须报告实际生效的挂载、网络、资源、用户、capabilities 和 seccomp 保证。仅记录 `backend=container` 不足以形成安全证据。backend 无法满足 profile 的 required guarantees 时必须拒绝执行，不能静默降级到 local executor。

### 3.5 SWE-bench

SWE-bench 官方 harness 使用 Docker 构建 base、environment 和 instance images，并对 prediction 中的 `model_patch` 应用和评分。官方 quickstart 建议先用 gold patch 验证评测环境；harness 的核心输入包含 instance id、model identity 和 patch。[SWE-bench Quickstart](https://www.swebench.com/SWE-bench/guides/quickstart/)、[official run_evaluation](https://github.com/SWE-bench/SWE-bench/blob/main/swebench/harness/run_evaluation.py)

官方说明评估会消耗大量磁盘、内存和 CPU，并存在 image/result cache。相同 run id 与 instance id 可能复用已有结果，因此 CodeAgent adapter 必须生成不会错误复用的 run identity，并保存环境失败与 grader 结果。[SWE-bench Docker Setup](https://www.swebench.com/SWE-bench/guides/docker_setup/)

设计推断：SWE-bench 有两个不同执行域：

1. **Agent task environment**：Agent 在准备好的 instance repository 中搜索、编辑和运行公开验证；
2. **official grading environment**：adapter 只提交最终 patch，官方 harness 应用并运行 grader。

grader tests、gold patch 和评分命令不能注册为 Agent 工具。adapter 不能替 Agent 搜索、编辑或生成 patch，也不能把 grader success 伪装成 Candidate freeze 前的普通 validation evidence。

## 4. 统一设计边界建议

以下是本轮调研推荐的总体结构，仍需子设计确认：

```text
Model tool call
  → Tool schema validation
  → workspace/path validation
  → Policy / Approval
  → immutable operation spec
  → service or ExecutionBackend
  → typed result
  → receipt/artifacts/events
  → bounded model observation
```

### 4.1 工具层与后端层分离

工具 schema 表达 Agent 可以选择的逻辑操作；backend 表达操作在哪里和以什么保证执行。模型不应看到 host/container 路径、Docker flags、image id 或可执行文件绝对路径。

建议至少区分：

- `SearchService`：固定 ripgrep 调用和结果解析；
- `WorkspaceEditService`：原子文件操作事务；
- `CommandProfileRegistry`：可信 profile 定义和结构化参数绑定；
- `ExecutionBackend`：执行已经冻结的 request；
- `ValidationPolicy`：决定 Candidate 所需证据；
- `CandidateService`：验证 journal、evidence 和 workspace 后冻结 Git patch。

### 4.2 workspace-relative identity

所有模型输入路径和 edit/command receipt 应使用 workspace-relative POSIX 表示。host absolute path、container mount path 和 benchmark workdir 只属于 backend mapping 和诊断 artifact。

需要显式区分：

- source workspace；
- active task workspace；
- execution-visible workspace path；
- artifact export path。

这能让同一工具协议运行在当前 Git worktree、未来 bind mount 或 SWE-bench instance container 中。

### 4.3 Execution requirements 与 guarantees

command profile 不应直接选择某个 backend，而应声明最低执行要求，例如：

- writable workspace；
- network disabled/allowed；
- isolated home/tmp；
- timeout；
- required interpreter/runtime；
- resource limit 是否必须；
- workspace audit 是否必须。

backend 在执行前返回或冻结实际 guarantees。requirements 无法满足时产生独立的 backend rejection，不执行命令。receipt 应同时保存 requirements、guarantees 和实际 workspace mapping 摘要。

当前 `LocalCommandExecutor` 的诚实 guarantees 应包括 Linux process group、minimal environment、timeout、artifact 和 Git audit，同时明确没有 network、CPU、memory、disk、PID 或 syscall isolation。

### 4.4 通用 validation evidence

Candidate 不应只查找任意成功 command，而应根据冻结的 validation policy 判断。建议后续设计至少表达：

- profile id 和 profile revision/hash；
- command request/receipt id；
- backend guarantees；
- base commit 与 edit revision；
- exit status；
- started/finished time；
- targeted/full scope；
- required/optional；
- evidence artifact hash。

任何新的 edit transaction 都应使旧 revision 的 required evidence 失效。SWE-bench grader result 属于外部 evaluation evidence，不替代 Candidate 的本地 validation policy。

### 4.5 原子编辑事务

编辑请求应先完成全部预检，再进行任何写入。预检至少包括：

- 所有路径规范化、边界、symlink 和敏感路径检查；
- 操作冲突，例如同一路径同时 delete/update；
- before hash/context；
- 文本编码、文件类型和大小；
- rename source/destination；
- 最终父目录计划；
- 整体 patch 和文件数量上限。

执行失败必须恢复事务前状态；journal 只有在全部文件与 artifact 写入成功后才提交。journal 应记录 transaction id、操作列表、before/after identity 和统一 diff，而不是只靠多个无关联的单文件 event 推断原子性。

### 4.6 统一结果与失败分类

建议普通工具和命令共享顶层失败类别：

- `invalid_arguments`；
- `policy_denied`；
- `approval_denied`；
- `path_rejected`；
- `tool_unavailable`；
- `backend_rejected`；
- `timed_out`；
- `cancelled`；
- `completed`；
- `internal_error`。

领域结果仍可细分，例如 search 的 no matches 不是失败，pytest assertion failure 是 completed/non-zero，patch context mismatch 是 completed domain rejection，而非 Runtime internal error。

## 5. 四份 Technical Design 的范围

### 5.1 Search and Read Tools

需要冻结：

- 工具划分：file listing、text search、file read、Git status/diff；
- ripgrep 固定 argv 与参数映射；
- ignore/hidden/symlink/binary/sensitive 规则；
- JSON 解析和排序；
- match、file、byte、line/context 上限；
- rg missing、invalid regex、partial output、timeout 的失败语义；
- Git porcelain `-z` parser 与 diff 输出边界。

### 5.2 Atomic Workspace Edit Protocol

需要冻结：

- create/update/delete/move/mkdir 的结构化操作；
- multi-file transaction schema；
- before hash/context 与 newline 规则；
- 事务预检、提交、回滚和 crash consistency；
- journal/event/artifact schema；
- Candidate 收集与 resume 状态解释。

### 5.3 Command Profiles and Validation

需要冻结：

- profile 来源、信任边界和 revision；
- executable/runtime reference 与 backend 解析；
- 结构化参数模板；
- cwd、environment、timeout、network 和 side-effect policy；
- targeted/full pytest、lint、typecheck、build、CLI smoke 首批范围；
- validation policy 与 Candidate evidence；
- profile unavailable、command failure 和 invalid evidence。

### 5.4 Execution Backend Boundary

需要冻结：

- immutable execution request/result；
- workspace/path mapping；
- requirements/guarantees；
- local POSIX backend 当前保证；
- container backend 的未来接口，不实现 Docker；
- stdout/stderr、取消、timeout、resource termination 和 artifact export；
- backend rejection、infrastructure failure 和 command result 的区分；
- SWE-bench task environment 与 grader adapter 的边界。

## 6. 建议设计与实现顺序

四份设计不应完全串行，也不应一次冻结全部细节。建议顺序：

1. 先共同冻结 workspace-relative identity、统一失败分类和 operation/result envelope；
2. 并行推进 Search/Read 与 Execution Backend 两份设计；
3. 基于 backend request 完成 Command Profiles/Validation；
4. 基于 edit revision 与 validation evidence 完成 Atomic Edit/Candidate 契约；
5. 设计评审通过后按“搜索 → 编辑 → command profiles → feedback/summary”分阶段实现；
6. 本地 fixture dogfood 后再实现 SWE-bench adapter；
7. 是否实现 container sandbox 由任务评估和威胁模型决定，不与 SWE-bench adapter 自动绑定。

这里的依赖不是代码必须按同样顺序落地。Atomic Edit 可以先实现主体，但 Candidate evidence 的最终 schema 必须与 Command Profiles 设计对齐。

## 7. 开放问题

进入 Technical Design 前仍需逐项决策：

1. ripgrep 是必须依赖，还是允许语义降级的 fallback？
2. 搜索是否默认只覆盖 Git/ignore 允许的非 hidden 文件，如何显式读取 tracked hidden 文件？
3. read tool 遇到非 UTF-8 文本时应拒绝、声明 encoding，还是有限 replacement？
4. edit transaction 使用结构化 operations，还是接受受限 unified patch 作为输入？
5. mkdir 是显式操作还是 create/move 的受控隐式父目录计划？
6. rename 由 Runtime 直接执行，还是只表达 delete+create 并由 Git 在 Candidate 阶段检测？
7. command profiles 首批从内置配置、项目文件还是 CLI 注入？谁有权修改？
8. `project_python` 如何解析：当前进程、显式绝对路径、venv metadata 或 backend image contract？
9. Candidate 的 required validation 默认策略是什么，用户如何审查或覆盖？
10. local backend 缺乏 network isolation 时，声明 `network=disabled` 的 profile 应拒绝还是允许用户显式降级？
11. container task workspace 应直接 writable bind mount，还是 copy-in/copy-out 后比较 patch？
12. SWE-bench inference 是否复用官方 instance image，还是在官方 grader 之外准备独立但等价的 Agent image？

这些问题应在对应子设计中给出选项、trade-off 和推荐结论；未确认前不进入实现。

## 8. 本轮结论

阶段二可以独立于具体 sandbox 产品实现，但不能独立于执行隔离语义设计。当前最重要的不是立即引入 Docker，而是先把工具逻辑、Policy、不可变 operation、Execution Backend、validation evidence 和 Candidate 分层。

搜索层适合采用 ripgrep 的成熟 ignore 与 JSON 输出；编辑层应采用 Runtime 管理的原子事务并继续让 Git 生成最终 Candidate patch；命令层应从 pytest 特例演进为可信 profile；SWE-bench 应保持“Agent 产出 patch、官方 harness 独立评分”的薄 adapter 边界。

下一步应先形成统一 envelope/identity 决策，再开始 `Search and Read Tools` 与 `Execution Backend Boundary` 两份 Technical Design。
