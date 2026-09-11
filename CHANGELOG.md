# Changelog

本文件记录面向使用者的变化。

## 0.5.0 - 2026-09-11

### Added

- Tool Calling Runtime、Session Event 和有界上下文构建；
- Git worktree Candidate、原子文本编辑、validation evidence、Accept/Discard；
- Bubblewrap 命令隔离、文件系统/网络权限请求和命令环境 provenance；
- VS Code 右侧栏会话时间线、全局 History、审批、Stop、本轮预算扩额和原生 Diff；
- DeepSeek、GLM 和确定性 Fake provider；
- SWE-bench 官方镜像准备、Docker 命令投影、grader、batch resume、usage/cost 与轨迹摘要；
- 离线产品 demo 和三个 real-repository dogfood 任务；
- 连续 CCES raw tail、rolling semantic condensation、Current Task Anchor 和有界截断恢复；
- 语义压缩与截断恢复的低干扰 Timeline 提示、batch task phase/timestamps 及 main/condenser 分项记账。

### Fixed

- Validation 命令现在仅在 `purpose=validation` 下启用 Bash `pipefail`，管道前段失败不再生成成功证据；
- UI 不再把 validation command success、当前 revision evidence 和 official grader 混称为 “Validation passed”；
- Timeline 不再展示语义摘要正文、token/model 诊断或截断恢复 JSON；
- Batch 保存 Provider/基础设施失败 attempt 的 trajectory、usage 和 cost，并在恢复后将其作为 retry overhead 纳入成本保护；
- 无 condenser 调用时按完整数值零记账，GLM 旧配置缺省 effort 统一迁移为 `high`。

### Evaluation

- 在 commit `932b52ffff4209a748c6646a056d2ab43111637e` 的干净工作树上完成 HAL SWE-bench Verified Mini 50：
  official grader resolved 35/50；50/50 产生非空补丁；48/50 在 72-step 预算内结束；0 provider error，0 infrastructure error。
- 正式结果为单次 GLM-5.3、temperature `1.0` 运行，用于验证端到端 repository-level workload，不声明 SOTA 或运行间方差。
