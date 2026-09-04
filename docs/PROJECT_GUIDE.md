# CodeAgent 当前状态

本文是项目文档入口，只描述当前阶段。长期方向见[系统目标](系统目标.md)，落地结构见
[当前架构](ARCHITECTURE.md)，近期工作见[下一阶段](NEXT_PHASE_PLAN.md)。

## 当前基线

CodeAgent Runtime `0.5.0` 已形成可执行、可恢复、可自动评测的本地 Coding Agent 基线：

- Session 初始直接探索 source repository；首次编辑或执行命令时，经批准动态创建专属 Git worktree；
- Search/Read、原子多文件编辑、Candidate 查看/采纳/丢弃、Session resume 已形成闭环；
- 正式 CLI 命令在 per-command Bubblewrap 中运行，默认断网且限制宿主写入；
- Context 由 append-only Event 按每次 API 调用确定性投影，不再维护隐式 Active Code；
- DeepSeek 与 GLM 通过同一 OpenAI-compatible gateway 接入；provider usage 会写入独立审计事件；
- SWE-bench 已跑通 source preparation、Docker execution projection、gold preflight、单题运行、official grader
  和首批十题基线；
- 正式 batch/resume、run manifest、usage/cost accounting 与 deterministic trajectory analyzer 已形成闭环；
- context overflow Event 会记录有界的 minimum-set 分类估算与最大 Observation 来源。

当前自动化基线为 `226 passed, 3 skipped`；其中真实 Bubblewrap/Docker 测试按环境显式启用。

## Benchmark 结论

首批十题使用同一题集和 48-step 总预算：

| 模型 | External oracle | 正常完成 | Context 超限 | Step 超限 |
| --- | ---: | ---: | ---: | ---: |
| GLM 5.2 | 8/10 | 9/10 | 0 | 1 |
| DeepSeek V4 Pro | 8/10 | 3/10 | 6 | 1 |

相同 oracle 分数没有代表相同的 Agent 生命周期质量。DeepSeek 多题在已经生成有效 patch 后仍因当前
32k/22k usable context 配置终止；GLM 的行动收敛和完成状态明显更稳定。完整评测口径、数据位置和
复现边界见 [BENCHMARKING](BENCHMARKING.md)。

两题 GLM 5.2 L4 smoke 已通过正式 batch 运行：两题均正常完成并由 official grader resolved，46/46 次
provider response usage 完整，总计 585,988 token，价格快照成本为 ¥3.556160。

## 当前开放问题

评测基础设施已经收口。下一阶段使用现有确定性证据驱动优化：

1. 用固定 selection 对 Runtime/context 改动做可复现对照；
2. 根据 context breakdown、post-edit steps、重复读取和 validation 事实定位收敛问题；
3. 先优化 deterministic reduction、Observation bounding 和预算分配；
4. 只有数据证明最低集合仍频繁超限时，再设计 semantic condensation；
5. Working Set cache、LangGraph 和通用容器 Workspace backend 继续暂缓。

## 阅读顺序

| 文档 | 用途 |
| --- | --- |
| [README](../README.md) | 安装、provider 配置和 CLI 快速使用 |
| [ARCHITECTURE](ARCHITECTURE.md) | 当前对象、调用链、安全边界和源码入口 |
| [BENCHMARKING](BENCHMARKING.md) | SWE-bench 架构、结果语义、基线和复现要求 |
| [DECISIONS](DECISIONS.md) | 仍约束后续实现的决策 |
| [NEXT_PHASE_PLAN](NEXT_PHASE_PLAN.md) | 尚未完成的近期工作 |
| [TESTING](TESTING.md) | 自动化与真实环境测试 |
| [MANUAL_TEST](MANUAL_TEST.md) | 正式 CLI dogfood |

已完成 TD、阶段调研和历史评测统一保存在 [`archive/`](archive/)，仅用于追溯，不是当前实现依据。
当前事实冲突时以代码和测试为准。
