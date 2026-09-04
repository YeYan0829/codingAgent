# 下一阶段计划

## 目标

SWE-bench 接入与首批双模型基线已经跑通。正式 batch/resume、run manifest、usage/cost accounting 和
deterministic trajectory analyzer 已进入 Runtime 基线；两题 GLM 5.2 L4 smoke 已 2/2 resolved，并验证
46/46 次 provider usage、价格快照成本和真实 trajectory。下一阶段依据数据优化，不继续扩展 benchmark
adapter 或统一提高预算。

## 已完成的评测基线

- GLM smoke 的 `model_usage` coverage 已验证为 46/46；缺失 usage 仍有 explicit unavailable/partial 语义；
- 批次汇总 input/output/total、cache hit/miss 和 reasoning token；
- 成本使用带来源与日期的独立 Decimal 价格快照，不进入 Session Event；
- run manifest 固化 selection、模型配置、Context/step budget、镜像 digest 和 Runtime commit；
- context overflow 新事件记录有界 minimum-set 分类、最大 Observation identity 和估算 residual。

当前固定运行已经可以回答“用了多少 token、哪些可缓存、花费如何计算、结果是否完整”。

## P1：Trajectory 指标与缺陷修复

已建立并用真实 trajectory 校验不依赖 LLM judge 的第一版确定性统计。下一步围绕以下事实做对照分析：

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
