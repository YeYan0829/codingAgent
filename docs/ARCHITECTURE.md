# 当前 Runtime 架构

本文只描述已经落地的结构与不变量。产品取舍见 [DECISIONS](DECISIONS.md)，具体行为以源码和测试为准。

## 1. 核心流程

```text
Session(source_only)
→ Search / Read
→ 首次受保护 edit 或 command：审批并创建 Session worktree
→ Atomic Edit / Command
→ validation evidence
→ show changes
→ accept / discard
→ 在同一 Session 与 worktree 中继续
```

项目没有独立 Task 实体。Session 始终绑定一个 source repository；worktree 是唯一 active workspace。
worktree HEAD 是最近 accepted baseline，working tree 是尚未处理的当前修改。用户无需在 Session 开始前
选择读或写模式，权限随实际操作动态升级。

## 2. 主要模块

| 模块 | 职责 | 代码入口 |
| --- | --- | --- |
| Session | metadata、append-only Event、resume | `codeagent/session/` |
| Workspace | source/active identity、Git worktree 生命周期 | `codeagent/workspace/` |
| Tools | Agent schema、ToolRegistry、read/edit/command 入口 | `codeagent/tools/` |
| Safety | PathGuard、敏感路径与 symlink 边界 | `codeagent/safety/` |
| Runtime | Agent loop、权限、命令、编辑、Candidate、验证 | `codeagent/runtime/` |
| Context | Event projection、Runtime Snapshot、budget/reduction | `codeagent/context/` |
| Model gateway | Fake、DeepSeek、GLM 和 usage 标准化 | `codeagent/model_gateway/` |
| Benchmark | SWE-bench source、Docker executor、preflight、grader | `codeagent/benchmark/` |

所有 Agent 能力必须通过 ToolRegistry。Policy、Approval、Workspace identity 和 executor 边界不能由
benchmark 或 provider adapter 绕过。

## 3. Search、Read 与 Edit

文件能力只接受 active-workspace-relative path，并复用 PathGuard、SensitivePath 和 symlink 检查。
Search 使用固定构造的 ripgrep argv；Git status/diff 使用固定只读 argv。`read_file` 返回真实范围、总行数、
前后剩余标记和完整 bytes SHA。

唯一编辑工具 `apply_workspace_edit` 支持 UTF-8 文本的 create/replace/delete/move。对已有文件的操作必须携带
最近读取得到的 SHA；整个 operations 数组先 prepare，再作为一个事务提交。失败会回滚；无法证明恢复时
Session 进入 `recovery_required`。

## 4. Workspace 与 Candidate

第一次受保护操作触发：

```text
source_only → preparing_workspace → changes_active
```

Runtime 创建 detached Git worktree并重新构建工具服务。之后的读取、编辑、命令和验证全部针对 active
worktree，source 只是 baseline。Atomic Edit 和命令产生的文件变化都是合法 Candidate 修改，并统一推进
`candidate_revision`。

`accept` 冻结当前 patch并复检 identity，通过 Git 三方合并应用到用户 source；冲突时拒绝覆盖并保留现场。
成功后 worktree HEAD 前移为内部 checkpoint。`discard` reset/clean 到 accepted baseline，不能证明恢复则
fail closed。

## 5. 命令执行边界

`run_command` 接受一个 shell string，但宿主始终以 `shell=False` 启动固定 shell；字符串只在隔离执行域中
解释。Runtime 不分析 shell AST、不猜测缺失权限，也不自动重放失败命令。

正式 CLI 使用 per-command Bubblewrap：

- host root 只读，Candidate 和 Runtime 临时目录按 Policy 可写；
- source、Git metadata 和确定敏感路径受到保护；
- private `/tmp`，网络默认关闭；
-额外 host write 或 WSL host network 必须显式申请；
- sandbox 不可用时 fail closed，不回退裸执行。

SWE-bench 使用另一种 `CommandExecutor`：每条命令创建一次性 Docker container，把同一个 host Candidate
投影到 `/testbed`。容器是 execution backend，不建立新的 Workspace/Session/Candidate identity。详见
[BENCHMARKING](BENCHMARKING.md)。

每个真正启动的命令都有 before/after Git audit。exit nonzero 或 timeout 不自动 taint；只有进程或 after
boundary 无法确认时进入 `workspace_tainted` 或更严格的 `recovery_required`。

## 6. Validation

Runtime 不判断测试命令是否充分。`run_command(purpose="validation")` 成功且对应当前 Candidate tree 时形成
validation evidence；后续文件变化会使它失效。Agent final、validation evidence、patch 和 external oracle
是四个独立状态。

## 7. Context 与模型调用

`events.jsonl` 是持久历史，每次 API 请求的 Context 是临时投影视图：

```text
append-only Events
→ UserTurn / ModelStep / ToolExchange projection
→ Runtime Snapshot
→ recent bounded Tool Observation + historical typed residue
→ token budget reduction
→ provider request
```

Context 不维护 Active Code 或自动 Working Set。源码是可重新获取的环境事实，只通过真实 search/read
Observation 进入输入；旧 Observation 在预算压力下退化为访问记录，需要精确内容时由 Agent 重新读取。
裁剪只发生在合法的已闭合 ToolExchange/ModelStep 边界，不制造不完整 provider 协议。

默认 DeepSeek context limit 为 32k：预留 generation、continuation/tool 和 safety 后 usable input 为 22k；
GLM 实验配置为 128k/118k。最低集合仍超限时写入明确的 `context_budget_exceeded` 终止事件，保留 Session
和 workspace。当前没有 semantic condensation。

每个 provider 响应还写入独立 `model_usage` Event。它包含标准化 token usage 和 provider request id，
不进入后续 Context，也不计为模型步骤。

## 8. 持久化与恢复

正常状态只有三个权威来源：

```text
session.json   当前 Session/workspace/revision/grants
events.jsonl   对话、模型 usage、审批与关键执行摘要
Git worktree   accepted baseline 与当前文件内容
```

完整命令输出、runtime HOME/TMP、事务备份和冻结 patch不是长期权威状态。成功后尽量清理；失败、超时、
tainted 或 recovery 场景才保留有界 diagnostics。Resume 从 metadata、Event 和 Git 客观状态重建服务，
不信任历史对话中的旧 workspace 描述。

## 9. 当前限制

- 只支持 Linux/WSL2 本地运行；
- source 必须是满足当前准入约束的 Git repository；
- 无 cgroup CPU/内存/进程数限制、复杂 seccomp 或 domain/port 网络控制；
- 原子编辑不支持 binary、encoding 猜测、mode、copy、目录递归操作和 case-only rename；
- Runtime 不自动安装依赖、不判断 validation 质量、不解决 Git 合并冲突；
- 无 semantic condensation、Working Set cache、RepoMap、RAG、LangGraph 或多 Agent；
- SWE-bench Docker executor 是 benchmark adapter，不是通用产品 Workspace backend。

## 10. 测试入口

模块行为由 `tests/` 中对应测试约束；完整运行方式见 [TESTING](TESTING.md)。历史设计推导、阶段调研和
旧评测位于 [`archive/`](archive/)，不作为当前接口依据。
