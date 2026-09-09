# 评测结果

本页把开发验证和正式成绩分开记录。开发 smoke 用于发现链路问题，不能与正式 50 题合并计算。

## Final：HAL SWE-bench Verified Mini

`v0.5.0-rc.2` 的正式评测使用 HAL 发布的固定 50 题和 GLM-5.3。

| 项目 | 冻结设置或当前状态 |
| --- | --- |
| 题集 | HAL SWE-bench Verified Mini，固定 50 题 |
| 调度 | 20 Easy → 24 Medium → 6 Hard；难度来自固定版本的 `swe-bench-tasks` |
| 模型 | GLM-5.3，reasoning `max` |
| 资源 | 全部题目统一使用 72 次模型调用上限和 8,192 单次输出上限 |
| 尝试 | 每题一个正常完成结果；中断题按恢复规则重跑并增加 attempt |
| Agent 运行 | **PENDING** |
| 解决题数 | **PENDING，不提供预测值** |
| Token 与成本 | **PENDING** |
| 逐题报告 | **PENDING** |

正式 selection 见
[hal-verified-mini-50.json](../benchmarks/swebench/selections/hal-verified-mini-50.json)。
机器可读运行配置见
[v0.5.0-rc2-hal-mini-50.json](../benchmarks/swebench/configs/v0.5.0-rc2-hal-mini-50.json)。

正式运行会从 `v0.5.0-rc.2` 的干净 checkout 启动。每题结束后立即保存结果，
中断后使用同一个 run ID 恢复。

`v0.5.0-rc.1` 的首次运行发现 grader adapter 会把 empty patch 误记为基础设施失败。
该运行已经停止并作废，不计入正式成绩，也不会与 `rc.2` 的结果合并。

## Development：GLM-5.3 Provider smoke

2026-09-09 的最小真实请求完成了下面的链路：

```text
reasoning → read_file → tool result → continued reasoning → final answer
```

| 指标 | 结果 |
| --- | ---: |
| 模型请求 | 2 |
| 工具调用 / 结果 | 1 / 1 |
| input / output / total token | 6,512 / 76 / 6,588 |
| reasoning token | 48 |
| usage 覆盖 | complete |
| 按价格快照计算的成本 | CNY 0.018128 |

该结果只证明 GLM-5.3 Provider 参数、工具回合、推理回传和计费链路可用。
它不是 SWE-bench 成绩。机器记录见
[GLM-5.3 smoke](../benchmarks/swebench/smoke/glm-5.3-tool-roundtrip-2026-09-09.json)。

同日还用 `psf__requests-1766` 完成一次 GLM-5.3 Docker benchmark smoke。源码准备、Agent、patch 导出和
official grader 均完成；7 次模型请求的 usage 完整，成本为 CNY 0.294200。再次使用同一 run ID 时，
Runner 跳过了已完成题，没有再次调用模型。该单题仍只算 Development 链路验证。

## Development：GLM-5.2 Hard 校准

历史预算校准使用 GLM-5.2 和 reasoning `max`。`django__django-15629` 在 61 次模型调用后主动结束；
Agent 的验证通过，official grader 判定 unresolved。

该次运行记录 1,616,580 total token，按当时价格快照计算为 CNY 8.742844。
它只用于选择所有正式题共用的资源上限。

HAL 官方固定列表也包含 `django__django-15629`。CodeAgent 没有因此替换该题；
正式选择保持 HAL 原样。历史运行使用不同模型，并明确保留为 Development 证据。

详细记录见
[glm-5.2-hard-1-result.json](../benchmarks/swebench/calibration/glm-5.2-hard-1-result.json)。

## Development：GLM-5.2 两题 smoke

2026-09-04 的两题运行用于检查 Docker、patch 导出和 official grader 链路。
它来自 dirty 开发目录，因此不是冻结版本成绩。

| 指标 | 结果 |
| --- | ---: |
| 题目 | `pytest-dev__pytest-7205`、`sympy__sympy-20590` |
| official grader 通过 | 2/2 |
| 有 usage 的响应 | 46/46 |
| input / output / total token | 577,756 / 8,232 / 585,988 |
| 按历史价格快照计算的成本 | CNY 3.556160 |

[公开证据摘录](evidence/glm52-smoke-20260904.json)记录了当时的代码版本、题集哈希、Docker 镜像、
patch 哈希和 grader 报告。

## Development：旧自定义题集

早期固定十题和自定义 50 题仍保留，方便复查历史实验。自定义 50 题已经标记为
`development_custom_selection`，不会作为 `v0.5.0` 正式基线。

这些运行不能与 HAL 50 题合并成一条成绩趋势。模型正常结束、Agent 测试通过和 official grader 通过
也必须分别统计。

## 正式报告将补充的内容

完成 50 题后，本页会增加：

- official grader 解决题数，分母固定为 50
- `resolved`、`unresolved`、`budget_exhausted`、`provider_error`、`infra_error`、`not_started` 分布
- 每题 attempt、停止原因和报告路径
- usage 覆盖率、总 token 和按冻结价格计算的成本
- release commit、tag、selection SHA 和 Docker image identity

运行条件和恢复规则见[评测方法](BENCHMARKING.md)。
