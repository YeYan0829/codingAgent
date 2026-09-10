# 评测结果

本页把开发验证和正式成绩分开记录。开发 smoke 用于发现链路问题，不能与正式 50 题合并计算。

## Final：HAL SWE-bench Verified Mini（待重新冻结）

`v0.5.0-rc.2` 原计划使用 HAL 发布的固定 50 题和 GLM-5.3。输出截断和 reasoning 上下文问题发现后，
该计划已被后续候选取代；`rc.2` 配置保留为历史证据，不产生正式成绩。下一候选尚未冻结。

| 项目 | 冻结设置或当前状态 |
| --- | --- |
| 题集 | HAL SWE-bench Verified Mini，固定 50 题 |
| 调度 | 20 Easy → 24 Medium → 6 Hard；难度来自固定版本的 `swe-bench-tasks` |
| 模型 | GLM-5.3；下一候选 reasoning `high` |
| 资源 | 全部题目统一使用 72 次模型调用上限和 8,192 单次输出上限 |
| 尝试 | 每题一个正常完成结果；中断题按恢复规则重跑并增加 attempt |
| Agent 运行 | **PENDING** |
| 解决题数 | **PENDING，不提供预测值** |
| Token 与成本 | **PENDING** |
| 逐题报告 | **PENDING** |

正式 selection 见
[hal-verified-mini-50.json](../benchmarks/swebench/selections/hal-verified-mini-50.json)。
历史机器可读运行配置见
[v0.5.0-rc2-hal-mini-50.json](../benchmarks/swebench/configs/v0.5.0-rc2-hal-mini-50.json)。

下一次正式运行必须从新的干净 release tag 启动。每题结束后立即保存结果，中断后使用同一个 run ID 恢复。

`v0.5.0-rc.1` 的首次运行发现 grader adapter 会把 empty patch 误记为基础设施失败。
该运行已经停止并作废，不计入正式成绩，也不会与 `rc.2` 的结果合并。

### rc.1 作废轨迹的 Runtime 诊断

作废运行已完成 41 题：19 resolved、4 个有 patch 的 unresolved、18 empty patch。总计 566 次模型请求中，
20 次输出恰好达到 8,192 token；其中 18 题最终 empty patch，只有 `django__django-12304` 和
`sympy__sympy-15599` 产生非空 patch。这两题作为 Runtime 修复后的定向真实回归，不计入正式成绩。

该数据支持先把 GLM reasoning 默认值从 `max` 调整为 `high`，并修复 finish reason、截断续跑和 reasoning 历史构造；
它不支持直接提高 8,192 单次输出上限。是否调整 output token budget 将在新策略的定向回归之后决定。

### Runtime reasoning `high` 两题回归

修复后的 dirty development Runtime 使用相同 8,192 输出上限和 72 步总上限重跑上述两题。两题都正常完成，
没有 `model_output_truncated`；最大单次输出分别为 4,208 和 3,495 token，因此本轮不提高 output token budget。

| Instance | rc.1 | 新回归 | 旧/新模型步骤 | 旧/新 total token | 旧/新 reasoning token |
| --- | --- | --- | ---: | ---: | ---: |
| `django__django-12304` | unresolved | resolved | 7 / 37 | 67,643 / 460,689 | 9,292 / 7,345 |
| `sympy__sympy-15599` | unresolved | unresolved，patch SHA 相同 | 10 / 12 | 149,309 / 167,557 | 17,219 / 10,925 |

合计 reasoning token 从 26,511 降到 18,270，但请求数从 17 增到 49，input token 从 187,940 增到
603,007。Django 虽然得到正确 patch，却出现明显的重复探索和多次纠错。这说明 `high` 有效降低单次长推理，
但“请求上下文只保留紧邻上一 ModelStep reasoning”的连续性与效率仍需复查，当前结果不能用于冻结发布。

后续修复以[上下文管理与语义压缩技术设计](CONTEXT_MANAGEMENT_TECHNICAL_DESIGN.md)为准：连续事件尾窗和滚动语义摘要
将取代固定四步原文窗口与历史 residue，不恢复 Active Code/Working Set。

生成结果和完整运行证据保存在本地 `benchmarks/swebench/runs/runtime-reasoning-high-regression-2-20260909/`，
不进入 Evaluation Candidate commit。

### Semantic condensation 实现后回归

2026-09-10 的 dirty Development Runtime 完成了正式 HAL 50 前验证，不计入正式成绩。

- 受控真实 GLM smoke 确定触发 1 次 condenser，随后主请求正常完成；总计 7,245 token，成本 CNY 0.063340。
- Django 优先单题在 19 个主请求内 official resolved，成本 CNY 0.853512。首次目标文件读取为
  `django/db/models/enums.py:1-81`，CCES source 为 `seq:12/seq:14`；该题始终低于 soft threshold，证据一直留在
  raw tail，没有进入 summary，也没有重新读取同一目标文件。
- 随后的原两题 selection 为 Django resolved、SymPy output-truncated。SymPy 因把 `read_file` 返回 hash 的一个字符
  抄错而反复触发安全 edit conflict，继而放大探索；44 个请求中出现 8 次长度截断，最后连续 3 次后有界停止。
  该运行没有 condenser call，因此不能归因于摘要丢失。
- Runtime 随后把紧邻长度截断的恢复请求临时降为 `low` reasoning effort。SymPy 单题复测实际出现 1 次
  `high` 截断；下一请求以 `low` 返回工具调用，再恢复 `high`，最终 19 个请求完成且 official resolved，成本
  CNY 2.568668。

这些数据支持继续保留 8,192 单次输出上限：不提高全局输出预算，而在截断的立即恢复边界压低推理强度。
真实 benchmark 题都没有达到语义压缩阈值，因此 summary 质量的真实 Provider 证据来自受控 smoke，不能把两题结果
解释为摘要效果。完整机器摘要和 Session 证据保存在本地 benchmark artifact 中，不进入 Evaluation Candidate commit。

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
