# Command Environment Contract

本文是当前命令执行环境的维护参考。适用范围包括本地 Linux/WSL2 Product Runtime 的 Bubblewrap backend，
以及 SWE-bench 的显式 Docker backend。安全边界见[安全模型](SECURITY.md)。

## 三个不同对象

| 对象 | 含义 |
| --- | --- |
| Runtime environment | 运行 CodeAgent CLI/RPC 的 Python 进程环境 |
| Command environment | 某一次 `run_command` 实际获得的 cwd、PATH、变量、文件系统和网络 |
| Sandbox policy | 限制这条命令可访问哪些资源的规则 |

Runtime 自己的虚拟环境不是目标项目环境；sandbox 允许访问项目也不保证项目依赖已经安装。

## 命令生命周期

每次 `run_command` 都创建 fresh shell，不复用上一命令的进程状态。本地 backend 的内层 shell 是：

```text
/bin/bash --noprofile --norc -c <command>
```

只有 `purpose=validation` 增加 `-o pipefail`：

```text
/bin/bash --noprofile --norc -o pipefail -c <command>
```

因此，同一命令内的 `export`、`source`、alias、function 和 `cd` 在进程结束后消失。Candidate 中实际写入的文件、
Session metadata 和仍有效的 session-scope grant 可以跨命令存在。需要激活项目环境时，Agent 必须在同一条命令中完成，
或使用明确的 executable 路径。

Runtime 不解析 shell string 来识别 Python、Node、pytest、npm 等工具，不自动改写、安装依赖或重试业务命令。

## EffectiveCommandEnvironment

实际进程环境、Runtime Snapshot 和命令结果 provenance 使用同一构造结果。来源按后者覆盖前者：

1. Runtime 安全默认值；
2. 从启动进程允许继承的基础变量；
3. execution backend 显式提供的环境。

允许继承的基础集合为：

```text
PATH, LANG, LC_ALL, LC_CTYPE, TZ, TERM
```

Runtime 覆盖或设置：

```text
HOME=<private runtime home>
TMPDIR=<private tmp>
XDG_CACHE_HOME=<private runtime cache>
PYTHONIOENCODING=utf-8
PYTHONDONTWRITEBYTECODE=1
```

Provider key、云凭据、SSH agent、cookie、token、未知环境变量和高权限 socket 不会默认继承。Runtime Snapshot 只暴露
环境变量名称和非 secret 事实，不保存 secret value。

Snapshot 中的 execution environment 包含 contract revision、backend、默认 cwd 及其解析路径、PATH 来源、private HOME/TMP、
network mode、fresh-shell 语义和 workspace 访问范围。它描述本次 backend 的事实，不枚举语言工具是否存在，也不推荐解释器。

## Product Bubblewrap backend

本地命令在新的 Bubblewrap namespace 中运行：

- active Candidate 与 Runtime 私有目录可写；
- source repository 和相关 Git metadata 只读；
- 宿主根目录默认只读可见；
- HOME 和 `/tmp` 使用 Runtime 私有目录；
- 网络默认关闭；
- 额外写入位置或网络需要 Policy 与 Approval；
- backend executable 或 namespace probe 失败时拒绝执行，不回退到宿主 shell。

一次性权限只对当前命令有效；session-scope grant 在后续命令前仍会经过 Policy 和资源 identity 复检。

## SWE-bench Docker backend

Benchmark 每条项目命令创建一次性 container，并把宿主 Candidate bind mount 到 `/testbed`。Backend 显式提供 HOME、TMP、
encoding 和网络策略；命令结束后 shell 状态不保留，文件变化通过 mount 返回同一 Candidate。

Docker backend 使用与 Product 相同的 utility/validation shell 区分，但不使用 Bubblewrap，也不经过 VS Code 人工 Approval。
它不是普通产品 workspace backend。

## 命令请求与结果

`run_command` 的 `purpose` 只允许：

- `utility`：探索、读取、普通构建或辅助命令；
- `validation`：用于证明当前 Candidate 的测试或检查命令，并启用 `pipefail`。

Runtime 不根据命令文本自动改变 purpose。对于看起来像测试的 utility 命令，只能记录诊断，不生成 validation evidence。

每条真实启动的命令记录：

- 原始 command、cwd 和 timeout；
- status、exit code、timeout 与有界 stdout/stderr；
- before/after Candidate revision 和 Git tree audit；
- environment contract revision、backend 和环境变量名称；
- effective network/filesystem policy；
- workspace state 与必要诊断。

非零退出和 timeout 是正常 terminal result，不自动 taint workspace。只有 Runtime 无法确认进程边界或执行后的文件状态时，
Session 才进入 `workspace_tainted` 或 `recovery_required`。

## Validation 证据

Validation pipeline 的退出状态由 `pipefail` 传播。例如 `pytest | tee report.log` 中 pytest 失败时，整个命令返回非零，
不能形成成功 evidence。Utility pipeline 保持 Bash 默认语义。

成功退出仍不等于测试充分。形成 current validation evidence 还要求命令使用 validation purpose、workspace audit 完整，
并且 base commit、workspace revision 和 Candidate subject tree 与当前状态一致。Official benchmark grader 是独立证据。

## 环境准备与权限

项目依赖缺失时，Agent 可以通过普通命令探索并在获准范围内准备环境，但 Runtime 不提供绕过 `run_command` 的安装器：

```text
Agent command + permissions
→ ToolRegistry
→ Policy / Approval
→ execution backend
→ workspace audit
→ ToolResult
```

下载依赖需要 network grant；Candidate 外写入需要对应文件权限。仓库中的 setup script 是普通不可信命令，不会因文件名
自动执行。用户未要求执行、测试或环境准备时，Agent 不应仅为便利安装依赖。

## 当前边界

- 没有语言、框架或包管理器 detector；
- 没有 Session environment overlay；
- 没有 repo-local setup script 信任与自动重放；
- 没有 domain/port 级网络授权；
- 没有跨 workspace 的环境缓存身份和配额协议；
- Candidate 内大型 ignored 环境目录仍可能占用磁盘，不属于正式产品保证。
