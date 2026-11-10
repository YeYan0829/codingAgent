# 提示词与工具类型的来源及许可

本项目随附的改编文本和复用类型使用以下固定上游版本；许可证与源码一同提供。

| 本地内容 | 上游来源 | 许可 |
| --- | --- | --- |
| `edit_file.md` 的精确匹配、唯一上下文与小段替换说明 | [Aider `editblock_prompts.py`](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/editblock_prompts.py) | [Apache-2.0](LICENSE.aider) |
| `condenser.md` 的任务、代码状态与测试分节结构 | [OpenHands 摘要模板](https://github.com/OpenHands/software-agent-sdk/blob/704cbe6015e3d59cabe04632175d99df2d448999/openhands-sdk/openhands/sdk/context/condenser/prompts/summarizing_prompt.j2) | [MIT](LICENSE.openhands-sdk) |
| 三个 `subagent_*.md` 角色提示、`tools/upstream/task.py` 与 `task_tracker.py` | [OpenHands software-agent-sdk](https://github.com/OpenHands/software-agent-sdk/tree/704cbe6015e3d59cabe04632175d99df2d448999) 的角色提示与 TaskAction、TaskItem 类型 | [MIT](../../tools/upstream/LICENSE.OpenHands) |

编辑提示采用本地 JSON 参数 `path/edits/old_text/new_text`，由文件事务实现匹配、原子写入和撤销。摘要模板补充了中文来源规则与验证范围。角色提示按本地实际工具适配：代码调查只读，命令和实现角色的权限受 ToolRegistry、Policy、Approval 与 Bubblewrap 约束。

工具输入使用本地 Pydantic 类型生成 schema，并在执行前校验。上游工具定义的职责划分作为设计参考；运行时使用本项目的文件、命令与权限实现。
