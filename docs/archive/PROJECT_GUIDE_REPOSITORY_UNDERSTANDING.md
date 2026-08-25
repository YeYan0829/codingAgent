# 当前设计上下文

> **Historical design：** 本文记录已停止推进的 Repository Understanding、Project Model 和 Shared Project View 纵向切片。它不再是当前设计入口；当前状态见[当前设计上下文](../PROJECT_GUIDE.md)。

本文是 CodeAgent 当前设计阶段的入口。它只回答三个问题：

1. 项目现在处于什么阶段；
2. 下一步正在下沉哪个逻辑能力；
3. 开始 Technical Design 前还有哪些问题没有决定。

项目为什么存在见 [System Vision](../系统目标.md)，当时的逻辑目标架构见 [Target Architecture](TARGET_ARCHITECTURE.md)。当前 v0.4 Runtime 的实际代码结构和行为见 [当前 Runtime 实现](../ARCHITECTURE.md)。

## 1. 当前阶段

CodeAgent 已有：

- 一份稳定的 System Vision；
- 一份当前认可的逻辑 Target Architecture；
- 一个已经实现并验证的 v0.4 Runtime 底座，包括只读工具、Policy/Approval、Git worktree、受控 pytest、受控文本 patch、不可变 Candidate 和显式 Apply。

当前缺少的不是总体架构，而是把目标架构中的逻辑组件继续下沉为可实现的 Technical Design。

Technical Design 需要明确输入输出、数据模型、接口、技术选型、生命周期、失败语义和测试策略。在这些问题确定前，不把逻辑架构图中的组件直接映射成 Python 类，也不假设旧 v0.5 VS Code 方案仍是当前实施路线。

## 2. 首个设计方向

首个方向是 Repository Understanding，但目标不只是提高 Agent 的检索或上下文选择能力，而是完成下面的纵向闭环：

```text
Repository evidence
→ Repository Observations
→ Project Model Snapshot
→ Shared Project View
→ 追问 / 解释 / 补充 / 提案
→ 后续 Design / Planning
```

目标是建立一个有证据、可检查、可追问、可扩展的项目理解表示：

- Agent 用它组织任务相关上下文和形成高层理解；
- 开发者用它理解模块、关系、数据流和判断依据；
- 双方可以围绕同一个表示继续追问、展开证据和讨论未来设计；
- 用户可以补充信息或加入尚未实现的模块，但不必先“修正 Agent”才能继续；
- 讨论结果可以成为后续 Design Preview 的输入。

这是一条跨越 Repository Understanding 和 Shared Project View 的纵向切片，不意味着把两者实现成同一个模块。

## 3. Project Model 与 Shared Project View

两者职责必须分开：

| 概念 | 职责 | 不负责 |
| --- | --- | --- |
| Project Model | 保存带来源、版本和失效语义的项目理解，供 Agent 和其他组件消费 | 决定具体 UI 布局或交互控件 |
| Shared Project View | 将 Project Model 投影成人和 Agent 可以共同引用、导航和讨论的表示 | 成为第二套项目事实或绕过 Runtime 读取代码 |

Shared Project View 不只是 repository map。它至少应支持：

- 查看模块职责、依赖、调用或关键 execution/data flow；
- 只显示与当前任务相关的部分；
- 追问“为什么这样判断”并展开文件、symbol 或其他证据；
- 请求进一步解释、比较其他解释或继续探索；
- 补充领域信息；
- 表达尚未实现的模块、关系或候选设计；
- 将选定内容带入后续 Design / Planning。

第一版不预设具体图形库、前端框架或图数据库。

## 4. 必须区分的信息类型

Project Model 和 View 不能把代码事实、模型推断和未来设计混为一谈。Technical Design 至少需要决定如何表达：

| 类型 | 含义 |
| --- | --- |
| Observed | 可由当前代码、Git、symbol 或工具结果直接验证的事实 |
| Inferred | Agent 基于证据形成的解释或关系 |
| User-provided | 用户提供、但未必能从代码直接验证的领域信息 |
| Proposed | 尚未实现的模块、关系或设计方案 |
| Open question | 双方尚未确认、需要继续探索的问题 |

所有与代码有关的内容还需要绑定 source identity。代码变化后，旧观察和推断必须能够被标记为 stale，不能静默继续充当当前事实。

用户“加入一个模块”可能是在补充 Agent 遗漏的现有模块，也可能是在提出未来模块；系统必须保留这两种语义的区别。

## 5. 调研范围

本轮不开发新算法。Technical Design 前先调研并比较可以集成的现有实践，重点回答：

1. 现有 Coding Agent 如何建立 repository map、定位相关代码并选择上下文；
2. AST、symbol、引用、调用关系、词法检索和模型推断分别适合提供哪些证据；
3. 如何把大型项目理解压缩为任务相关、可逐层展开的表示；
4. 现有代码结构、架构或 execution-flow 工具如何支持可视化和追问；
5. 哪些 benchmark 可以验证 file/symbol localization、repository QA 和 change-impact prediction；
6. 哪些人机理解效果只能通过场景测试、观察和定性研究评估。

调研结论需要记录来源、适用边界、替代方案和采用理由。为了避免再次产生文档分叉，第一轮调研先进入 Repository Understanding Technical Design 的 “Research Basis” 章节；只有材料明显影响正文可读性时才拆出独立 Research Artifact。

## 6. 评估边界

不为“用户是否完全理解项目”设计一个虚假的单一分数。评估分三层：

### 可重复的后端能力

- file / symbol localization；
- repository-level question answering；
- 相关测试定位；
- 任务相关上下文的召回、噪声和成本；
- 可验证关系的正确性；
- source 变化后的 stale detection。

### 表示与状态一致性

- 事实、推断、用户补充和未来提案是否明确区分；
- 关系是否能追溯到证据；
- 用户新增的 Proposed 模块是否不会被当成当前实现；
- 用户追问和补充是否绑定正确的 Project Model 版本；
- Agent 后续回答和 Design Preview 是否消费了正确版本。

### 人机协作效果

通过具体理解任务观察：

- 用户能否正确解释一条关键流程；
- 能否找到结论的证据；
- 能否发现 Agent 的错误或不确定判断；
- 追问后理解是否改善；
- 能否用同一视图表达未来模块并进入后续设计。

早期允许使用少量场景、观察记录和用户反馈，不要求立即形成综合 benchmark。

## 7. 首个验证场景

使用当前 CodeAgent 仓库自身作为 fixture，完整验证场景包含三个连续阶段：

- Understand：解释一次 readonly exploration 请求，生成 `AgentRunner`、Policy、`ToolRegistry`、`SessionStore` 等组件的局部视图，并支持追问和展开代码证据；
- Collaborate：用户在同一视图加入 Proposed `Repository Understanding` 模块，双方讨论它与当前组件的可能连接，同时保持 current、proposed 和 open question 的区别；
- Carry Forward：把选定视图投影为带 source identity、证据、提案和开放问题的 Design Context，供后续 Technical Design 使用。

完整 10 步流程及验收契约以 [Repository Understanding Technical Design 第 2 节](REPOSITORY_UNDERSTANDING_TECHNICAL_DESIGN.md#2-完整验证用例)为权威来源。第一版可以使用 CLI 或简单交互原型呈现，不把完整 IDE 作为前置条件。

## 8. 当前 Technical Design

[Repository Understanding Technical Design](REPOSITORY_UNDERSTANDING_TECHNICAL_DESIGN.md) 已形成 Proposed 版本，统一维护本纵向切片的输入输出、数据模型、接口、生命周期、失败语义、技术选择和测试策略。

该文档目前用于设计评审，不表示 Repository Understanding 已经实现。在设计获得确认前，不开始大规模实现，也不创建额外 Roadmap、User Stories 或 Application Layer 文档。

## 9. 当前开放问题

前述核心问题已经在 Technical Design 中形成第一版选择。仍需通过实现前 spike 关闭的 decision gates 是：

- 检索预算和停止条件的默认值；
- 首个交互 renderer 在 React Flow 与 Cytoscape.js 之间的选择；
- CLI 或本地 Web adapter 的首个交互 transport；
- RepoQA Python smoke subset 的固定 case 和模型配置。

Decision gates、采用理由和延期能力统一维护在 Technical Design，不在本文重复展开。

## 10. 阅读和实施入口

- 理解产品目标：[System Vision](../系统目标.md)。
- 理解逻辑组件和边界：[Target Architecture](TARGET_ARCHITECTURE.md)。
- 理解当前 Repository Understanding 方向和评估边界：本文。
- 评审具体技术设计：[Repository Understanding Technical Design](REPOSITORY_UNDERSTANDING_TECHNICAL_DESIGN.md)。
- 核对已有 Runtime 接入位置：[当前 Runtime 实现](../ARCHITECTURE.md)。
- 运行自动化测试：[测试说明](../TESTING.md)。
- 验收当前 v0.4 底座：[人工验收](../MANUAL_TEST.md)。
- 回溯旧路线和历史决策：`archive/`。
