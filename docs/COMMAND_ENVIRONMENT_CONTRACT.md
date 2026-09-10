# Command Environment Contract v1

状态：**Environment v1 已实现；第 11 节扩展项仍未实现**
适用范围：本地 Linux / WSL2 Product Runtime 的 `run_command`；SWE-bench backend 继续使用其显式准备的容器环境。

## 1. 决策摘要

CodeAgent 不增加 Python、Node、Java 等语言枚举，不扫描项目后替 Agent 选择解释器，也不解析 shell string
判断 pytest、pip、npm 或其他命令语义。环境支持继续建立在已有的受控任意命令之上：Runtime 提供真实、
有界的环境事实，Agent 使用 `run_command` 自主探索和纠错；新增网络或 Candidate 外资源仍经过既有
Policy、Approval、Bubblewrap 和 workspace audit。

本契约区分三个对象：

| 对象 | 含义 | 不应混淆为 |
| --- | --- | --- |
| Runtime environment | 运行 CodeAgent/RPC 的 Python 进程环境 | 目标项目依赖环境 |
| Command environment | 某一次 `run_command` 实际获得的 cwd、PATH、变量、文件系统和网络 | 持久 shell session |
| Sandbox policy | 限制命令可访问的资源 | 安装工具或提供依赖的机制 |

沙盒回答“命令能访问什么”；Command environment 回答“命令从什么初始进程状态运行”。允许访问项目并不
保证 pytest 存在，pytest 存在也不代表命令获准联网或写入 source。

## 2. 本轮 dogfood 的事实结论

Session `17619bad5c21` 的 Runtime 使用 CodeAgent 开发 `.venv`，但 demo 仓库本身没有准备 pytest 环境。
以绝对路径启动 Runtime Python 不会激活虚拟环境，也不会自动把其 `bin` 加入 `PATH`。沙盒随后准确返回：

```text
python: command not found
/usr/bin/python3: No module named pytest
```

Agent 根据错误改用 `python3 -c` 断言并成功完成替代验证。因此：

- 失败不是 Bubblewrap 禁止 pytest，也不是 stderr 缺失；
- demo 没有提供其宣称需要的测试环境；
- Runtime Snapshot 没有说明最终 PATH、fresh-shell 语义以及 Runtime Python 与项目命令环境的分离；
- Agent 已证明能在明确失败后自行纠正，但 UI/最终回答必须诚实区分“运行 pytest”和“替代验证”。

## 3. 非目标与禁止项

本轮不得实现：

- `PythonEnvironmentDetector`、`NodeEnvironmentDetector` 或其他语言/框架枚举；
- 根据文件名、命令文本或 exit code 在 Runtime 内猜测下一条命令；
- Runtime 自动改写 Agent 的 shell string、自动重试或偷偷安装依赖；
- 因发现 `.venv`、`setup.sh`、`Makefile` 等文件而自动执行它们；
- 默认把 CodeAgent 自己的 `.venv` 当作目标仓库环境；
- 为通过 demo 绕过网络 Approval、source 只读、ToolRegistry 或 workspace audit；
- 把一次 shell 内的 `export`/`source` 伪装成跨命令持久状态；
- 在 Event、Session metadata 或模型 Context 中保存 secret 值。

Runtime 可以描述可验证的进程事实，但不得把这些事实提升为“推荐解释器”或“正确测试命令”。

## 4. 命令生命周期与持久性

每次 `run_command` 都创建独立 Bubblewrap 和全新的：

```text
/bin/bash --noprofile --norc -c <agent command>
```

下列状态只在该命令进程树内存在，命令结束后消失：

- `export` 修改；
- `source`/activate 造成的 shell 状态；
- shell function、alias 和当前 shell 的 cwd；
- `once` 权限 grant。

下列事实可以跨命令存在：

- Candidate 或 Runtime cache 中实际创建的文件；
- Session metadata 中明确保存的、非 secret 配置；
- `scope=session` 且仍满足 Policy 的权限 grant；
- 每次重新构造得到的同版本环境策略。

因此 Agent 可以在同一条命令中使用：

```bash
source /known/environment/activate && project-test-command
```

也可以探索后直接使用绝对 executable。Runtime 不判断哪种形式更正确。

## 5. Environment v1 的来源与优先级

最终 Command environment 必须由可追踪的通用来源确定，后者覆盖前者：

1. Runtime 的安全默认值；
2. 从 Extension/CLI 启动进程安全继承的变量；
3. execution backend 显式提供的环境（例如 SWE-bench prepared container）；
4. 未来经用户明确提供或批准的 Session environment overlay。

Environment v1 只实现 1～3 和事实暴露，不同时实现 Session overlay。只有 dogfood 证明 Agent 被迫在大量命令中
重复相同环境前缀后，才单独设计第 4 项。

允许继承的基础集合保持有界：

```text
PATH, LANG, LC_ALL, LC_CTYPE, TZ, TERM
```

Runtime 继续覆盖：

```text
HOME=<private runtime home>
TMPDIR=/tmp
XDG_CACHE_HOME=<private runtime cache>
PYTHONIOENCODING=utf-8
PYTHONDONTWRITEBYTECODE=1
```

禁止默认继承 provider key、云凭据、SSH agent、cookie、token、任意未知变量及高权限 socket。显式调用某个
绝对 executable 不会使其父环境自动进入 PATH。PATH 条目在 sandbox 内不可见或不可执行时，必须保留真实
失败，不静默删除后伪称环境一致。

这一策略参考成熟 Agent 的通用 shell environment policy：环境通过继承、过滤和显式覆盖形成，而不是由
语言检测器决定。参考 [OpenAI Codex configuration reference](https://developers.openai.com/codex/config-reference/)
与 [Codex cloud environments](https://developers.openai.com/codex/cloud/environments/)。CodeAgent 仍以自身
Safety/Approval 约束为准，不照搬其实现细节。

## 6. 提供给 Agent 的权威环境事实

每次构建 Context 时，Runtime Snapshot 必须提供当前有效事实，而不是历史推测：

```json
{
  "execution_environment": {
    "contract_revision": "command-environment-v1",
    "backend": "bubblewrap",
    "shell": "/bin/bash --noprofile --norc",
    "default_cwd": ".",
    "default_cwd_resolves_to": "/absolute/active-workspace",
    "path": ["/usr/local/bin", "/usr/bin", "/bin"],
    "path_source": "filtered_host_environment",
    "home_kind": "private_runtime_home",
    "tilde_is_host_home": false,
    "tmp_kind": "private_tmp",
    "network_mode": "off",
    "commands_are_fresh_processes": true,
    "shell_state_persists": false,
    "source_workspace_access": "read_only",
    "active_workspace_access": "read_write"
  }
}
```

约束：

- `path` 是本次命令实际使用的有序 PATH，不是扫描得到的 executable 列表；
- 不列举 Python/Node/pytest 是否存在；Agent 可用任意命令自行探测；
- backend executable 不可用时如实返回 unavailable，不假装存在 fallback；Snapshot 的
  `executable_available` 只表示可信 executable 可发现，不承诺 namespace probe 或 payload 必然成功；
- network grant 只改变获批命令的 effective policy；下一次 Snapshot 仍显示默认网络状态，除非存在有效的
  Session grant；
- Snapshot 不包含环境变量 secret value。

## 7. 命令失败与 Agent 自行纠正

`run_command` 结果继续使用通用结构，不增加语言专用错误码。至少保留：

- 原始 command 与实际 cwd；
- payload 是否启动；
- status、exit code、timeout；
- 有界 stdout/stderr；
- effective environment contract revision；
- environment variable names；
- effective network/filesystem policy 摘要。

Runtime 不因 `command not found`、module missing 或其他错误自动推荐命令。Agent 应通过现有工具继续执行
`pwd`、`env`、`command -v`、文件读取或项目自带命令来探索，并根据 Observation 自主选择下一步。

Prompt 只增加行为约束：不要假设工具存在；需要时先探测；每条命令是 fresh shell；如果用户要求的验证无法
运行，必须明确报告失败及替代验证，不能把替代验证表述成原验证已通过。

## 8. 环境准备与 Approval

CodeAgent 不新增绕过 `run_command` 的安装器。若用户任务确实需要，Agent 可以通过普通命令在允许位置创建
环境或安装依赖：

```text
Agent 形成 command + permissions
→ ToolRegistry
→ SandboxPolicy
→ network/filesystem ASK（如需要）
→ 用户 Allow/Reject
→ Bubblewrap 执行
→ before/after workspace audit
→ ToolResult 返回 Agent
```

规则：

- Candidate 内写入沿用现有策略；Candidate 外写入必须申请对应资源；
- 下载依赖必须申请 network，优先使用 `scope=once`；
- network 当前意味着 host network，不承诺域名/端口限制，Approval 必须展示这一事实；
- 用户未要求执行、测试或环境准备时，Agent 不得仅为便利安装依赖；
- 仓库内 setup script 视为不可信普通命令，不能因约定文件名自动执行；
- 创建的大型、ignored 环境目录可能影响 audit、Candidate、Accept/Discard 和磁盘，未完成专项设计前不把
  “自动在 Candidate 创建环境”声明为正式产品保证。

## 9. Demo 与 dogfood 调整

现有 Product demo 的“run focused pytest”与实际 fixture 不一致。实现阶段必须拆分：

### 9.1 离线产品主链路

- fixture 自带不需要下载的确定性验证入口；
- 验证 Extension、Approval、worktree、编辑、命令、Diff、Accept/Discard；
- 不借用 CodeAgent Runtime `.venv`；
- 文档只宣称实际运行的验证。

### 9.2 环境与网络专项 dogfood

- fixture 明确缺少一个依赖，并提供可信的目标验证方式；
- Agent 先观察 Environment Snapshot，再用任意命令探索；
- Agent 自行形成环境准备/安装命令并申请 `network once`；
- 分别测试 Reject 和 Allow；
- Allow 后运行用户要求的真实验证，不用替代断言冒充；
- 记录实际 command、PATH、effective network、cwd、exit code 和 validation identity；
- 不把外层平台禁止 nested Bubblewrap 归类为 Agent 或依赖失败。

## 10. 实现结果与完成标准

已完成：

1. 增加 `EffectiveCommandEnvironment` 数据模型与安全测试；
2. 从实际 sandbox environment 构造 DTO，并由 executor 使用其 process environment；
3. 通过当前 `CommandService` 把同一构造路径投影到 Runtime Snapshot；
4. 为命令结果/Event 增加有界 provenance，保持旧 Event 兼容；
5. 更新 prompt，引导 Agent 探索、纠错和诚实报告验证退化；
6. 修正离线 demo，并增加独立 environment/network dogfood fixture；
7. Approval UI 明确区分 workspace upgrade 与 command permissions，并直接显示 network scope；完整环境信息的
   常驻展示仍留待独立设计。

2026-09-06 已在 Extension Development Host 使用 DeepSeek 完成 environment/network Allow 与 Reject dogfood：
Allow 路径在 Candidate 的 ignored `.project-env` 安装精确依赖并完成目标验证，没有同步环境目录到 source；
Reject 路径没有执行安装、没有留下 grant 或 validation evidence，Agent 未修改 tracked source 并如实停止。
正式发布仍必须针对冻结 commit 重跑，不能以 Fake Model 或旧开发工作区记录代替。

完成标准：

- 没有语言、包管理器或测试框架枚举；
- Runtime 不解析 Agent command 的业务语义；
- `utility` 使用普通 Bash；只有显式 `purpose=validation` 使用
  `/bin/bash --noprofile --norc -o pipefail -c`，本地 Bubblewrap 与 SWE-bench Docker 语义一致；
- 因此 `pytest | tee report.log` 的前段失败会返回非零且不能形成 validation evidence；Runtime 不自动改写 purpose，
  只为高置信度测试型 utility 命令记录诊断标志；
- Snapshot 描述与 executor 实际环境来自同一构造结果；
- Agent 能通过任意命令发现环境、看到完整失败并继续纠正；
- 网络/额外写入仍经 Approval，Reject 不产生隐式 grant 或自动重试；
- secret 不进入 Context、Event、Output 或 Session metadata；
- 离线 demo 与网络专项 dogfood 分开，结论不越过实际证据；
- L0、真实 Bubblewrap、VS Code 真实模型 dogfood 分层报告。

## 11. 后续仍需单独决策

下列问题不在 Environment v1 中预先实现：

- Session environment overlay 的 schema、授权和修改时机；
- repo-local setup script 的信任与重放模型；
- ignored 大型环境目录应位于 Candidate、Runtime cache 还是外部受控目录；
- domain/port 级网络授权；
- UI Environment disclosure；
- 跨 workspace/session 复用环境及其 identity、配额和清理。

这些能力必须由实际 dogfood 证据触发，不能通过预建枚举或隐式 detector 提前扩张。
