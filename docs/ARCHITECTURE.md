# v0.5.0 架构设计

v0.5.0将调查、修改、验证和人工采纳组织成一条可审查的任务流程。模型通过受控工具读取项目、提出操作和接收执行结果；Runtime维护执行状态与会话事件，VS Code通过本地JSON-RPC展示时间线、审批、验证和Diff。

## 隔离修改与人工交付

读取从源项目开始，需要编辑或命令时建立独立Git worktree。候选目录中的修改与源目录分离，验证记录绑定相应文件树。开发者选择Accept后才将候选修改应用到源项目；Discard处理候选目录，源项目保持原状。采纳时还会检查源项目是否发生并发变化。

实现入口包括`codeagent/workspace/git_worktree.py`、`runtime/candidate.py`、`runtime/edit_service.py`和`runtime/applied_changes.py`。工具注册、路径检查、Policy与Approval约束模型操作，命令通过Bubblewrap隔离执行。

## 会话、上下文与产品界面

`session/`保存消息、工具结果和执行事件；`context/`装配模型请求并管理上下文压缩；`model_gateway/`对接Provider。CLI与`product/`使用同一Runtime能力，VS Code扩展通过`rpcClient.js`连接本地RPC进程，将执行反馈和修改审查放进任务界面。

## 正式测评

`benchmark/`提供SWE-bench适配、固定题单、批次与费用统计。正式50题使用统一模型参数和实验限制，候选补丁在独立评分流程中接受官方测试。版本、配置、完整50题结果与失败类型见[测评报告](HAL50_EVALUATION.md)。
