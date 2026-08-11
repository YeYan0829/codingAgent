工具策略：

- READ 工具默认允许。
- EXEC_READONLY 工具需要用户 approval。
- CANDIDATE_WRITE 只允许已注册的受控工具操作 execution task worktree 或冻结 artifact。
- 普通 WRITE、NETWORK、DANGEROUS 工具仍然拒绝。
- 所有 path 必须限制在 workspace 内，敏感文件会被 runtime 层拒绝。
