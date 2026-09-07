# 评测结果

本页只列出能够追溯到现有记录的结果。开发过程中的数字不能自动视为正式成绩。

正式成绩必须说明使用了哪版代码、哪个模型、哪些题目和什么运行环境。
尚未运行的评测保持 **pending**，不会填写预测值。

## 正式固定 50 题

项目已经选定一个固定的 SWE-bench Verified 50 题子集。
固定题目可以避免每次运行使用不同样本。

| 项目 | 当前状态 |
| --- | --- |
| 题目 | 19 Easy、26 Medium、5 Hard，共 10 个仓库 |
| 环境检查 | 选题文件记录所有入选题曾通过 gold preflight；本轮未重跑 |
| 计划模型 | GLM 5.2，最终以正式运行记录为准 |
| Agent 运行 | **Pending，尚未运行** |
| 解决题数 | **Pending，不提供预测值** |
| Token、成本和时延 | Pending |
| 逐题报告 | Pending |

“Gold preflight”是运行官方答案的环境检查。它只能证明题目和测试环境当时可以工作，
不能证明 CodeAgent 解决了这些题。

题目列表和 Docker 环境信息见[固定 50 题文件](../benchmarks/swebench/verified-eval-50.json)。
完整环境检查报告仍待整理为公开证据。

## 2026-09-04：GLM 5.2 两题 smoke

这次运行只用于检查评测链路。题目只有两个，不能代表模型的整体修复能力。

运行时的代码目录有未提交修改。因此，这组结果也不是冻结版本的发布成绩。

| 指标 | 结果 |
| --- | ---: |
| 固定题集 | `verified-smoke-2-v1` |
| Runtime commit | `7f8dc4019b3838a5f717cff0b3f3a7f5b62f9446`，运行时 dirty |
| 模型 | GLM 5.2 standard API，thinking disabled |
| 模型调用上限 | 每个请求 48 次 |
| official grader 通过 | 2/2 |
| Agent 正常结束 | 2/2 |
| Agent 测试通过 | 2/2 |
| 有 token 记录的响应 | 46/46 |
| Token | input 577,756；output 8,232；total 585,988 |
| 缓存输入 / 非缓存输入 | 216,064 / 361,692 |
| 按价格快照计算的成本 | CNY 3.556160 |

两题是 `pytest-dev__pytest-7205` 和 `sympy__sympy-20590`。

成本使用[2026-09-04 GLM 价格快照](../benchmarks/swebench/prices/glm-5.2-standard-api-2026-09-04.json)计算。
这是按保存费率推导的结果，不是账单金额。

[公开证据摘录](evidence/glm52-smoke-20260904.json)记录了代码版本、模型设置、题目文件哈希、Docker 镜像、
patch 哈希和 official grader 报告。

这个摘录只能证明列出的两次运行。它没有完整记录之前是否发生过重试，也没有公开完整执行轨迹。

## 开发探索不列为成绩

开发期间曾用[固定十题](../benchmarks/swebench/verified-first-10.json)比较 GLM 和 DeepSeek。

这些运行缺少完整的代码版本、重试记录和公开结果文件。因此，本页不再展示原先的成绩表。

开发运行仍说明了一件事：模型正常结束不等于修复成功。
Agent 自己的测试和 official grader 也可能给出不同结果。
正式报告会分别统计它们。

## 正式报告将包含什么

完成 50 题后，本页会增加以下指标：

| 指标 | 含义 |
| --- | --- |
| 解决题数 | official grader 通过的题数，分母固定为 50 |
| 正常结束率 | Agent 在预算内正常给出最终结果的比例 |
| 完整交付率 | Agent 正常结束、有修改、测试通过且 grader 通过的比例 |
| 失败分布 | 区分模型预算、Runtime、环境和 grader 失败 |
| Token 与成本 | 包含所有运行和重试，并说明计量是否完整 |
| 时延 | 分别报告环境准备、Agent 执行和 grader 时长 |
| 分组结果 | 按难度和代码仓库展示题数，不从少量题目外推总体表现 |

评测如何固定环境、恢复批次和计算这些指标，见[评测方法](BENCHMARKING.md)。
