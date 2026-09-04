# 当前有效决策

本文只保留仍约束当前和下一阶段实现的决策。完整历史、被取代条目和设计论证位于
[`archive/`](archive/)。实现事实以 [ARCHITECTURE](ARCHITECTURE.md) 和测试为准。

## D1：Session 连续，能力动态升级

- 用户不在会话开始前选择 readonly/execution mode；
- Session 初始读取 source，首次受保护编辑或命令经批准后创建专属 worktree；
- worktree 创建后成为唯一 active workspace，source 保持 baseline 身份；
- Session 是当前唯一上层运行容器，不引入 Task 实体或并行 active worktree。

## D2：模型只有请求权，Runtime 持有执行权

- Agent 只能调用 ToolRegistry 注册的闭合工具；
- Policy、Approval、PathGuard、Workspace identity 和 executor 由 Runtime 强制执行；
- 不因 provider、benchmark 或提示词绕过相同边界；
-审批绑定冻结操作和资源，不授权 Agent 任意扩大范围。

## D3：当前修改由 Git worktree 表达

- worktree HEAD 是最近 accepted baseline，working tree 是 pending changes；
- Atomic Edit 和可信完成 audit 的命令变化都是合法当前修改；
- accept 冻结并复检 patch，以三方合并写入 source，成功后前移内部 checkpoint；
- discard 必须证明恢复 baseline；无法确认时 fail closed；
- `workspace_tainted` 表示 after boundary 无法建立，`recovery_required` 表示更强的 identity/恢复不确定性。

## D4：文件编辑使用单一原子事务工具

- Agent-facing 编辑入口为 `apply_workspace_edit`；
- create/replace/delete/move 在写入前统一校验，已有文件必须携带完整 SHA 前置条件；
- 失败回滚全部操作；无法证明回滚时进入 `recovery_required`；
- 不接受任意 patch、binary 或越过 active workspace 的路径。

## D5：任意命令受 OS sandbox 和资源 Policy 约束

- `run_command` 接受 shell string，安全边界不是命令文本白名单；
- 正式 Session 使用 per-command Bubblewrap，默认最小写权限和断网；
-额外文件或网络能力必须显式申请，hard deny 不能由普通审批解除；
- sandbox 不可用时 fail closed，不提供裸宿主 fallback；
- validation evidence 只证明明确标记的命令针对当前 Candidate 成功，不评价测试质量。

## D6：Event 持久化与 API Context 分离

- `events.jsonl` 保存恢复和审计所需历史，不等于每次 provider 输入；
- Context 通过 UserTurn/ModelStep/ToolExchange 投影临时构建；
-裁剪必须保持 provider 工具协议完整，并优先退化旧 Observation 内容而不是破坏事件历史；
-最低集合超限时明确终止当前 UserTurn，保留 Session/workspace。

## D7：Context v2 不维护隐式 Active Code

- 源码是可重新获取的环境事实，通过 search/read 按需进入 Recent Tool Context；
- 旧源码 Observation 在预算压力下退化为结构化访问记录，精确内容由 Agent 重新读取；
- 不自动分析 Agent read 行为来维护 Working Set；
- 只有 benchmark 证明重复 retrieval 是显著成本或失败来源时，才评估显式 pin/cache；
- deterministic reduction 不足后才考虑可重建 semantic condensation。

## D8：执行切片不等于 UserTurn

- execution slice 是内部控制点，耗尽不创建新的用户消息；
- 同一 UserTurn 可以继续执行，达到总模型步骤预算才以未完成状态终止；
- Agent final、slice pause、step exhaustion 和 context exhaustion 必须分别表达。

## D9：SWE-bench 使用 prepared source 和执行投影

- official image 中 prepared `/testbed` 是 source 内容与依赖环境的事实来源；
- dataset base、prepared HEAD/tree 和 image digest 分别记录；
- host Git worktree仍是 Workspace/Candidate 权威；
- Docker `/testbed` 是 per-command execution projection，不建立第二套 Workspace backend；
- gold/eval artifacts 不进入 Agent ToolRegistry；external oracle 独立运行。

## D10：模型 usage 是审计事实，价格是报告策略

- 每个 provider 响应记录独立 `model_usage` Event，包括 provider/model/request id 和可获得的 token 字段；
- malformed tool arguments 已产生的 provider usage 也必须记录；
- usage Event 不进入模型 Context，也不占模型步骤；
- 缺失 usage 必须显式表达，不能按零处理；
- Session 不固化易变价格，成本由带来源、币种和生效日期的价格快照在报告层计算。

## 明确暂缓

LangGraph、多 Agent、Task 数据模型、隐式 Working Set、RepoMap/RAG、通用 Docker Workspace backend、
自动权限推断和自动命令重放均不是当前基线的一部分。
