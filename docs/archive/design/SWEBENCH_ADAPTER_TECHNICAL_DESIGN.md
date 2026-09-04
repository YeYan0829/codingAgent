# SWE-bench Adapter Technical Design

状态：Phase 0 结论已接受；Phase 1 Source Preparation、Phase 2 Execution Projection 已实现并通过真实 Docker 验收

## 1. 目标与边界

第一版 adapter 将官方 benchmark 环境转换为现有 CodeAgent 能消费的输入，不重定义 Workspace、Candidate 或 Session：

```text
task metadata → official image（固定 digest）→ export prepared /testbed
→ Git admission → 现有 GitWorktreeManager / Candidate
```

Phase 1 只实现 source preparation。Phase 2 实现 Candidate worktree 到容器 `/testbed` 的 projection 和 Docker CommandExecutor；grader、批量调度和统计仍不在范围内。

## 2. Phase 0 证据与决策

对 `sympy__sympy-20590` 的官方镜像 `sha256:3a282752833ce34730ee0621e22033501c993f45742775ca57f04c9ff27178a0` 做过真实实验：

- `/testbed/.git` 是普通、自包含的 Git directory；
- repository 非 shallow，不使用 alternates，没有失效的绝对路径配置；
- 导出到 WSL 后 `git fsck`、`git status` 和 `git worktree add` 均成功；
- 当前 `GitWorktreeManager` 直接消费导出 repo 后状态为 `ready`；
- prepared HEAD `5efaed257c...` 的父提交才是 dataset base `cffd4e0f...`；
- setup 将 1758 个 tracked path 的 mode 从 `100644` 改为 `100755` 并提交，证明 host clone/reset 不能普遍替代 official prepared source。

因此采用方案 B：官方 prepared `/testbed` 是 source 内容的事实来源。

## 3. Identity

| 字段 | 含义 |
|---|---|
| `instance_id` | benchmark task identity |
| `image_ref` | 数据集提供的 image reference |
| `image_digest` | 实际消费的不可变 image identity |
| `dataset_base_commit` | benchmark provenance 与官方 grader 基准 |
| `prepared_head` | CodeAgent source/Candidate 的 Git baseline |
| `prepared_tree` | prepared 内容与 file mode identity |

不得把 `dataset_base_commit` 直接写入现有 `WorkspaceContext.base_commit`；CodeAgent 创建 worktree 后的 `base_commit` 是 `prepared_head`。

## 4. Preparation lifecycle

`SWEbenchSourcePreparer` 执行：

1. 校验 task 输入，拒绝可逃逸 cache root 的 instance id；
2. pull image ref，从 image inspect 取得唯一 RepoDigest；
3. 用 `repository@sha256:...` 创建一次性 container；
4. 把完整 `/testbed/.` 导出到 cache staging，包括 `.git` 和 ignored artifacts；
5. 执行 Git admission 与真实 CodeAgent worktree probe；
6. 写 manifest，并把 staging 原子发布到 digest-keyed cache；
7. 成功或失败均移除一次性 container；失败 staging 不进入正式 cache。

```text
<cache_root>/
├── staging/<instance>-<uuid>/
└── sources/<instance>/<digest>/
    ├── manifest.json
    └── source/.git/...
```

cache hit 仍重新执行 Git admission 并核对 manifest identity，不只相信目录存在。

## 5. Git admission

正式 cache 必须满足：

- `/testbed/.git` 是普通目录，且 `/testbed` 是 repository root；
- repository 非 shallow，不使用 object alternates；
- `git status --porcelain=v1 --untracked-files=all` 为空；
- benchmark 依赖的 base/HEAD commit 与 tree object 可读取；不要求完整历史
  `git fsck --full` 成功，因为部分经典仓库存在与目标快照无关的旧对象格式或坏远端引用；
- dataset base 存在且是 prepared HEAD 的 ancestor；
- HEAD/tree 是合法 object id；
- 当前 `GitWorktreeManager` 能创建、inspect 为 executable 并清理 detached worktree。

ignored build/install artifacts允许保留，因为它们属于官方 prepared `/testbed`，且不进入 Candidate diff。non-ignored untracked、tracked dirty、Git metadata 缺失或 identity 不匹配一律 fail closed。

## 6. Failure semantics

`SWEbenchPreparationError.stage` 区分 `input`、`image_pull`、`image_identity`、`container_create`、`container_cleanup`、`docker`、`git_admission` 和 `cache_validation`。这些是 Agent turn 之前的 environment/preparation failure，不得统计为 Agent failure。

## 7. Phase 2：Execution projection

命令执行保持以下边界：

```text
host CodeAgent linked worktree → execution projection → official container /testbed
→ run_command / validation
```

每条命令创建一次性 digest-pinned container：

- host Candidate 以 read-write bind mount 映射到 `/testbed`；
- container payload 使用宿主 UID/GID，保证 bind mount 写回且不通过宽泛 `safe.directory` 绕过 Git ownership 检查；
- Candidate `.git` gitfile 单独只读覆盖，避免命令改写；
- gitfile 指向的 source common Git directory 按原 host 绝对路径只读映射，使容器内 `git status/diff/rev-parse` 可以解析 linked worktree；
- runtime HOME/TMPDIR 使用独立 host artifact 目录；
- source/Git metadata 不可写，Docker socket 不进入容器；
- 默认 `--network none`、drop all capabilities、`no-new-privileges`；
- 当前不模拟 WSL host network 语义，出现 network grant 时 executor fail closed；
- container 退出或超时后强制清理，stdout/stderr 继续使用既有有界 artifact；
- 宿主 `CommandService` 在 payload 停止后使用既有 workspace snapshot/audit 识别命令导致的 Candidate 变化。

容器只是命令执行域，不持有新的 Workspace identity。Search、Read、Atomic Edit、Candidate、snapshot、freeze/accept/discard 继续只认 host worktree。

真实官方镜像验收为 opt-in，避免普通 pytest 隐式拉取 GB 级镜像：

```bash
CODEAGENT_RUN_SWEBENCH_DOCKER=1 \
  pytest -q tests/test_swebench_docker_integration.py
```

验收覆盖 prepared source、真实 linked worktree、容器内 Git/Conda Python、容器写回 host Candidate、宿主 workspace audit、source 隔离和最终 worktree/container 清理。

## 8. Phase 3：单任务 harness 与 official grader

单任务 harness 已接入正式 `AgentRunner`，自动继续同一 UserTurn 的 execution slice，使用 `CandidateService.preview_patch()` 导出当前 tree 相对 prepared HEAD 的 patch，并生成官方 prediction JSON。`.json` 顶层遵循官方协议，即使单题也输出 prediction list。它不把 patch apply 回 source。

结果独立记录 `agent_status/agent_final`、`validation_passed`、`patch_present` 与 `oracle_status/oracle_passed`。Phase 3 不把容器内临时安装状态、Agent final text或普通 validation command误当作 external oracle。

每次模型请求完成后，Runtime 还会追加独立的 `model_usage` 事件，记录 provider、model、provider request id，
以及 provider 实际返回的 input/output/total、cache hit/cache miss 和 reasoning token（不提供的字段为
`null`）。这类事件只用于审计，不进入模型上下文，也不参与 UserTurn 的模型步骤计数。即使模型返回的
tool arguments 无法解析，只要 provider 已返回 usage，该次请求仍必须记录。单题 `result.json` 的
`token_usage` 聚合请求数和各 token 字段；`requests_with_usage < requests` 明确表示计量不完整，不得把
缺失值按零计费。人民币成本不固化进 Session 事件，因为 provider 价格会变化；报告层应按运行时选定的
价格快照计算成本。

`SWEbenchCLIGrader` 调用固定版本官方 harness。command prefix 与 report path template 由 benchmark 配置明确注入，Runtime 不扫描或猜测不同 SWE-bench 版本的日志布局；非零退出、调用失败、缺失/无效 report 分别返回 grader environment 状态。

### Gold preflight

`SWEbenchTaskRepository` 从固定 commit 的 task repo 只读加载 task metadata，并把
problem statement 提供给 Agent、把 gold/test/eval artifacts 仅写入 grader dataset。
Agent 的 ToolRegistry 不注册这些评估 artifact。`SWEbenchGoldPreflight` 使用官方
`predictions_path=gold`，逐题记录：

- `gold_passed`：官方 gold patch resolved，可进入 Agent benchmark；
- `gold_failed`：官方进程完成但 gold 未满足全部 required tests，当前题不进入正式集；
- `environment_failed`：Docker、超时、report 缺失或官方 infrastructure/error 状态。

首批题使用 `max_workers=1`，避免当前 Docker Desktop 3.7 GiB 内存限制引入并发噪声。
`psf__requests-2317` 的 gold 在本机仍因既有网络/序列化回归测试失败而 unresolved，
已由 gold 通过的 `pytest-dev__pytest-7432` 替换。
