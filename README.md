# CodeAgent v0.5.0

CodeAgent 是运行于 Linux／WSL2 的本地 Coding Agent。它在独立 Git worktree 中调查、修改和验证代码，通过 VS Code 的时间线、审批与原生 Diff 展示执行过程；开发者审查后使用 Accept 或 Discard 处理候选修改。

本版在固定 HAL SWE-bench Verified Mini 50 题上，使用 GLM-5.3／high 完成 **35/50（70%）**，正式模型费用估价为150.040204元。题单、配置、运行可靠性与逐题失败分析见[测评报告](docs/HAL50_EVALUATION.md)。

- [安装与使用](docs/INSTALLATION.md)
- [架构设计](docs/ARCHITECTURE.md)
- [测评报告](docs/HAL50_EVALUATION.md)
- [v0.5.0发布附件](https://github.com/YeYan0829/codingAgent/releases/tag/v0.5.0)

下载同版Python wheel与VSIX后，按安装说明配置Runtime路径及模型。安装包与原发布源码对应；正式测评的冻结源码和原始补丁、评分另随Release附件提供。

## 源码布局

| 目录 | 内容 |
| --- | --- |
| `codeagent/` | Runtime、工具、会话、上下文、CLI及RPC实现 |
| `vscode-extension/` | VS Code扩展源码及渲染测试 |
| `benchmarks/` | 正式50题的题单、模型配置与价格快照 |
| `docs/` | 安装、架构及测评报告 |

本快照保留v0.5.0已发布的Runtime和插件执行源码。产品后续演进与当前完整回归测试见[最新版本](https://github.com/YeYan0829/codingAgent)。
