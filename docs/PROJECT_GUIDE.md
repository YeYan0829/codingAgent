# CodeAgent 当前状态与文档入口

本文是项目文档总入口，只回答“当前实现到哪里、不同文档分别负责什么”。安装和首次运行从
[README](../README.md) 开始；发生冲突时，以代码、测试和[当前架构](ARCHITECTURE.md)为准。

## 当前基线

CodeAgent Runtime `0.5.0` 已形成可执行、可恢复、可审阅的本地 Coding Agent 产品原型：

- Session 从 source-only 开始，首次受保护操作经审批后创建专属 Git worktree；
- Search/Read、原子编辑、Bubblewrap 受控任意命令、Validation、Accept/Discard 和 resume 已闭环；
- DeepSeek/GLM、确定性 Context 投影、usage 审计与 SWE-bench adapter 已落地；
- VS Code Chat View 已支持配置、历史、Run/Continue、实时活动、Stop/Approval、原生 Diff 和 Changes 交付；
- Command Environment v1 已统一实际进程、Runtime Snapshot 与 Event provenance，不引入语言检测器；
- 离线产品主链路及 environment/network Allow、Reject 分支已经完成人工 dogfood。

当前 L0 自动化基线为 `274 passed, 3 skipped`。真实 Bubblewrap、Docker 与付费模型评测属于独立证据层，
不由默认 pytest 结果代替；运行口径见[测试说明](TESTING.md)。

## 当前边界与下一步

当前仍只支持 Linux/WSL2 与满足准入条件的本地 Git 仓库。网络授权是 host network，不是域名/端口白名单；
命令没有 cgroup 配额；Runtime 不自动配置项目环境、不判断验证充分性，也不解决 Git 合并冲突。

下一步以 GitHub 展示和真实仓库基线为目标，完成安装包实测、固定版本验收、视频/截图与评测证据整理；
正式 50 题运行前还需落实恢复身份核对、全部 attempt 计量和结果导出。产品功能已具备录制主链路的条件。
原系统目标中的调研/设计产物不再属于当前交付范围。详细判断见[展示导向现状梳理](SHOWCASE_READINESS.md)。
Session 环境复用、环境配置应用流程和环境可见性仍需独立设计，不能混入代码 Accept，也不是当前展示前置条件。
具体列表见[下一阶段计划](NEXT_PHASE_PLAN.md)。

## 文档分工

| 文档 | 唯一职责 |
| --- | --- |
| [README](../README.md) | 产品简介、安装、CLI/VS Code 快速入口 |
| [ARCHITECTURE](ARCHITECTURE.md) | 已落地模块、数据流与安全不变量 |
| [DECISIONS](DECISIONS.md) | 仍然有效的跨阶段决策 |
| [PRODUCT_RPC](PRODUCT_RPC.md) | Extension/Runtime 协议、配置和开发调试 |
| [PRODUCT_ACCEPTANCE](PRODUCT_ACCEPTANCE.md) | VS Code 人工验收步骤与证据口径 |
| [COMMAND_ENVIRONMENT_CONTRACT](COMMAND_ENVIRONMENT_CONTRACT.md) | 命令环境事实、继承、隔离和 Approval 边界 |
| [CHAT_TIMELINE_UX_DESIGN](CHAT_TIMELINE_UX_DESIGN.md) | 当前会话时间线与微交互规范 |
| [TESTING](TESTING.md) | 自动化、真实环境测试层级及其不证明的内容 |
| [BENCHMARKING](BENCHMARKING.md) | SWE-bench 数据、运行、结果和成本口径 |
| [NEXT_PHASE_PLAN](NEXT_PHASE_PLAN.md) | 尚未完成且已经排好优先级的工作 |
| [MANUAL_TEST](MANUAL_TEST.md) | CLI 专项 dogfood，不代替 VS Code 产品验收 |
| [系统目标](系统目标.md) | 当前产品定位、展示交付范围、完成标准与非目标 |
| [SHOWCASE_READINESS](SHOWCASE_READINESS.md) | 2026-09-06 展示现状、简历措辞对齐、首页/视频方案与评测差距 |

[VS Code 产品层 Technical Design](VS_CODE_PRODUCT_TECHNICAL_DESIGN.md)保留 Phase 0～5 的设计推导，当前行为以
Product RPC、UX 规范和 Product Acceptance 为准。更早的路线、研究与评测位于 [`archive/`](archive/)，只用于
追溯，不作为当前接口依据。
