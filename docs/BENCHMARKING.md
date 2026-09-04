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
