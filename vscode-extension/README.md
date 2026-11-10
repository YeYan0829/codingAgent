# CodeAgent for VS Code

在编辑器侧栏描述代码任务，查看模型调查、文件编辑与命令反馈，并通过原生Diff审查当前项目的变化。CodeAgent v0.6.0支持持久受控undo、权限批准、Stop和会话历史；可选同步子角色提供代码调查、命令或局部实现分工。

需要Linux／WSL2、VS Code 1.106+及配套Python Runtime。安装VSIX后，将`codeagent.rpcCommand`设为Runtime的`codeagent-rpc`绝对路径，再从侧栏命令菜单配置模型与API Key。密钥保存在VS Code SecretStorage。

1. 打开项目，保存仓库访问范围并连接模型。
2. 新建会话，描述目标与测试命令。
3. 查看按序工具反馈，按需批准或停止。
4. 展开修改文件打开原生Diff；需要恢复时确认撤销最近一轮受控编辑。

修改直接写入当前项目。Undo检查全部涉及文件，外部改动冲突时保留现场；命令的其他副作用继续保留。标题栏历史支持搜索和切换会话，各自保留草稿。

[安装与使用](../docs/INSTALLATION.md)说明wheel与VSIX安装、首次任务及故障处理；[架构与设计](../docs/ARCHITECTURE.md)解释执行、上下文、权限和恢复；[测评报告](../docs/HAL50_EVALUATION.md)展示固定HAL50的40/50结果与两版证据。

源码开发、免费样本、测试与打包入口见[开发测试与构建](../docs/INSTALLATION.md#7-开发测试与构建)。
