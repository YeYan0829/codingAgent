工具策略：

- READ 工具默认允许。
- EXEC_READONLY 工具需要用户 approval。
- CANDIDATE_WRITE 只允许已注册的受控编辑工具操作 Session 的 Agent worktree。采纳前的固定 patch 由 Runtime 内部流程产生，不向 Agent 开放工具。
- 普通 WRITE、NETWORK、DANGEROUS 工具仍然拒绝。
- 所有 path 必须限制在 workspace 内，敏感文件会被 runtime 层拒绝。
