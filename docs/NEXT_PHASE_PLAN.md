# 下一阶段计划

## 目标

SWE-bench 接入与首批双模型基线已经跑通。下一阶段把 token、trajectory、终止状态和 external oracle
转化为可复现、可比较的优化反馈，而不是继续扩展 benchmark adapter 或统一提高预算。

## P0：评测可审计

1. 验证 GLM、DeepSeek 的 `model_usage` 覆盖率，确保每次 provider 响应都有 usage 或明确缺失；
2. 在批次结果中汇总请求数、input/output/total、cache hit/miss 和 reasoning token；
3. 用带来源与日期的价格快照计算运行成本，不把易变价格写入 Session Event；
4. 固化 selection、模型配置、Context/step budget、镜像 digest 和 Runtime commit。

完成标准：一次固定题集运行可以回答“用了多少 token、哪些可缓存、花费如何计算、结果是否完整”。

## P1：Trajectory 指标与缺陷修复

建立不依赖 LLM judge 的确定性统计：

- Agent final、validation、patch present、external oracle 四类结果；
- 正常完成、context budget、step budget、protocol/environment failure；
- 首次编辑步骤、总模型请求、工具/命令/编辑次数；
- 同 SHA 同范围重复读取、形成修改后仍持续探索；
- validation purpose 与真实测试命令不一致；
- 修改公共测试或 fixture 等范围扩张信号。

先修复可归因于 Runtime/harness 的问题，再对固定十题做对照重跑。LLM judge 只用于无法由事件、patch
或 oracle 判断的少量语义问题，不进入基础统计必需链路。

## P2：Context 优化决策

根据 P0/P1 数据决定是否推进：

1. 先调整 deterministic reduction、Observation bounding 和预算分配；
2. 最低集合仍频繁超限时，再设计可重建的 semantic condensation；
3. 只有重复源码 retrieval 成为显著成本或失败来源时，才评估显式 pin/cache；
4. 不用统一扩大 context 或 step budget 掩盖不收敛行为。

## 暂缓

- LangGraph 或其他 Agent orchestration 框架迁移；
- 隐式 Active Code、自动 Working Set、RepoMap、embedding/RAG；
- 通用 Docker/remote Workspace backend；
- 多 Agent、多 active worktree 和 Task 数据模型；
- 自动依赖安装、权限推断和自动重放失败命令；
- cgroup、复杂 seccomp、domain/port network policy。

已完成能力和双模型基线见[当前状态](PROJECT_GUIDE.md)与
[Benchmark 说明](BENCHMARKING.md)。
