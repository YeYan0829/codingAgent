# 近期实施计划

## 已完成

1. Linux/WSL 执行环境收口；不再承担 Windows 双平台适配。
2. Search/Read：ignore、hidden、SensitivePath、PathGuard、symlink 和固定 Git 只读 argv。
3. Atomic Edit：多文件 create/replace/delete/move、SHA 前置条件、rollback、resume 与 Candidate patch。
4. Session/Workspace/Persistence：source-only Session、按需 worktree、accepted baseline、三方合并、accept/discard 和轻量持久化。
5. Controlled Arbitrary Command + Sandbox：`run_command`、资源权限、Bubblewrap、workspace audit、统一 Candidate revision 和 validation evidence。
6. Context Management v1：Event projection、Runtime Snapshot、deterministic reducers、Active Code、token budget、whole-turn eviction 和 Resume 重建。

旧 Command Profile / pytest-only 执行体系已删除，不再扩展 profile。

## 已完成验收

- 针对性与全量 pytest；
- 真实 Bubblewrap integration，包括 Candidate/source/Git/network 边界；
- `compileall` 与 `git diff --check`；
- 正式 CLI 的按需 upgrade、任意命令、查看、采纳/丢弃和 resume dogfood；
- 文档与代码术语一致性检查。

这些测试证明 Runtime 接入正确，不评价真实模型是否聪明地选择命令。随后五次真实 HTTPX Session 已完成，见 [真实 LLM Agent 测试审计](REAL_LLM_AGENT_EVALUATION_2026-08-30.md)。它们证明执行切片、read/search schema 和容量错误体验得到改善，也暴露当前 Working Context 无法支撑稳定长程执行。

## 下一实现目标：Context Management v2

正式设计见 [Context Management v2 TD](CONTEXT_MANAGEMENT_V2_TECHNICAL_DESIGN.md)。在继续 benchmark adapter 前，按以下顺序实现：

1. 删除 Active Code 和独立 4k code budget，不再从 read/search 行为推断重要源码；
2. 投影 `Session → UserTurn → ModelStep → ToolExchange[]`，只在 closed ModelStep 之间操作历史；open ModelStep 先进入 Runtime recovery，不发起下一次主 Agent 请求；
3. 实现 Observation Bounding、Recent Raw 和 Typed Residue；`preferred_recent_raw_steps=4` 是可在 hard pressure 下继续退化的目标，不是下限；
4. 实现 active/completed UserTurn 的 deterministic reduction、80-Event soft trigger、22k hard threshold、11k post-compaction target，以及 Mandatory/Protected/Compressible BudgetReport；
5. deterministic 机制仍不足时，再追加 OpenHands 式可重建 `context_condensed` Event 和 semantic summary；
6. 使用 Fake Model/fixture 验证协议和容量，用真实 HTTPX 任务做人工作验收；
7. 只有重复读取未变化源码被 benchmark 证明是显著成本或失败来源时，才另行设计显式生命周期的 Context cache optimization。

Context Manager 继续独立于当前 ReAct loop。本轮不引入 LangGraph，也不先建设 Execution Checkpoint；先验证无 Active Code 的 Event/View/Condensation 基线。

## 后续目标：SWE-bench 薄 adapter

SWE-bench 薄 adapter：

SWE-bench 采用薄 adapter，不预先设计大型通用 backend：

```text
benchmark task/repository/environment
→ 启动正式 Session Runtime
→ Agent 使用相同 Search/Read、Atomic Edit、run_command
→ 导出当前修改的最终 patch
→ 官方 harness 独立验证与评分
```

adapter 不绕过 ToolRegistry、Policy、Approval、Bubblewrap 或 Candidate/accept 边界。若 benchmark 已在容器内运行，是否允许 nested Bubblewrap 必须通过实际 capability probe 决定；不能自动降级为裸执行。Working Context 验收完成前不开始实现 adapter，避免把已知 Agent loop 缺陷带入 benchmark。

## 暂缓

- Task identity、多 Agent、多 active worktree；
- Docker/remote executor 产品化；
- cgroup、复杂 seccomp、domain/port network policy；
- 自动安装依赖或自动推断权限；
- validation classifier、命令质量判断；
- 通用 secret/token 文件名扫描器。
- 隐式 Active Code / Working Set、RepoMap、embedding 或 RAG；
- LangGraph 与语义 Execution Checkpoint。
