# Benchmark 说明

本文说明当前 SWE-bench 接入边界、结果口径和首批基线。具体实现以
[`codeagent/benchmark/`](../codeagent/benchmark/)及对应测试为准。

## 运行架构

```text
task metadata + pinned official image
→ 导出 prepared /testbed 到 host cache
→ Git admission + Session Candidate worktree
→ 每条 run_command 投影到一次性 Docker /testbed
→ 导出 Candidate patch
→ official SWE-bench grader
```

Docker `/testbed` 是命令执行投影，不是第二套 Workspace identity。Search、Read、Atomic Edit、Session、
Candidate 和 patch 都以宿主 worktree 为权威；容器内命令的写入通过 bind mount 回到同一 Candidate。
每条命令使用独立容器，但镜像与挂载内容连续，命令结束后的文件状态由 host worktree 持久化。

## 数据与隔离

- source 来自固定 digest 的官方 instance image，而不是重新 clone 后猜测环境；
- dataset base commit、prepared HEAD/tree 和 image digest 分别记录；
- gold patch、FAIL_TO_PASS/PASS_TO_PASS 只进入 preflight/grader，不暴露给 Agent；
- 首批任务定义见 [`benchmarks/swebench/verified-first-10.json`](../benchmarks/swebench/verified-first-10.json)；
- gold preflight 必须先证明官方环境和 required tests 可运行；gold unresolved 不进入正式题集；
- Docker Desktop 当前内存较小，首批使用单任务串行，避免并发噪声。

## 结果状态

以下状态必须独立报告：

| 字段 | 含义 |
| --- | --- |
| `agent_status` / `agent_final` | Runtime 是否正常结束 UserTurn，以及模型最终文本 |
| `validation_passed` | Agent 是否用 `purpose=validation` 成功验证当前 Candidate |
| `patch_present` | 是否导出了非空 Candidate patch |
| `oracle_status` / `oracle_passed` | official grader 是否正常完成，以及 SWE-bench 是否 resolved |

Oracle 通过不等于 Agent 正常完成；Agent final 也不等于 validation 或 external oracle 通过。

## Token 与成本

每个 provider 响应对应一个 `model_usage` Session Event，记录：

- provider、model、provider request id；
- input、output 和 total token；
- cached input、cache miss input 和 reasoning token（provider 不提供时为 `null`）。

该事件不进入模型 Context，也不占 UserTurn 模型步骤。工具参数 JSON 损坏时，只要 provider 已返回 usage，
仍记录该次请求。单题 `result.json.token_usage` 聚合请求数与 token；
`requests_with_usage < requests` 表示计量不完整，缺失值不得按零处理。

成本属于报告层派生值：必须保存 provider 价格来源、币种和生效日期，分别计算缓存输入、非缓存输入、
输出等类别。Session 只保存 provider 返回的计量事实，不固化价格。

## 首批十题基线

固定题集包含 6 Easy、4 Medium；没有纳入预计需要长时间工作的 Hard。两批均使用每个 UserTurn 最多
48 个模型步骤：

| 模型 | External oracle | Completed | Context exceeded | Step exhausted | Validation passed |
| --- | ---: | ---: | ---: | ---: | ---: |
| GLM 5.2 | 8/10 | 9 | 0 | 1 | 7 |
| DeepSeek V4 Pro | 8/10 | 3 | 6 | 1 | 6 |

两批失败题相同：`pydata__xarray-3305` 未形成 patch并耗尽步骤；`sphinx-doc__sphinx-9258` 未满足
PASS_TO_PASS。DeepSeek 另有多题在 patch 可通过 oracle 后仍发生 Context 超限，所以后续优化不能只看
resolved rate。

历史 batch manifest 位于本地 `benchmarks/swebench/runs/`，该目录包含可再生成的大体积运行产物，
不作为源码提交内容。以后正式对比至少记录 selection id、Runtime commit、provider/model、预算配置、
开始结束时间、逐题结果、token usage 覆盖率和 official report。

## 验证入口

- Source preparation：`tests/test_swebench_source_preparation.py`
- Docker projection：`tests/test_swebench_docker_executor.py`
- 真实官方镜像：`tests/test_swebench_docker_integration.py`
- Gold preflight：`tests/test_swebench_preflight.py`
- 单任务与 grader：`tests/test_swebench_harness.py`

真实 Docker 测试是 opt-in，具体命令见 [TESTING](TESTING.md)。

## 正式 Batch 与恢复

selection 只回答“跑哪些题”，包含 selection id、固定上游版本和有序 task id；provider、模型和预算属于
run configuration，不得写入 selection。正式入口示例：

```bash
codeagent swebench-batch benchmarks/swebench/verified-smoke-2.json \
  --task-repo reference/swe-bench-tasks \
  --source-cache benchmarks/swebench/source-cache \
  --output-root benchmarks/swebench/runs \
  --provider glm --model glm-5.2 \
  --grader-command '.venv-swebench/bin/python -m swebench.harness.run_evaluation' \
  --report-template 'benchmarks/swebench/runs/grader-reports/codeagent__glm-5.2.{run_id}.json'
```

命令按 selection 顺序串行运行。每题先写 `running` task-state，完成后原子写入结果；异常时已经完成的
题目不会丢失。使用相同参数和 `--run-id <id>` 恢复。只有状态完整、配置指纹一致且 prediction、trajectory
artifact 都存在的题才会跳过；selection hash 或配置不一致会 fail closed。不完整题会用新的单题 run id
重新执行，旧诊断材料不会作为成功结果复用。CLI 会从固定 task repo 为 selection 导出 grader dataset，
gold/test patch 只进入该 grader 输入，不会进入 Agent prompt 或 ToolRegistry。

## Run manifest、usage 与 cost

每批目录包含：

- `run-manifest.json`：selection hash、Runtime commit/dirty/version、无凭据 endpoint identity、模型配置、
  step/context reserve，以及逐题 image/prepared Git identity；
- `tasks/*/task-state.json`：可恢复的单题状态；
- `tasks/*/trajectory-summary.json`：确定性轨迹事实；
- `batch-summary.json`：进度、oracle、usage 和可选 cost 汇总。

模型配置指纹由实际 ModelConfig、RuntimeConfig 和经过脱敏的 endpoint identity 确定性计算。manifest 不保存
API key、URL userinfo、query 或 fragment。`finished_at` 是运行生命周期字段；其他 identity 字段在 resume 时
必须通过一致性检查。

usage coverage 有三种状态：`complete` 表示每次 provider response 都有 usage，`partial` 表示仅部分有，
`unavailable` 表示没有可用计量；token 值 `0` 与未知的 `null` 严格区分。历史十题缺少 `token_usage`，保持
unavailable，不补零，也不依据后来的配置猜测成本。

价格通过独立 JSON snapshot 输入，字段包括 snapshot id、provider/model、生效日期、币种、每计价单位 token
数量、各计价项 Decimal 字符串和来源。金额使用 `Decimal` 计算。coverage 不完整、计价项未知或缺价格时，
`total` 为 `null`，只保留 `known_subtotal`，避免把局部金额表示为精确总成本。

## Deterministic trajectory analyzer

analyzer 从 Event、result 和 validation 事实计算模型步骤、终止原因、工具分类、有效修改、validation、
同 revision 同范围重复读取和规范化相同命令等指标。“有效修改”是 Candidate revision 实际增加，不包含
失败或 no-op edit。它只回答“发生了什么”；为什么发生、修改是否聪明或测试是否充分，需要人工/Codex review。

新产生的 context overflow Event 会记录最终 minimum set 的分类估算、最大 Observation 来源和 estimation
residual，analyzer 可直接展示。该摘要只含 category、token、item count、tool/call/path，不复制正文。旧
Session 可以直接分析；历史 Event 缺少这些字段时输出 `null` 与 unavailable reason，不进行推断。usage
按事件顺序能可靠对应模型 step 时才计算 post-edit token。

## 2026-09-04 最小真实 smoke

`verified-smoke-2-v1` 已用 GLM 5.2 标准 API、48-step 总预算和 official grader 完成。run id 为
`l4-smoke-glm52-20260904`，两题均正常结束、形成 patch、存在 successful validation，并由 external oracle
resolved。46/46 provider responses 含 usage：总计 input 577,756、output 8,232、total 585,988，其中缓存
命中 216,064、推导的非缓存输入 361,692 token。按访问于 2026-09-04 的官方标准 API 价格快照，正式完成
批次成本为 ¥3.556160。

| Task | Oracle | Steps | First/last edit | Post-edit steps | Validation | Exact read / command repeat |
| --- | --- | ---: | --- | ---: | --- | --- |
| `pytest-dev__pytest-7205` | resolved | 29 | 3 / 24 | 5 | 7 passed, 0 failed | 0 / 0 |
| `sympy__sympy-20590` | resolved | 17 | 9 / 9 | 8 | 8 passed, 3 failed | 0 / 0 |

首次执行第一题时，真实目录中的 `sessions/worktrees` 暴露了 trajectory Session 定位缺陷；task-state 正确
保持 failed。修复并补回归测试后以同一 batch id resume，不完整题按规则重新执行。首次未纳入正式完成批次
的调用另消耗 339,618 token，按同一快照约 ¥1.944668；因此本次验证的实际 API 总消耗为 925,606 token、
约 ¥5.500828。该失败运行保留作诊断，不混入最终 batch summary。
