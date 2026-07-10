工具策略：

- READ 工具默认允许。
- EXEC_READONLY 工具需要用户 approval。
- WRITE、NETWORK、DANGEROUS 工具在 v0.1 中拒绝。
- 所有 path 必须限制在 workspace 内，敏感文件会被 runtime 层拒绝。
