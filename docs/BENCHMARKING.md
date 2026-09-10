# SWE-bench 评测

本文记录 benchmark harness、运行身份、恢复、成本和 artifact contract。正式与开发结果见
[评测结果](EVALUATION_RESULTS.md)。最终 HAL 50 尚未运行；本文不提供预测通过率。

## 正式题集身份

`v0.5.0` 的正式题集是 **HAL SWE-bench Verified Mini** 的固定 50 个 instance。版本控制中的
[selection](../benchmarks/swebench/selections/hal-verified-mini-50.json)记录：

| 身份 | 固定值 |
| --- | --- |
| Selection ID | `hal-swebench-verified-mini-50-7b231a9` |
| HAL harness revision | `7b231a952828022a43977f21acfd452adda5088c` |
| 官方 task ID 文件 | `agent_eval_harness/benchmarks/swebench_verified_mini_task_ids.txt` |
| 官方列表 SHA‑256 | `ee7dc327d73dd45402679beed9f6ab22e5295cb6a2bb3b76669ca5e1f60e526f` |
| Selection 文件 SHA‑256 | `852042b05e2a1a95fd8e7dd01448f367aa8e332baf58eadbbd53167c2d90001f` |
| `swe-bench-tasks` commit | `3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d` |
| SWE-bench commit | `334882dd1f2664cc55c1abfe9de4884af023c0c0` |

HAL 列表没有难度标签。Harness 只为调度读取固定 `swe-bench-tasks` 的 `task.yaml`：20 Easy、24 Medium、
6 Hard；组内保持 HAL 官方顺序。难度只改变执行先后，不改变模型、Context、步骤、timeout 或 attempt 配置。

早期自定义题集 [verified-eval-50.json](../benchmarks/swebench/verified-eval-50.json)是 Development selection，
不计入正式成绩。

## Evaluation Candidate 绑定

[机器配置快照](../benchmarks/swebench/configs/v0.5.0-evaluation-candidate-hal-mini-50.json)固定评测参数，
其 release identity 是“包含该文件的 Evaluation Candidate commit”。实际运行时，`run-manifest.json` 记录：

- Runtime version、Git commit 和 dirty 状态；
- selection path、ID 和 SHA‑256；
- provider/model、endpoint identity 和 config fingerprint；
- Runtime、Context、condenser 和 reasoning 配置；
- SWE-bench 与 task repository identity；
- started/finished 时间和逐题状态。

正式运行必须从该 commit 的干净 checkout 启动。历史 RC 或 dirty Development 的 artifact 不得恢复、拼接或计入这 50 题。
配置 JSON 是审计快照；CLI 参数仍需与它机械核对。

## 固定模型与资源

| 设置 | 值 |
| --- | ---: |
| Provider / model | GLM / `glm-5.3` |
| Temperature | `1.0` |
| Reasoning | enabled，正常 effort `high` |
| 截断紧邻恢复 effort | `low`，恢复后回到 `high` |
| GLM `clear_thinking` | `true` |
| 单次主输出上限 | 8,192 token |
| Runtime context limit | 128,000 token |
| generation / continuation / safety reserve | 8,192 / 4,000 / 2,000 token |
| 可用输入预算 | 113,808 token |
| 执行切片 / 每题 ModelStep 上限 | 12 / 72 |
| Context events | max 80，target 40 |
| Context token ratio | soft 0.8，target 0.5 |
| Summary / condenser safety | 2,048 / 2,000 token |
| Condenser 调用 | 每主步骤最多 4 次，其中 soft 最多 1 次，无 tools |
| 命令 timeout | 默认 120 秒，最大 1,800 秒 |
| 完成尝试 | 1；Provider/基础设施中断后人工恢复时 attempt 增加 |
| 项目命令网络 | 默认关闭 |

Input、output、cached input 和 reasoning token 是事后用量，不是每题总 token 上限。没有
`max_total_tokens_per_task`。单条 ToolResult 的模型可见文本上限是 16,000 UTF-8 字节；Context 使用 rolling semantic
summary 和连续 raw CCES tail，具体 contract 见[上下文参考](CONTEXT.md)。

## 单题数据流

```text
读取固定 task
→ 从官方 instance image 导出 /testbed
→ 核对 image digest、base commit、HEAD 和 tree
→ 创建宿主 Candidate worktree
→ AgentRunner 处理 problem statement
→ 每条项目命令进入一次性 Docker container
→ 导出 prediction patch
→ official grader 评分
→ 原子保存任务状态、trajectory、usage、cost 和报告路径
```

搜索、读取和编辑发生在宿主 worktree；Docker 只提供项目命令环境，并把同一 worktree mount 到 `/testbed`。
Benchmark 使用预先允许的评测权限，不经过 VS Code 的人工 Approval/Accept，因此不能证明产品交互体验。

Gold patch、FAIL_TO_PASS 和 PASS_TO_PASS 仅供 source preparation 与 official grader 使用，不加入 Agent prompt、Runtime
Snapshot 或 ToolRegistry。这是 harness 数据流隔离，不是抵御 Docker daemon 或宿主管理员的独立安全边界。

## Validation 与 official grader

Agent 的 `run_command(purpose="validation")` 使用 Bash `pipefail`。退出码 0 只形成针对当时 Candidate tree 的 validation
evidence；它不证明测试范围充分，也不等于 official grader resolved。

Official grader 独立使用 SWE-bench 任务定义判定 patch。结果必须分别记录：

- Agent 是否正常结束；
- 是否产生 patch；
- validation 命令及其身份；
- grader 是否完成；
- grader 是否判定 resolved。

Empty patch 是 grader 正常完成的 `unresolved`，不是基础设施失败。

## Batch 顺序与状态

`codeagent swebench-batch` 按 selection 顺序串行执行。正式 selection 顺序不符合 Easy → Medium → Hard 时，在模型请求前
拒绝启动。每题启动前原子写入 `running`；执行中 `phase` 为 `agent` 或 `grader`，结束后为 `completed`。

每题 `task-state.json` 至少保存 instance、attempt、phase、timestamps、Agent/grader 状态和结果身份。Provider 或基础设施
异常还会在 `attempts/` 保存该次尝试的 Event 引用、trajectory、usage、cost 和错误。批次级
`batch-summary.json.current_task` 同步保存当前 instance、attempt、phase 和更新时间，因此完成数不增长时仍可判断正在运行的
阶段。

完成 outcome 分为：

- `resolved`；
- `unresolved`；
- `budget_exhausted`；
- `output_truncated`；
- `provider_error`；
- `infra_error`；
- `not_started`（批次汇总派生）。

正常 unresolved 是完整结果，恢复时跳过。Provider 或基础设施异常保存 failed 状态和 attempt 账本并停止 batch，
不伪装成 completed，也不会自动重跑整道题。
Trajectory 中的 excessive truncation 等字段只用于诊断，不改变 Agent 行为或 outcome。

## Artifact 结构

```text
benchmarks/swebench/runs/<run-id>/
├── run-manifest.json
├── batch-summary.json
└── tasks/<index>-<instance-id>/
    ├── task-state.json
    ├── attempts/<attempt>.json
    ├── attempts/<attempt>-trajectory.json
    ├── trajectory-summary.json
    ├── prediction.json
    └── runs/.../sessions/.../events.jsonl
```

存在 official grader 输出时，`task-state.json` 和 trajectory 记录配置的 report path。每题完成后立即原子更新状态与批次汇总。
Runtime-generated artifact 可以在正式 code freeze 期间新增，但不能反向修改 Runtime、harness 或评测语义。

## 断点恢复

使用相同 `--run-id` 恢复时，batch 复核 selection、模型 config fingerprint、价格快照和成本保护身份。可跳过的 completed
结果还必须有 prediction、trajectory 和唯一 Session events；证据缺失时不视为可恢复完成。

```text
resolved / 正常 unresolved → 跳过
not started → 继续
provider / infra interrupted → 人工恢复后重新执行，attempt + 1；保留旧 attempt 成本
身份或价格变化 → 拒绝恢复
```

Harness 不会自行验证 grader executable 的内容哈希，因此正式操作记录还必须保存固定 grader repository commit 和命令。

## Usage 与成本

每次 provider 响应按 `purpose=main_agent|context_condenser` 记账。没有 condenser 请求时，condenser requests、token 和 cost
是 complete numeric zero；发生请求但 Provider 缺失 usage 时才标记 unavailable。

批次顶层 `usage` 和 `cost` 表示所有已确认支出，包括最终参与评分的完成尝试和 Provider/基础设施中断前已经成功返回 usage
的请求。`cost_breakdown.scored_*` 只聚合完成题目；`retry_overhead_*` 单列失败 attempt。成本保护使用二者之和，而
resolved rate 仍只由 official grader 完成结果决定。没有 usage 的失败 HTTP 请求不推测 token 或费用。

正式 batch 使用[冻结价格快照](../benchmarks/swebench/prices/glm-5.3-standard-api-2026-09-09.json)。Reasoning token 已包含在
output token 中，不重复计价。GLM 没有在此流程使用稳定的账户余额 API，因此成本保护采用本地公式：

```text
估算剩余 = 250 CNY 批次预算 - 已完整记录成本
```

每道未完成题启动前重算；估算剩余低于 10 CNY 时保存汇总并停止。已启动题不因余额变化中断。已有请求缺少完整成本时，
不启动下一题。这是本地保护值，不代表 Provider 真实余额，也不要求花完整个预算。

## 正式运行入口

下面的命令只在 Evaluation Candidate 已提交、全量验证通过且 working tree clean 后执行。本轮文档冻结不运行它。

```bash
export TASK_REPO="$PWD/reference/swe-bench-tasks"
export SOURCE_CACHE="$PWD/benchmarks/swebench/source-cache"
export RUNS_DIR="$PWD/benchmarks/swebench/runs"
export GRADER_COMMAND="$PWD/reference/SWE-bench/.venv/bin/python -m swebench.harness.run_evaluation --max_workers 1 --timeout 1800"

.venv/bin/codeagent swebench-batch \
  benchmarks/swebench/selections/hal-verified-mini-50.json \
  --task-repo "$TASK_REPO" \
  --source-cache "$SOURCE_CACHE" \
  --output-root "$RUNS_DIR" \
  --provider glm --model glm-5.3 \
  --temperature 1.0 \
  --reasoning --reasoning-effort high \
  --max-tokens 8192 --slice-steps 12 --max-model-steps 72 \
  --grader-command "$GRADER_COMMAND" \
  --report-template "$PWD/logs/evaluation/{run_id}/codeagent__glm-5.3/{instance_id}/report.json" \
  --price-snapshot benchmarks/swebench/prices/glm-5.3-standard-api-2026-09-09.json \
  --cost-budget-cny 250 \
  --minimum-remaining-cost-cny 10 \
  --run-id v0.5.0-evaluation-candidate-hal-mini-50
```

恢复必须使用完全相同的命令和 run ID。启动前保存 Evaluation Candidate SHA，并确认：

```bash
git status --short
git rev-parse HEAD
sha256sum benchmarks/swebench/selections/hal-verified-mini-50.json
```

## 正式报告边界

Final HAL 50 result pending。完成前不报告预测通过率，也不把 partial/diagnostic/calibration run 合并成正式结果。

最终报告至少包含：

- official grader resolved 数，分母固定 50；
- 全部 outcome、attempt 和停止原因；
- usage coverage、main/condenser token 与冻结价格成本；
- Evaluation Candidate、最终 release commit/tag、selection 和价格快照身份；
- Docker image digest 和 grader report 路径；
- Evaluation Candidate 到 release commit 是否仅有文档差异。

当前没有完整分阶段时延统计，不能声称提供 source preparation、Agent 和 grader 的中位数/P90。

## Development 证据

Provider smoke、Docker smoke、GLM‑5.2 calibration、定向 reasoning/truncation 回归和未完成 HAL partial 都属于
Development/diagnostic。它们可以证明链路或支持参数选择，但不计入正式 50 题。具体记录和分类见
[评测结果](EVALUATION_RESULTS.md)。历史配置 JSON 保留用于审计，不是正式恢复入口。
