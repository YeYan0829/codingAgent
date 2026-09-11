# v0.5.0 评测结果

本页记录 CodeAgent `0.5.0` 的正式 HAL SWE-bench Verified Mini 50 结果、运行可靠性和失败边界。开发 smoke、历史
calibration 和未完成运行不与正式成绩合并。

## 正式结果

| 项目 | 结果 |
| --- | --- |
| Benchmark | HAL SWE-bench Verified Mini，固定 50 题 |
| Evaluation Candidate | `932b52ffff4209a748c6646a056d2ab43111637e`，clean worktree |
| Provider / model | GLM / `glm-5.3` |
| Sampling | temperature `1.0`，reasoning `high` |
| 资源上限 | 单次输出 8,192 token；每题最多 72 个主 ModelStep |
| Official grader resolved | **35 / 50（70%）** |
| 难度分布 | Easy 16/20；Medium 17/24；Hard 2/6 |
| Outcome | 35 resolved；13 unresolved；2 budget exhausted |
| Patch | 50 / 50 非空 |
| Provider / infrastructure error | 0 / 0 |
| 正式运行成本 | CNY 150.040204 |

难度来自固定版本 `swe-bench-tasks` 的 `task.yaml`，用于执行排序，不是 HAL 官方标签，也不改变题目配置。

正式运行身份：

| 身份 | 值 |
| --- | --- |
| Run ID | `v0.5.0-evaluation-candidate-932b52f-hal-mini-50` |
| Selection ID | `hal-swebench-verified-mini-50-7b231a9` |
| Selection SHA-256 | `852042b05e2a1a95fd8e7dd01448f367aa8e332baf58eadbbd53167c2d90001f` |
| Model config fingerprint | `c5c997fd066a693189d4543d248bb1dfcc42bd17c44282cb61c34f2bf9472bb1` |
| SWE-bench commit | `334882dd1f2664cc55c1abfe9de4884af023c0c0` |
| Task repository commit | `3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d` |

[机器可读发布证据](evidence/v0.5.0-hal-mini-50.json)保存核心数字、未解决题目、usage、成本和本地正式 artifact 的
SHA-256；[固定 selection](../benchmarks/swebench/selections/hal-verified-mini-50.json)与
[运行配置](../benchmarks/swebench/configs/v0.5.0-evaluation-candidate-hal-mini-50.json)进入版本控制。包含完整 Session、模型
事件和 grader 路径的 `benchmarks/swebench/runs/` 按仓库策略保存在本地，不作为源码发布物提交。

## Runtime 运行结果

50 道题总计产生 1,229 次有完整 usage 的 Provider 响应：

| Runtime 指标 | 结果 |
| --- | ---: |
| 正常结束 Agent loop | 48 / 50 |
| Model-step budget exhausted | 2 / 50 |
| Context exceeded | 0 |
| 输出截断事件 | 25，分布于 13 题 |
| Semantic condensation | 7 / 7 成功 |
| Protocol error | 1，后续恢复 |
| Provider / infrastructure error | 0 / 0 |
| Retry overhead attempt | 0 |

总 usage 为 31,582,505 input token、940,359 output token、32,522,864 total token；其中 cached input 为
21,491,648，reasoning token 为 714,244。按冻结价格快照计算，本次固定评测成本为 CNY 150.040204；这不是运行产品的
固定成本。

输出截断和语义压缩更多出现在较长、较难的任务中。发生截断的 13 题中 5 题 resolved，未发生截断的 37 题中 30 题
resolved；该相关性受到任务长度和难度混杂，不能解释成截断导致失败。

对发生压缩的失败轨迹抽查显示，摘要保留了任务目标、修改文件、验证证据和 pending 工作，压缩后 Agent 继续执行了工具调用。
本轮没有识别到由压缩信息断裂直接造成的失败；这不等于证明压缩对所有任务的成功率没有影响。

两道 hard 题在 72 个主 ModelStep 后终止：

- `django__django-15629`：创建 FK 的目标测试通过，但修改已有 PK collation 的路径仍失败；
- `sphinx-doc__sphinx-9461`：实现未收口，三个目标测试仍失败。

步骤上限直接影响了这两题的 outcome，但继续增加预算不保证修正其实现方向。本次发布不提高预算，也不重跑正式 50 题。

## 失败分布

| 难度 | Resolved | Unresolved / budget exhausted |
| --- | ---: | ---: |
| Easy | 16 / 20 | 4 |
| Medium | 17 / 24 | 7 |
| Hard | 2 / 6 | 4 |

最后 10 个执行位置为 4/10，其中包含全部 6 道 hard。后半程通过率下降与固定的 Easy → Medium → Hard 调度一致，不能解释为
Runtime 随运行时间退化。

按主要失败原因归类：

| 类别 | 数量 | 说明 |
| --- | ---: | --- |
| 任务语义错误或实现覆盖不足 | 12 | 找到相关代码但边界、公开 API、backend 分支或兼容行为不符合 official tests |
| Official test patch 组合冲突 | 1 | `astropy__astropy-7336` 修改了官方补丁随后重命名的测试文件 |
| ModelStep 预算耗尽 | 2 | 已产生非空但未完成的 Candidate patch |
| Provider / infrastructure failure | 0 | 正式运行没有此类中断 |

13 道正常结束但未解决的任务都被 Agent 记录为 `validation_passed=true`。这说明当前 validation 只能证明所选命令在对应
Candidate 上成功退出，不能证明测试覆盖了真实规格。失败补丁常见模式是模型新增的测试与自己的实现假设一致，但没有覆盖相邻
兼容行为或 official test 的反例。

## 逐题失败摘要

| Instance | 主要原因 |
| --- | --- |
| `sympy__sympy-18763` | `strict=True` 使乘法表达式没有按要求加括号。 |
| `astropy__astropy-7336` | 预测补丁与官方测试文件重命名冲突，grader 无法组合应用；实现范围也宽于原需求。 |
| `matplotlib__matplotlib-23476` | 保留 `_original_dpi`，但没有在序列化时丢弃设备像素比放大的 `_dpi`。 |
| `sympy__sympy-15875` | 把无法可靠判断的 `is_zero` 返回为 `True`，目标要求保守返回 `None`。 |
| `django__django-13346` | 用 numeric lookup mixin 处理 JSON `__in`，未覆盖不同 backend 和复合 JSON RHS。 |
| `django__django-13809` | 跳过了检查调用，但仍输出 “Performing system checks...” 提示。 |
| `django__django-14725` | 自行设计 `can_create=False`，正式公开 API 要求 `edit_only=True`。 |
| `sympy__sympy-17318` | 在 `split_surds()` 防御空输入，没有在 `_sqrt_match()` 排除非正有理平方项，并写错目标期望。 |
| `django__django-16950` | 目标测试通过，但改变所有默认 key 行为，造成一个既有测试回归。 |
| `sympy__sympy-20428` | 修复 dense multiplication 的 strip，未修正 `ExpressionDomain.Expression.__bool__` 的根因。 |
| `scikit-learn__scikit-learn-25747` | 只在索引长度变化时保留 DataFrame 索引，遗漏等长但标签不同的情况。 |
| `pylint-dev__pylint-4551` | 只扩展 inspector 推断，未完成 Optional 标签、writer 输出和 diagram 支持。 |
| `django__django-14011` | 直接关闭线程连接，未处理共享的 SQLite 内存连接，导致 LiveServer 测试大面积失败。 |
| `django__django-15629` | 预算耗尽；遗漏 collation-only 变更时的 FK 重建和 SQLite 路径。 |
| `sphinx-doc__sphinx-9461` | 预算耗尽；遗漏 Python domain 的 class-property directive 支持，补丁仍有临时调试测试。 |

上述分析来自正式 prediction、official grader 状态、固定 gold/test patch 与 Session trajectory 的离线对照。Gold patch 和
official tests 在 Agent 执行时不进入模型 Context。

## 结果能证明什么

本轮支持以下结论：

- `0.5.0` Runtime 从干净候选提交完成了完整 50 题 repository-level 运行；
- 所有题目产生非空 patch，且没有 Provider 或基础设施错误；
- Context 压缩调用均正常完成，输出截断恢复没有形成最终 `output_truncated` outcome；
- 主要剩余瓶颈是规格对齐和验证充分性，而不是已观察到的 Context transport 或基础设施稳定性。

本轮不支持以下结论：

- CodeAgent 达到 SOTA；
- 70% 是基础模型或 Runtime 单独的能力；
- 单次 temperature `1.0` 运行能够估计方差；
- 语义压缩对任务正确率没有影响；
- Agent 自报 validation success 等于 official resolved。

## Development 证据

以下记录只用于解释参数和链路选择，不计入正式 35/50：

- [GLM-5.3 tool roundtrip smoke](../benchmarks/swebench/smoke/glm-5.3-tool-roundtrip-2026-09-09.json)：验证
  reasoning、工具回合、usage 与成本链路；
- [GLM-5.2 历史 smoke](evidence/glm52-smoke-20260904.json)：验证 Docker、patch 导出和 official grader；
- 历史 hard calibration：用于选择统一 72-step 上限；
- 未完成的 dirty HAL partial：用于发现 validation `pipefail`、UI 文案和 batch attempt 记账问题。

这些结果使用不同代码状态、模型或完成范围，不与正式成绩拼接。评测数据流、恢复和 artifact contract 见
[SWE-bench 评测方法](BENCHMARKING.md)。
