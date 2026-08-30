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

## 下一设计与实现目标：Agent Orchestration / Working Context

在继续 benchmark adapter 前，先完成正式 Technical Design 与分阶段实现：

1. 同一文件多个相关范围组成稳定 Working Set，不被最新小范围读取覆盖；
2. Tool History 与当前代码工作集分离；
3. 定义最小 Execution Checkpoint 和 slice/Resume 连续性；
4. active UserTurn 内旧闭合 tool call/result 成对缩减，保留 provider 合法边界；
5. Context 容量在失败前主动降级并提供可测试 BudgetReport；
6. 不新增 Task 实体、目录型 artifact 或未经验证的 Runtime 语义真相；
7. 使用 Fake Model/fixture 验证编排，用真实 HTTPX 任务做人工作验收。

Context Manager 继续独立于当前 ReAct loop。是否引入 LangGraph 必须等状态语义明确后决定，不能让框架替代设计。

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
