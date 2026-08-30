# Controlled Arbitrary Command + Sandbox Technical Design

状态：**Accepted and Implemented（2026-08-28）**  
目标环境：Linux / WSL2 单机本地运行  
取代对象：实现完成后取代 `Command Profiles / Validation` 的命令限制机制

本文既是已批准契约，也是当前实现的设计依据。实际模块、测试证据和已知限制以 `ARCHITECTURE.md`、`TESTING.md` 为准。

“能力”指对某类宿主资源的授权，例如“写 `/opt/cache`”；“授权”指 Policy 或用户批准后得到的具体能力；“有效策略”指某条命令真正执行时，基础规则和授权合并后的结果。

## 1. Problem / Motivation

设计开始前的 `pytest.targeted` / `pytest.full` 只能证明固定命令接入正确，无法覆盖 `uv run pytest`、`npm test`、`cargo test`、`make` 和项目脚本。继续增加 Command Profile（预定义命令模板）会成为不完整的 shell 白名单，并把项目工具链知识错误地固化进 Runtime；这些旧组件现已删除。

目标模型改为：

```text
Agent 提出任意 shell command
→ Runtime 根据命令申请的资源做 Policy / Approval
→ Bubblewrap 强制文件系统、网络和进程边界
→ 命令只能在获准范围内产生效果
```

安全性不再来自“Runtime 认识命令名字”，而来自命令无论叫什么都无法越过相同的操作系统边界。

## 2. 被取代的执行模型（设计输入）

本设计实施前的调用链如下，仅用于解释迁移动机，不是当前架构：

```text
run_validation
→ ProfileRegistry 校验 profile/target/timeout
→ 编译固定 CommandSpec(argv)
→ CommandPolicy 只允许 pytest
→ ApprovalGate
→ LocalCommandExecutor(shell=False)
→ Git 前后审计、CommandResult、validation_completed
→ CandidateService 检查有效 pytest 记录
```

| 当前组件 | 当前职责 | 目标变化 |
| --- | --- | --- |
| `ProfileRegistry` | 命令集合、参数 schema、argv、timeout、类别和 revision | 删除 |
| `ValidationService` | 编译、审批、执行、审计、验证证据 | 拆成命令编排与验证证据 |
| `CommandPolicy` | 检查 pytest 名称、argv、target、timeout | 改为资源能力 Policy |
| `LocalCommandExecutor` | 直接在宿主启动固定 argv | 由 Bubblewrap executor 取代；禁止无沙盒 fallback |
| `ApprovalGate` | 批准一次固定命令 | 保留，输入改为增量权限申请 |
| `CommandArtifactStore` | 临时目录与失败诊断 | 保留统一存储职责 |
| `workspace_tainted` | 当前把检测到的非Atomic命令变化视为污染 | 重定义：命令变化合法；只有无法建立可信after boundary才taint |

实现已经加入 `bwrap` 可信路径 probe、namespace smoke test、安装提示和 fail-closed；实际 Linux/WSL 环境仍需单独安装系统 `bwrap`，不可用时不会回退到宿主 executor。

## 3. Goals

- 一个稳定工具支持 shell pipeline、重定向、变量、链式命令和项目脚本。
- 默认写权限只覆盖 Candidate worktree、隔离临时目录和少量 Runtime 自有目录。
- source、Git 元数据、Runtime 状态和敏感路径受到强制保护。
- 默认断网；经批准可让单条命令或本 Session 使用 WSL 当前宿主网络。
- 权限按资源增量申请，支持 `ALLOW / ASK / DENY` 与 `ONCE / SESSION`。
- Worktree 负责修改交付隔离，Sandbox 负责进程副作用隔离。
- 命令、审批、有效权限、结果和可确定违规有统一审计。
- 将来可把已准备好的 SWE-bench workspace 接到同一窄执行协议，但本阶段不实现 benchmark orchestration。

## 4. Non-goals

本阶段不实现 Landlock、Docker、microVM、gVisor、macOS/Windows 原生沙盒、远程 executor、domain/port 网络规则、代理、credential broker、动态宿主 PID 暴露、完整 cgroup、复杂 seccomp、自定义容器镜像或企业多用户隔离。

也不保证防御内核/Bubblewrap 漏洞、判断命令业务语义、联网后阻止数据外传，或自动适配所有语言缓存、后台服务、GUI、D-Bus 和 Docker daemon。

## 5. Design Principles

1. OS enforcement 是边界，命令文本分类不是边界。
2. Agent-facing command 确实任意，不保留 profile 白名单。
3. Policy 表达资源能力，不返回 Bubblewrap 参数。
4. hard deny（普通审批不可解除）优先于所有授权。
5. 默认宽读、敏感隐藏、最小写、断网。
6. 审批绑定冻结的 command request；批准后不得换命令。
7. host path 与 sandbox path 尽量一致。
8. Bubblewrap 缺失或不工作时 fail closed。
9. 正常成功只持久化摘要，不为权限或命令另建 artifact 家族。
10. Sandbox 是一次命令的 backend，不是长期容器或用户概念。

## 6. Target Execution Architecture

```text
Agent / Fake Model
  │ run_command
  ▼
ToolRegistry
  │ schema + Session state gate
  ▼
CommandService
  ├─ CommandRequestValidator
  ├─ PermissionPolicy（基础策略 + Session grants）
  ├─ ApprovalGate（只处理 ASK）
  ├─ EffectivePolicyResolver
  ├─ WorkspaceAudit / ValidationEvidenceRecorder
  └─ SandboxedCommandExecutor
       └─ BubblewrapBackend
            └─ /bin/bash --noprofile --norc -c <frozen command>
```

领域层只认识 `CommandRequest`、`PermissionRequest`、`PermissionGrant`、`EffectiveSandboxPolicy` 和 `CommandResult`。只有 `BubblewrapBackend` 知道 mount 与 namespace 参数。

后续 SWE-bench adapter 可以实现相同的窄 `CommandExecutor.execute(request, effective_policy, workspace)` 协议；本阶段不为尚不存在的 Docker backend 抽象生命周期。

## 7. Arbitrary Command API

Agent-facing 工具为单一 `run_command`：

```json
{
  "command": "uv run pytest tests/unit -q && python scripts/check.py > /tmp/check.log",
  "cwd": ".",
  "timeout_seconds": 300,
  "purpose": "validation",
  "permissions": [{
    "capability": "network",
    "reason": "uv 需要下载缺失依赖",
    "scope": "once"
  }]
}
```

- `command`：非空、有长度上限的 shell string；不接受 profile、executable 或 Bubblewrap flags。
- `cwd`：Candidate 内 repository-relative path，默认 `.`；拒绝 absolute、`..` 和 symlink escape。
- `timeout_seconds`：正整数；默认/最大值属于全局 `CommandLimits`。
- `purpose`：`utility | validation`，只决定是否形成验证记录，不决定命令是否合法。
- `permissions`：可选、闭合、有数量/长度上限的增量权限申请。

首版不静态分析 `command` 来推断文件或网络需求，也不从 stderr 自动生成 Permission Request。权限发现责任属于 Agent：它应根据任务、仓库说明和先前 diagnostics 显式填写 `permissions`。漏报时命令就在当前有效策略中执行并可能普通失败；Runtime 返回 diagnostics 与 Effective Policy（本次实际生效的权限）摘要。Agent 若判断确实缺权限，提交新的 CommandRequest 和 Permission Request。Runtime 绝不自动重放已经执行过、可能留下副作用的 shell command。

选择 shell string 是因为 pipeline、重定向、inline environment、通配、子 shell 和 `&& / || / ;` 是核心场景。Runtime 在宿主侧始终以 argv 和 `shell=False` 启动 Bubblewrap；冻结 command 作为单个参数交给沙盒内固定 `/bin/bash --noprofile --norc -c`。shell 解释只发生在沙盒内。

环境规则：

- 不继承 API key、token、SSH agent、D-Bus socket 等敏感变量；
- 继承有界 `PATH`、locale、timezone、terminal 等开发环境变量；
- `HOME` 保持宿主 identity path、默认只读，以发现用户级 SDK；
- `TMPDIR`、`XDG_CACHE_HOME` 等写缓存指向 Session 自有目录；
- 首版不开放任意宿主环境变量读取/注入。Agent 可在 command 内设置普通局部变量。

stdout/stderr 分流，结构化返回 exit code、signal、timeout 和截断信息。stdin 为 `/dev/null`，首版没有交互 TTY。

## 8. Sandbox Lifecycle

```text
validate frozen request
→ recovery_required / workspace_tainted gate
→ 必要时创建 Candidate worktree
→ 规范化 permission requests
→ Policy: ALLOW / ASK / DENY
→ ASK 时一次展示命令、增量权限和 scope
→ 形成 ONCE/SESSION grants
→ 计算 Effective Sandbox Policy
→ 复检 request、workspace 和 grant resources
→ 构造 Bubblewrap argv
→ 启动一次 sandboxed process tree
→ timeout/结束、workspace audit、记录结果
→ 最后一个进程结束，sandbox 消失
```

Bubblewrap 官方说明其从空 mount namespace 构造文件系统，最后一个进程退出后自动清理，也明确要求调用者自行定义安全模型。参见 [Bubblewrap README](https://github.com/containers/bubblewrap) 与 [bwrap man page](https://manpages.debian.org/bookworm/bubblewrap/bwrap.1.en.html)。

启动前 probe Linux/WSL2、可信绝对 `bwrap` 路径、最低版本、user/mount/PID/network namespace 和最小 smoke command。失败返回 `sandbox_unavailable`，不调用无沙盒 executor。WSL1 不支持；WSL2 走普通 Linux 路径，与 [Codex Linux sandbox](https://github.com/openai/codex/blob/main/codex-rs/linux-sandbox/README.md) 的公开边界一致。

## 9. Filesystem Policy

### 9.1 Base policy

```text
host /                          → sandbox / 相同路径              READ ONLY
Candidate active_root          → 相同路径                         READ WRITE
sandbox /tmp                   → 有大小上限的新 tmpfs             READ WRITE
Runtime session cache          → 相同路径                         READ WRITE
source_root                    → 相同路径                         READ ONLY / hard deny write
Candidate .git / actual gitdir → 相同路径                         READ ONLY / hard deny write
Session state/diagnostics      → 默认隐藏或 READ ONLY；父进程写日志
sensitive paths / host sockets→ 空目录/空文件遮罩                 NO READ / NO WRITE
```

mount plan 必须与集合输入顺序无关：先 root `--ro-bind / /`，再从宽到窄应用 writable bind，最后重新覆盖 source、Git/Runtime metadata 和 sensitive hard deny。Bubblewrap 按参数顺序应用文件系统操作；Codex 也采用“根只读、写根叠加、保护子路径重新只读”。

`/tmp` 使用 sandbox-private tmpfs，不映射宿主 `/tmp`；这是唯一有意不 identity mapping 的常用路径。跨命令缓存由 Runtime 集中拥有、限额和清理，不作为产品 artifact。

### 9.2 Sensitive read deny

首版 hard deny 只承诺覆盖 Runtime 能稳定确定的宿主资源：`~/.ssh`、`~/.aws`、`~/.gnupg`、CodeAgent 的 provider credential 路径，以及当前项目已有规则中能够映射到明确绝对宿主路径的资源。只读 root 仍会暴露 Unix socket，因此同时遮罩 `/run/user/<uid>`、system/session D-Bus、SSH agent、Docker/containerd 等高权限 socket，并移除相关环境变量。清单来自 Runtime 内置可信配置，不能由仓库扩大或缩小。

不为 `.env`、`secret`、`token`、`credential` 等 repo-local 名称建立新的通用 OS hard-deny 保证，也不扫描整个 Candidate writable tree寻找这类名称。现有 Search/Read、Atomic Edit 的 `SensitivePath` 规则仍约束对应结构化工具，但不能据此声称任意 sandboxed command看不到同名仓库文件。Candidate 本来就是命令的工作输入；如果仓库含真实秘密，用户仍须在运行不可信代码前移除或隔离它们。

对已存在宿主敏感目录使用 Runtime-owned 空目录只读覆盖，对已存在文件使用空文件覆盖。Bubblewrap/Linux bind mask不能对尚不存在、稍后才出现的路径提供通用未来名称 deny；本阶段不为此增加 glob扫描、fanotify或复杂文件系统监控。这一限制必须进入用户安全边界说明。

本项目借鉴 [Anthropic Sandbox Runtime](https://github.com/anthropic-experimental/sandbox-runtime/blob/main/README.md) 的“读默认允许、写默认拒绝”，同时以有限、确定的宿主敏感根作为额外 hard deny；不宣称拥有泛化 secret classifier。

### 9.3 Symlink and path race

- requested path 可以包含正常 symlink。Runtime 安全解析 canonical target，Policy 和 Grant 绑定 resolved path，Approval 同时展示 requested path 与 resolved path。
- 只有路径无法稳定解析、出现 symlink loop、resolved target跨入 hard-deny boundary，或审批后资源 identity变化时才拒绝。
- 对已存在文件/目录记录 resolved target 的类型、设备、inode；spawn 前重新解析 requested path，并复检它仍指向同一 target identity。
- Candidate、source、Git dir、Session dir同样复检 identity；mount source使用 canonical absolute path，sandbox destination也使用 canonical path。命令里的 requested alias仍可经 root只读视图访问，但实际授权边界以 resolved target为准。
- identity变化返回 `permission_resource_changed`，不自动扩权或重试。

本威胁模型隔离 sandboxed command，不防御同时控制 Runtime 宿主账户的对手。

## 10. Permission Model

文件资源申请使用以下闭合结构；NETWORK 分支不接受 `resource`：

```json
{
  "capability": "filesystem_write",
  "resource": "../shared-cache",
  "reason": "构建器需要复用宿主缓存",
  "scope": "once"
}
```

```text
Capability
├── FILESYSTEM_READ(canonical path)
├── FILESYSTEM_WRITE(canonical path)
└── NETWORK

PolicyDecision = ALLOW | ASK | DENY
PermissionScope = ONCE | SESSION
```

- `resource` 是非空路径字符串、禁止 NUL。absolute path直接解析；relative path相对 active Candidate root解析，而不是相对 shell `cwd`。审批始终展示原始值和 canonical resolved path。
- 已存在资源先跟随 symlink安全解析。文件 grant只覆盖该文件本身，不授予父目录的 create/delete/rename；目录 grant递归覆盖当时和以后位于该 canonical目录下的子路径。
- WRITE implies READ。READ 对不存在资源无意义，返回 `permission_resource_not_found`。
- Bubblewrap无法把一个尚不存在文件单独 bind为可创建目标。若 WRITE resource不存在，Runtime 找到最近的已存在 canonical父目录，把请求规范化为该父目录的递归 WRITE request；Policy重新检查扩大后的范围。Approval必须同时显示 requested target、effective parent directory和“可创建/修改该目录全部后代”的实际授权。若父目录是 `/`、跨 hard deny、无法稳定解析或扩大范围被Policy拒绝，则请求DENY。Runtime不得悄悄授予该父目录。
- `FILESYSTEM_READ` 用于基础策略软隐藏、可申请的用户目录；普通 root 只读已 ALLOW，hard-sensitive 永远 DENY。
- Candidate、sandbox `/tmp` 和 Runtime cache 写为基础 ALLOW。
- Candidate 外写默认 ASK；source、Git metadata、Session state、sensitive 路径 hard DENY。
- NETWORK 默认 ASK；授权后为 `NETWORK_HOST`。

优先级：`hard deny > 更具体保护覆盖 > explicit grant > base allow/read-only > default deny`。

Base Policy 位于 Runtime 版本化可信代码/配置，不写入仓库。Session grants 紧凑保存在 `session.json`；申请/审批/使用历史进入 `events.jsonl`，不新增 artifact。ONCE grant 只绑定一个 `execution_id`，执行或终止后失效；SESSION grant 绑定 session、canonical resource、capability、批准记录和 policy revision，resume 复检 identity 后才生效。

Runtime 只校验和规范化 Agent 已提交的 Permission Request；它不尝试从 shell AST、命令名字或工具链知识补全申请。没有申请不等于 Policy DENY：若命令不显式请求额外 capability，它可以直接在 Base + 已有 Session grants下运行并正常失败。

## 11. Policy + Approval Flow

Permission Request 由 Agent 作为 `run_command.permissions` 显式产生，包含 capability、文件 resource（NETWORK 无 resource）、reason、`once | session`。Runtime 不负责发现遗漏需求，也不因观察到一次 `Permission denied` 就代替 Agent申请权限。

1. Runtime 冻结 `execution_id + command + cwd + timeout + purpose + permission requests`。
2. Policy 对每项返回 ALLOW/ASK/DENY；DENY 立即结束，Approval 不能越过。
3. 所有 ASK 合并为一次审批，显示完整命令、cwd、Candidate、资源、scope 和网络变化。
4. 首版只允许整体批准或拒绝；更小权限由 Agent 提交新请求。
5. SESSION grant 先原子写 Session state再执行；保存失败不执行。
6. 用 Base、已有 Session grants、本次 ONCE grants 重新计算有效策略。
7. 复检原 request 和资源 identity，命令恰好执行一次。

批准后“执行原 command”是同一冻结请求的首次执行，不是先失败再自动重放。若未申请 capability 的命令已经运行并失败，Agent只能提交新的 command request；新 request拥有新的 `execution_id`，并可能申请更多权限。Runtime不自动重试，也不假设第一次失败没有留下 Candidate changes。

## 12. Network Model

- `NETWORK_OFF`：默认 `--unshare-net`。Bubblewrap 文档称新 network namespace 仅含 loopback device；Runtime 不主动启用/配置 loopback，因此不承诺本地 TCP 可用，也不 bind Unix socket。
- `NETWORK_HOST`：批准 NETWORK 后共享 WSL 当前 network namespace，使用当前 DNS 与 WSL2 到 Windows/Internet 的现有连通性。CodeAgent 不重建 NAT 或 mirrored networking。

HOST 是粗粒度高风险授权，可访问公网、局域网和宿主服务，也可能外传所有可读内容。审批必须明示。本阶段不伪装成 domain allowlist；未来受限联网需独立代理设计。

## 13. Worktree Integration

- Session 初始 source-only。首次 `run_command` 与 edit 一样先批准并创建 Session Candidate；命令绝不在 source 执行。
- active Candidate 是唯一默认可写代码目录。
- source 即使位于写 grant 父路径内，也最终重新 mount 只读。
- Candidate `.git`、解析后的 common git dir、`.codeagent` 和 Runtime metadata 最终只读/隐藏。任意 shell 可运行 Git 读命令；Git 写会因 metadata 只读失败。

### 13.1 Candidate change sources

Candidate 内有两种同等合法的修改来源：

- **Atomic Edit changes**：由 `apply_workspace_edit` 的 prepare/transaction/rollback管理，具有逐文件 expected hash和 edit receipt。
- **Sandboxed Command changes**：由已完成启动的 `run_command` 在 Candidate writable root内产生，例如 formatter、codegen、lockfile、generated source，或失败/timeout前留下的部分修改。它们没有 Atomic Edit transaction，但由 command前后 workspace audit建立来源边界。

两者都属于用户可查看、继续编辑、验证、采纳或丢弃的 Candidate changes。Runtime不因修改“不是 Atomic Edit产生”而自动 taint。Atomic Edit在之后读取command生成文件时仍使用正常完整SHA前置条件；accept从最终Candidate Git diff生成统一patch，不按来源拆分或只采纳Atomic Edit部分。

Session增加统一单调 `candidate_revision`：每次成功Atomic Edit提交后递增；payload实际启动的command在完成审计且检测到workspace tree变化时递增，无论exit为0、非0或timeout。没有改变subject tree的只读或utility command不推进revision，也不使已有validation evidence失效。command完成事件记录其`before_candidate_revision`和`after_candidate_revision`。

### 13.2 Command workspace audit

Runtime在spawn前必须先成功取得可信before snapshot，否则不执行并返回`workspace_audit_unavailable`。命令结束且进程树确认停止后取得after snapshot，至少包含：

- HEAD/base identity；
- porcelain status（tracked与非ignored untracked）；
- 当前Git-visible文件的内容fingerprint和subject tree；
- before/after差异形成的`command_induced_changes`，区分create/update/delete/rename；
- ignored residue只记录Runtime能够低成本识别的摘要，不声称完整逐文件归因。

`command_induced_changes`是审计来源信息，不是污染标记。失败命令留下的可确认变化同样进入当前修改，并返回给Agent检查。

### 13.3 Tainted and recovery states

`workspace_tainted`重新定义为：命令已可能修改Candidate，但Runtime无法建立完整、可信的after boundary；典型情况是after Git audit失败、结果互相矛盾，或命令结束后发现不能归因的Git-visible并发变化。此时禁止后续edit/command/validation/accept，只允许只读检查和discard，因为Runtime仍可尝试回到accepted baseline。

`recovery_required`用于更强的不确定性：无法确认命令进程树已经停止、Candidate/source/Git metadata identity被破坏、sandbox保护边界疑似失效，或discard/rollback后无法证明accepted baseline完整恢复。它具有最高优先级并fail closed。

command exit非0、timeout后确认进程树已停止、合法写Candidate、尝试写只读路径但未成功，都不会单独触发tainted/recovery。

### 13.4 Accept, discard and validation

- **accept**：消费Candidate相对accepted baseline的最终Git patch，包含Atomic Edit和command产生的所有Git-visible变化；不交付ignored residue。要求workspace非tainted/recovery，并满足第19节的current validation evidence invariant。
- **discard**：恢复accepted baseline并用`git clean -fdx`移除tracked、untracked和ignored residue；复检HEAD/status/tree。tainted也允许discard；复检失败转`recovery_required`。
- **validation**：可以针对任意来源组合后的当前Candidate运行。验证命令自身若产生可确认的workspace tree变化，先推进`candidate_revision`，成功evidence绑定命令结束后的revision和subject tree；后续Atomic Edit或改变tree的command会使它stale。

真实build/test常创建ignored的`target/`、cache等。它们不进入交付patch，审计也不承诺完整逐文件列举；discard负责清理。validation currentness以subject tree、workspace/base identity等正式绑定为准，不把“运行过另一条命令”本身当成源码变化。

## 14. Path Semantics

| 层 | 路径 | 用途 |
| --- | --- | --- |
| Agent API | repository-relative | `cwd`，相对 active Candidate |
| Policy/Audit/Approval | canonical Runtime absolute | capability、mount、日志、展示 |
| source identity | canonical source absolute | hard deny write、accept 目标 |

Bubblewrap identity mapping：host `/home/u/.../worktrees/s1` 在 sandbox 仍为同一路径，traceback、编译诊断、审批无需翻译 `/workspace`。

Permission Request 文件 resource 使用 absolute path；相对值先相对 Candidate 解析并在审批显示 canonical path。`~` 不在 schema 隐式展开，避免 HOME 解释差异。

## 15. Sandbox Violation / Error Model

| code | 位置 | 含义 |
| --- | --- | --- |
| `permission_denied` | Policy | hard deny 或资源未获准 |
| `approval_denied` | Approval | 用户拒绝 ASK |
| `permission_resource_changed` | spawn 前 | 资源 identity 变化 |
| `sandbox_unavailable` | probe | bwrap/namespace/WSL 不支持 |
| `sandbox_setup_failed` | payload 前 | mount/namespace/chdir/shell exec 失败 |
| `sandbox_violation` | 有确定证据时 | capability/resource/network 冲突 |
| `execution_timed_out` | supervisor | 超时并终止树 |
| `command_failed` | payload | shell 启动后非零退出 |

Backend 使用小型 Runtime-owned launcher/状态 fd，在 payload exec 前后报告状态，区分构造失败与普通非零退出，不解析 stderr 猜测。

Bubblewrap mount/network enforcement 没有通用“哪个 syscall 因哪条规则失败”事件流。payload 的 `EACCES`、`EROFS`、`ENOENT`、DNS/connect failure 也可能来自真实 mode、路径或程序配置。因此首版不把 stderr 冒充 `SandboxViolation`。普通失败附有效 write roots、被拒权限、network mode及申请权限提示。

该提示不是自动生成的 Permission Request，也不表示 Runtime已经判断出缺少哪项权限。Agent结合任务上下文和diagnostics决定是否提交新的request；旧execution不会被重放。若失败前已经修改Candidate，after audit照常记录这些合法command-induced changes。

## 16. Process / Resource Isolation

固定启用 user、PID、IPC、UTS namespace，禁止沙盒内继续创建 user namespace，使用新的 `/proc`、最小 `/dev`、`--new-session`、`--die-with-parent`；不暴露 host PID。Bubblewrap 在 PID namespace 运行最小 PID 1回收子进程；官方也要求 `--new-session` 防 TTY escape。

首阶段实现：

- timeout，先 TERM、宽限后 KILL Bubblewrap process/session；
- PID namespace和父进程死亡联动；
- stdout/stderr 流式落盘，ToolResult 有界 head/tail，单命令/Session诊断限额；
- `/tmp` tmpfs 大小上限；
- stdin禁用、无TTY、关闭继承 fd；
- `no_new_privs` 与 capability drop（由 probe 证明）。

首阶段不做精确 CPU、memory、总磁盘和进程数配额。namespace 不防 fork bomb；宿主同 uid 的 `RLIMIT_NPROC` 可能影响其他进程，不能当可靠 per-command 限制。需求出现后评估 cgroup v2。

## 17. Audit Model

每个 execution 在现有 events 中保留请求/审批摘要与完成结果：exact command（有界）、cwd/purpose/timeout、workspace/base/candidate revision与tree identity、Base Policy revision、有效读写/deny摘要、network mode、grants/decision/approval/scope、时间/exit/signal/timeout、输出摘要或诊断引用、可靠 violation，以及before/after workspace audit。

workspace audit至少记录`before_candidate_revision`、`after_candidate_revision`、before/after subject tree、Git-visible `command_induced_changes`及其create/update/delete/rename分类、audit completeness和最终workspace state。变化来自command是正常provenance（来源）信息；只有audit无法建立可信after boundary时才记录tainted，保护identity或恢复无法确认时记录recovery。

SESSION grants 当前权威状态只在 `session.json`；events 保存历史。成功且无 taint 的命令清理完整输出和 runtime；失败、timeout、sandbox setup、taint、recovery保留有界 diagnostics。不新增 `grants.jsonl`、`commands.jsonl` 或长期 sandbox目录。

exact command 可能含用户手写 secret，首版无法可靠脱敏。UI/文档须警告不要把 secret 写进命令；credential broker 不在范围。

## 18. Existing Code Migration

保留并重构：

- `AgentRunner` / `ToolRegistry`：注册 `run_command`，lazy worktree 后转交同一冻结请求。
- `WorkspaceContext` / `GitWorktreeManager`：保留 Candidate/source 与 resume gate。
- `ApprovalGate`：输入变为 permission bundle，支持 ONCE/SESSION。
- `SessionStore`：增加紧凑 Session grants和事件，不新增目录。
- 当前以Atomic Edit为中心的`edit_revision`有效性判断迁移为统一`candidate_revision`；Atomic Edit和产生workspace tree变化的command共同推进它。
- `CommandSpec/Result/Receipt`：改为 arbitrary command、有效策略、before/after candidate revision和command-induced changes语义。
- `CommandArtifactStore`：继续管理临时数据与失败诊断。
- workspace audit重构为合法change provenance；tainted/recovery按第13节新语义保留；accept/discard统一处理Atomic Edit与command changes。

新增建议：

- `runtime/permissions.py`：capability/request/grant/decision/effective policy。
- `runtime/sandbox_policy.py`：Base Policy、hard deny、grants合并、symlink canonical target和路径复检。
- `runtime/bubblewrap.py`：probe、mount plan、argv翻译。
- `runtime/sandbox_executor.py`：进程监督、状态fd、timeout、输出、错误分类。
- `tools/command.py`：闭合 `run_command` schema。

删除：

- `runtime/profiles.py`、`ProfileRegistry`、`CommandProfile`。
- Agent-facing `run_validation`。
- `PYTEST_ARGV_PREFIX` 与 pytest-only executor校验。
- `CommandPolicy` 中命令名/target/argv allowlist。
- 以“Agent不能传 shell”为安全目标的旧测试。

`runtime/validation.py` 若保留，只承担与命令集合无关的验证记录和 current evidence 查询。

## 19. Command Profile Removal Plan

不兼容旧 Session profile evidence；切换时不同时注册 `run_validation` 和 `run_command`。

| 旧职责 | 新归属 |
| --- | --- |
| 命令 allowlist / argv 编译 | 删除；shell任意 |
| 参数 schema | `run_command` schema |
| timeout | 全局 `CommandLimits` |
| environment revision | Base Sandbox Policy revision |
| audit classification | `purpose` + execution event |
| validation kind | `purpose=validation` 成功记录 |
| profile revision 防 stale | command hash + policy revision + candidate revision + subject tree |
| executable identity | 固定 bwrap/bash probe与复检；内部工具由 sandbox PATH解析 |
| pytest target校验 | 删除；pytest解释 shell arguments |

验证记录要求：`purpose=validation`、payload启动、exit 0、after audit完整且workspace非tainted/recovery，并绑定workspace revision、base commit、命令结束后的candidate revision、subject tree、command SHA-256和policy revision。后续Atomic Edit或任意command使旧记录stale。

这条记录的精确语义只有：“Agent或用户选择的一条验证命令，针对所记录的Candidate状态，在所记录的sandbox policy下成功执行。”它不保证命令充分、有意义、覆盖需求或一定是pytest。accept继续要求至少一条current validation evidence，这是**evidence-exists invariant（存在当前验证证据的不变量）**，不是validation-quality guarantee（验证质量保证）。accept确认界面显示exact command，由用户判断其意义；不会因为`true`也能成功而恢复命令allowlist或classifier。

## 20. Security Boundaries and Known Limitations

- Bubblewrap 是低层机制，安全性取决于 Runtime mount/namespace plan。
- broad read + 有限宿主敏感根不能证明覆盖所有私人数据；repo-local `.env`/secret-like名称不获得通用OS hard deny，NETWORK_HOST风险显著。
- 不防御同宿主账户的恶意并发进程。
- 宿主工具与只读配置不保证可复现；identity mapping优先兼容性。
- `/dev`、`/proc`、Unix socket和设备必须最小暴露；不 bind D-Bus、SSH agent、Docker socket。
- bind mask只保护构造sandbox时可解析的既有路径，不能对未来才出现的同名敏感路径作完整保证。
- ignored build residue不进 patch且无法完整归因；discard清理，但可影响后续命令。
- 不能可靠分类 payload每个权限错误。
- 无 cgroup，fork/memory/CPU exhaustion仍是风险。
- AppArmor、容器或 WSL1可能禁止 user namespace；必须 fail closed，不提供弱化模式。

## 21. Testing Strategy

不接真实 LLM；Fake Model 只验证正式 ToolRegistry/AgentRunner。

### Schema and domain

- command/cwd/timeout/purpose/permissions合法与非法组合；
- pipeline/redirection/quotes/env/chaining不被重写；
- filesystem resource的absolute/relative解析、requested/resolved双展示、symlink canonical target、文件精确grant、目录递归grant、write-implies-read、hard deny优先级；
- 不存在READ拒绝；不存在WRITE规范化为最近既有父目录、重新Policy并明确审批扩大范围；跨hard deny、loop、identity变化拒绝；
- NETWORK无resource，文件capability必须有resource；Runtime不从command或stderr推断Permission Request；
- ALLOW/ASK/DENY、ONCE一次、SESSION persist/resume；
- 审批前后 request/hash/resource identity一致。

### Bubblewrap integration

- missing/unsupported bwrap、userns失败明确 fail closed；
- SDK可读、Candidate可写、source/其他 host不可写；
- `.git`/gitdir/Session及明确宿主敏感根/socket不可读写；repo-local名称不被误宣称为通用hard deny；
- 合法symlink解析后授权，loop、跨hard-deny symlink、parent replacement、escape和nested mount precedence拒绝；
- 已存在path bind-mask有效，并证明未来不存在路径不在该保证内；Candidate不执行通用secret-name scan；
- private `/tmp` 和 identity cwd/traceback；
- NETWORK_OFF连接失败，NETWORK_HOST批准后访问测试本地服务；
- PID隔离、后台子进程回收；
- shell语义、exit/signal、stdout/stderr、timeout完整。

### Lifecycle and safety

- source-only首次 command组合审批、upgrade、原命令恰好一次；
- ASK/DENY不spawn；SESSION不重复问，ONCE下次重问；
- Atomic Edit与成功/失败/timeout command产生的Git-visible源码、lockfile、formatter/codegen变化都成为合法Candidate changes，可继续edit/command/validation/accept；
- command before/after audit正确记录create/update/delete/rename、revision和subject tree；只有tree变化才推进candidate revision并按正式identity判断旧validation是否stale；
- before audit失败不spawn；after audit无法建立可信边界才taint；进程树、保护identity或恢复无法确认才recovery；
- discard清除 tracked/untracked/ignored并复检，失败 recovery；
- resume recovery gate优先于 grants；
- validation证据只证明所选命令成功；accept测试只断言current evidence存在，不断言pytest或命令质量，也不引入`true` classifier；
- 漏报permission的命令普通失败、可能留下合法Candidate changes；Runtime返回policy摘要但不自动申请或重放，Fake Model以新execution显式申请后再运行；
- 正常清理、异常有界诊断；
- Fake Model完成 read → edit → arbitrary validation → accept-ready。

### Adversarial fixtures

- `../../`、symlink、rename protected mount、改 `.git`、读凭据、写 source；
- 后台 fork、忽略 TERM、输出洪泛、shell元字符；
- 尝试 D-Bus、SSH agent、Docker socket、nested user namespace；
- 普通 `ENOENT/EACCES` 不误报确定 violation。

完成标准：新增测试、全量 pytest、compileall、`git diff --check`，以及正式 CLI 在临时仓库 dogfood。Python/Node/Rust 至少选本机已有的两类工具链验证；缺工具链不得自动安装。

## 22. Implementation Plan

1. **平台 probe 与威胁 fixture**：以feature probe而非仅版本号冻结WSL2/userns要求和安装错误；当前机器先安装Bubblewrap才可完成集成验收。
2. **领域模型**：CommandRequest、完整文件resource schema、capability、grant、Base/Effective Policy、symlink canonical target和Session grants。
3. **Candidate revision与audit**：统一Atomic Edit/command change provenance、before/after snapshot、tainted/recovery新语义，以及accept/discard边界。
4. **Bubblewrap backend**：root RO、Candidate/cache RW、source/Git/确定宿主敏感根保护、identity、namespace、`/tmp`；不实现Candidate通用secret scan。
5. **进程监督**：固定bash、状态fd、timeout/tree、输出限制、错误分类。
6. **Policy + Approval**：ONCE/SESSION、permission bundle、Agent显式发现责任、原请求单次转交且不自动重放、audit。
7. **Agent工具切换**：注册`run_command`，删除`run_validation`/ProfileRegistry/pytest-only路径。
8. **Validation/Candidate迁移**：evidence-exists语义、purpose、command hash、policy/candidate revision、subject tree和accept gate。
9. **持久化收敛**：复用events/session/diagnostics，删除旧profile字段。
10. **集成与dogfood**：完成第21节并同步ARCHITECTURE、TESTING、MANUAL_TEST、README。

内部函数、类型和文件拆分可调整。若要允许 source写、允许审批越过 hard deny、提供无bwrap fallback、默认联网、让项目文件自授权、取消 tainted/recovery gate或改变 accept验证要求，必须重新人工 review。

## 23. Research Conclusions

直接借鉴：Bubblewrap的一次性 namespace、root RO + writable bind、PID/network模型；Codex的 root ro-bind、保护子路径重叠和WSL2检查；Anthropic的读默认开放、写 allow-only与Linux路径经验。

不引入：Codex bundled bwrap/Landlock/代理与复杂seccomp；Anthropic domain proxy、跨平台产品面和 weaker nested sandbox；两者的长期兼容矩阵。

调研证明 Bubblewrap只是 enforcement primitive，Policy仍由 CodeAgent定义；Linux bind-mount deny 对symlink、不存在路径和嵌套写根有真实陷阱，所以 mount plan必须是独立、纯输入可测试组件，不能散落在 subprocess字符串拼接中。

## 24. Review Summary

本轮 review 提出的 change provenance、Permission Request 责任、sensitive 范围、symlink 解析、validation 语义和 resource schema 已写入目标契约，并在后续 review 后完成实现。本节保留当时的关键 invariant，顶部状态和实现说明代表当前事实。

修改后的关键invariant：

- 只提供 shell-string `run_command`；宿主 `shell=False`，shell只在Bubblewrap内解释。
- Bubblewrap缺失/能力不足关闭 command，不降级。
- Base Policy在可信Runtime；SESSION grants在`session.json`；ONCE绑定execution。
- Atomic Edit和sandboxed command都能产生合法Candidate changes；来源进入audit，不决定能否accept。
- 只有after boundary不可信才tainted；进程、identity或恢复无法确认才recovery。
- Permission Request只能由Agent显式提交；Runtime不推断、不从stderr生成、不自动重放。
- root宽读、有限且确定的宿主敏感hard deny、Candidate最小写、source/Git/Session硬保护、默认断网；不承诺repo-local secret-name分类。
- symlink正常解析到canonical target，Approval同时展示requested/resolved并在spawn前复检。
- NETWORK只有OFF/HOST；PID隔离固定。
- 确定的Policy/构造错误结构化，payload内部权限错误不伪造归因。
- Command Profile整体删除；current validation只提供evidence-exists invariant，不保证质量。
- 不新增artifact家族，不提前引入Docker/cgroup/proxy。

实现期确认项的当前结果：

- 当前 Linux/WSL2 环境已经安装 `/usr/bin/bwrap`，并通过可信路径 probe、namespace smoke test 和正式 CLI dogfood；其他部署环境仍须各自 probe，不以版本字符串代替能力检查。
- `/tmp`、输出和 diagnostics 的具体字节上限已由实现常量与测试冻结。
- 宿主敏感根和 socket 清单已集中在 Runtime policy 中；没有扩展成 repo 文件名扫描器。

上述项目没有遗留 implementation-blocking open question。未来若改变 hard deny、网络、source 写保护或无沙盒 fallback 等产品边界，仍须重新 review。
