# SWE-bench 评测

SWE-bench 用真实开源仓库的 bug 修复任务检查 Coding Agent。CodeAgent 使用任务指定的 Docker 镜像，
最后交给 SWE-bench 官方评分程序判断修复是否成立。

本页说明正式评测如何固定题目和运行条件。已经得到的结果见[评测结果](EVALUATION_RESULTS.md)。

## 正式题集

`v0.5.0-rc.2` 的正式题集是 **HAL SWE-bench Verified Mini**。HAL 发布了固定的 50 个 instance ID；
CodeAgent 没有替换题目，也没有根据开发结果筛题。

版本控制中的题集是
[hal-verified-mini-50.json](../benchmarks/swebench/selections/hal-verified-mini-50.json)。它记录以下来源身份：

- HAL harness revision：`7b231a952828022a43977f21acfd452adda5088c`
- 官方列表路径：`agent_eval_harness/benchmarks/swebench_verified_mini_task_ids.txt`
- 官方列表 SHA-256：`ee7dc327d73dd45402679beed9f6ab22e5295cb6a2bb3b76669ca5e1f60e526f`
- 获取日期：`2026-09-09`

HAL 的列表没有难度标签。CodeAgent 只为安排运行先后读取固定版本
`swe-bench-tasks` 中的 `task.yaml` 难度字段。20 题排为 Easy，24 题排为 Medium，6 题排为 Hard；
同一组内保持 HAL 官方顺序。

难度只决定哪道题先运行。所有题使用相同的模型设置、步骤上限、命令时限和尝试规则。

早期自定义 50 题保留在
[verified-eval-50.json](../benchmarks/swebench/verified-eval-50.json)。它已经标记为 Development，
不计入 `v0.5.0` 正式评测。

来源：
[HAL SWE-bench Verified Mini](https://hal.cs.princeton.edu/swebench_verified_mini)、
[HAL harness](https://github.com/princeton-pli/hal-harness)。

## 一道题如何运行

```text
读取固定题目
→ 从指定 Docker 镜像导出 /testbed
→ 核对 Git commit、文件树和镜像 digest
→ 创建宿主 Git worktree
→ 在一次性 Docker container 中运行项目命令
→ 导出最终 patch
→ 使用 official grader 评分
→ 立即保存结果、用量、成本和报告路径
```

搜索、读取和编辑文件使用宿主 Git worktree。每条项目命令在新的 Docker container 中运行，
并把同一个 worktree 挂载为 `/testbed`。

Docker 只提供项目命令的执行环境。Agent loop、文件工具和修改规则仍由本地 Runtime 负责。

benchmark harness 使用预先允许的评测权限，不经过 VS Code 的人工 Accept。因此它不能证明产品界面的审批体验。

官方答案和测试列表只用于环境检查与评分。它们不会主动加入模型输入。

## 模型和统一资源上限

正式模型固定为 GLM-5.3。该模型在当前 API 上始终启用推理，不能关闭；本次固定使用
`reasoning_effort=max`。

50 题统一使用以下限制：

| 设置 | 固定值 |
| --- | ---: |
| 单次输出上限 | 8,192 token |
| Runtime 上下文上限 | 128,000 token |
| 每个执行切片 | 12 次模型调用 |
| 每题模型调用上限 | 72 次 |
| 项目命令默认 timeout | 120 秒 |
| 项目命令最大 timeout | 1,800 秒 |
| 每题 wall-clock 上限 | 10,800 秒 |
| 完成尝试 | 1 次 |
| 项目命令网络 | 默认关闭 |

输入、输出、缓存输入和 reasoning token 是事后记账字段，不是每题总 token 上限。
正式配置没有虚构 `max_total_tokens_per_task`。

ContextBuilder 每次请求仍会控制工具输出大小。单条工具结果和近期工具原文有独立限制，
不会因为模型上下文较大而无限增长。

机器可读配置见
[v0.5.0-rc2-hal-mini-50.json](../benchmarks/swebench/configs/v0.5.0-rc2-hal-mini-50.json)。

## GLM-5.3 兼容性 smoke

2026-09-09 的最小付费 smoke 使用 GLM-5.3 和 reasoning `max`。模型先推理并调用 `read_file`，
收到工具结果后继续推理，再给出最终答案。

两次模型请求都有完整 usage。合计 6,588 token，其中 reasoning 48 token；
按冻结价格快照计算为 `CNY 0.018128`。

同日的单题 Docker smoke 使用 `psf__requests-1766` 跑通源码准备、Agent、patch 导出和 official grader。
它在 7 次模型请求后完成，grader 判定 resolved；重复同一 run ID 时直接复用结果，没有再次调用模型。

这些 smoke 证明 Provider 参数、工具回合、推理内容回传、usage、成本计算和 Docker benchmark path 可以工作。
它们不计入正式 SWE-bench 成绩。机器记录见
[glm-5.3-tool-roundtrip-2026-09-09.json](../benchmarks/swebench/smoke/glm-5.3-tool-roundtrip-2026-09-09.json)。

## 批次顺序和持久化

`codeagent swebench-batch` 按 selection 顺序串行执行。正式 selection 加载时会检查
Easy → Medium → Hard 的顺序；顺序错误会在模型请求前被拒绝。

每题启动前先原子写入 `running` 状态。正常完成后，Runner 立即写入这些信息：

- instance ID 和 attempt
- completed 状态与结果分类
- Agent 和 official grader 状态
- token usage 与成本
- prediction、执行摘要和 official report 路径

结果分类至少区分：`resolved`、`unresolved`、`budget_exhausted`、`provider_error`、
`infra_error` 和 `not_started`。

official grader 正常完成但没有解决的题属于 `unresolved`。它也是一个完整结果，恢复时会跳过，
不会因为分数为零而自动重跑。

Agent 没有生成 patch 时，official grader 会把该题记录为 empty patch。CodeAgent 将它保存为正常的
`unresolved`，并保留每题目录中的批次报告。它不是基础设施失败。

Provider 或基础设施异常会保存为 failed，并立即停止 batch，不会标记为 completed。使用相同
`--run-id` 恢复时，这道未完成题会重新执行，attempt 加一。一个正常完成的题只允许一个完成结果。

当前记录保留最新一次未完成状态和最终完整结果。异常请求在产生完整单题 usage 前中断时，
其费用可能无法进入批次总成本；正式报告必须说明这种缺口。

## 断点恢复

恢复前，Runner 会核对 selection ID、selection 文件哈希、模型配置、价格快照和成本保护设置。

一个已完成结果还必须保留 prediction、执行摘要和唯一的会话事件文件。证据缺失时不会把它当作可跳过结果。

恢复语义如下：

```text
resolved 或正常 unresolved → 跳过
not started → 从这里继续
provider / infra interrupted → 重新执行，attempt 加一
配置或价格身份变化 → 拒绝恢复
```

正式运行还要求从同一个干净的 release tag 启动，并使用固定 grader 命令。
Runner 当前不会自行比较 grader 可执行文件的内容哈希。

## 本地成本保护

GLM 没有在本流程中使用稳定的官方账户余额查询接口。Runner 因此使用本地预算：

```text
初始预算 - 已完整记录的成本 = 估算剩余预算
```

正式 batch 创建时必须同时提供 CNY 价格快照和 `--cost-budget-cny`。
每道未完成题启动前都会重新计算。如果估算剩余预算低于 10 元，batch 保存汇总并停止。

已经启动的题不会因为余额变化被中途终止。已有结果缺少完整成本时，Runner 也不会启动下一题。

这是本地成本预算保护，不一定等于 Provider 账户真实余额。

GLM-5.3 价格快照见
[glm-5.3-standard-api-2026-09-09.json](../benchmarks/swebench/prices/glm-5.3-standard-api-2026-09-09.json)。
reasoning token 已包含在 output token 中，不会重复计价。

## 正式运行入口

下面的命令展示固定参数。`INITIAL_BUDGET_RMB` 由运行者在首次启动时填写，并在恢复时保持不变。

```bash
export TASK_REPO="$PWD/reference/swe-bench-tasks"
export SOURCE_CACHE="$PWD/benchmarks/swebench/source-cache"
export RUNS_DIR="$PWD/benchmarks/swebench/runs"
export GRADER_COMMAND="$PWD/reference/SWE-bench/.venv/bin/python -m swebench.harness.run_evaluation --max_workers 1 --timeout 1800"
export INITIAL_BUDGET_RMB="<本次批准的批次预算>"

.venv/bin/codeagent swebench-batch \
  benchmarks/swebench/selections/hal-verified-mini-50.json \
  --task-repo "$TASK_REPO" \
  --source-cache "$SOURCE_CACHE" \
  --output-root "$RUNS_DIR" \
  --provider glm --model glm-5.3 \
  --temperature 1.0 \
  --reasoning --reasoning-effort max \
  --max-tokens 8192 --slice-steps 12 --max-model-steps 72 \
  --grader-command "$GRADER_COMMAND" \
  --report-template "$PWD/logs/evaluation/{run_id}/codeagent__glm-5.3/{instance_id}/report.json" \
  --price-snapshot benchmarks/swebench/prices/glm-5.3-standard-api-2026-09-09.json \
  --cost-budget-cny "$INITIAL_BUDGET_RMB" \
  --minimum-remaining-cost-cny 10 \
  --run-id v0.5.0-rc2-hal-mini-50
```

恢复时使用完全相同的命令和 `--run-id`。正式调度器还应对每个 task 应用机器配置中的
10,800 秒 wall-clock 上限。

## Development 证据

GLM-5.2 两题 smoke 和单题 Hard 校准都属于 Development。它们用于验证链路和选择统一资源上限，
不会与 HAL 50 题合并计算成绩。

Hard 校准样本 `django__django-15629` 使用 61 次模型调用后主动结束。Agent 的验证通过，
official grader 判定 unresolved。这个结果支持 72 步和 8,192 单次输出的统一设置，
不表示 GLM-5.3 的正式成绩。

详细记录见
[glm-5.2-hard-1-result.json](../benchmarks/swebench/calibration/glm-5.2-hard-1-result.json)。

## 正式报告边界

正式 50 题完成前，不报告预测通过率。最终报告至少包含：

- official grader 解决题数，分母固定为 50
- 六类 task outcome
- 每题 attempt 和停止原因
- usage 覆盖率与完整成本
- release commit、tag、selection 和价格快照身份
- Docker image digest 和 grader report 路径

当前没有完整的分阶段时延统计。无法把源码准备、Agent 执行和 grader 分别报告为中位数与 P90。
