# 当前设计上下文

本文是当前设计状态的最小入口。项目目标见 [System Vision](系统目标.md)，已经实现的 v0.4 Runtime 结构和行为见[当前 Runtime 实现](ARCHITECTURE.md)。

## 1. 当前状态

截至 2026-08-24，原 Repository Understanding、Project Model 和 Shared Project View 纵向切片已经停止推进并归档。该方案把代码理解、context engineering、Agent loop、artifact model 和交互视图放进同一个设计，超出了当前实践所需的复杂度。

当前仍然有效的是：

- v0.4 Runtime 及其 safety、Policy、Approval、Session、worktree、Candidate 和 Apply 边界；
- System Vision 表达的长期产品问题和目标；
- 新能力必须复用 Runtime 的受控工具和安全边界。

当前没有已确认的 v0.5 Target Architecture 或组件 Technical Design。不要把归档方案当作下一步实施计划。

## 2. 下一步状态

初步 capability inventory 表明，当前更紧迫的问题是补齐基础 Coding Agent 的工具、执行、评估和展示闭环。绘图 MCP 服务或插件仍可作为后续候选，但不应早于“能用、能测、能展示”的基础能力。

详细代码审计、主流产品对照、JD 样本和优先级判断见 [v0.4 Coding Agent Capability Inventory](CAPABILITY_INVENTORY.md)。

下一轮规划尚未形成 Technical Design。需要先回答：

- 第一批代表性 Coding Task 和样例仓库是什么；
- 为完成这些任务，最小的搜索、编辑和命令工具面是什么；
- 可配置命令怎样继续遵守现有 Policy、Approval 和 workspace 边界；
- 任务级 eval 的成功条件、回归指标和固定数据集是什么；
- 怎样用简单 task trace 展示 Agent 的探索、修改和验证过程。

在这些问题确认前：

- 不实现新的 Repository Understanding 子系统；
- 不继续扩展已归档的 Project Model schema；
- 不预先选择复杂 UI、图数据库、RAG、长期记忆或 multi-agent 方案；
- 不新增 Roadmap、User Story 或 Application Layer 文档。

## 3. 阅读入口

- 项目为什么存在：[System Vision](系统目标.md)；
- 当前已经实现什么：[当前 Runtime 实现](ARCHITECTURE.md)；
- 当前能力缺口和 JD 调研：[v0.4 Coding Agent Capability Inventory](CAPABILITY_INVENTORY.md)；
- 怎样运行测试：[测试说明](TESTING.md)；
- 怎样人工验收 v0.4：[人工验收](MANUAL_TEST.md)；
- 回溯已暂停的目标架构、Repository Understanding 方案和旧版本设计：`archive/`。
