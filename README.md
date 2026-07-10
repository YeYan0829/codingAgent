# CodeAgent Runtime

CodeAgent Runtime 是一个本地交互式 Coding Agent Harness / Runtime 原型。v0.1 的目标不是自动修复代码，而是搭建一个可运行、可测试、可扩展的基础框架：用户通过 CLI 进入本地代码库，agent 使用 ReAct-style tool loop 做只读探索，runtime 负责 session、工具调用、approval、安全边界和日志记录。

## v0.1 当前能力

- Typer CLI：`start`、`ask`、`list-sessions`、`resume`。
- FakeLLM：不需要真实 LLM API key，默认可完成一轮仓库探索。
- Session 存储：`.codeagent/sessions/<session_id>/meta.json`、`events.jsonl`、`transcript.md`。
- 只读工具：`list_dir`、`show_tree`、`read_file`、`search_text`、`find_files`。
- 只读 git 工具：`git_status`、`git_diff_stat`，默认需要 approval。
- Safety：workspace path guard、敏感文件屏蔽、权限策略。
- Observability：append-only events 与人类可读 transcript。

## 安装方式

```bash
python -m pip install -e ".[dev]"
```

## CLI 使用方式

```bash
codeagent start .
codeagent ask . "帮我看看这个项目结构"
codeagent list-sessions .
codeagent resume <session_id> .
```

其中 `.` 是 workspace 参数，表示当前目录。也可以换成其他项目路径，例如 `codeagent start D:\path\to\repo`。`resume` 的第一个参数是 session id，第二个参数是 workspace。

交互命令：

- `/exit`：退出当前 CLI。
- `/status`：查看 session meta。
- `/tools`：列出可用工具。
- `/log`：显示 transcript 路径。
- `/call <tool_name> <json_args>`：手动调用工具。

## Demo 流程

```bash
codeagent ask . "帮我看看这个项目结构"
```

默认 FakeLLM 会先请求 `list_dir({"path":"."})`，如果发现 README 文件，再请求读取 README 前 80 行，最后用中文总结顶层结构、README 大意和下一步探索建议。

## 当前限制

- 没有真实 LLM 调用，`OpenAIClient` 只是预留适配器。
- 没有写文件、删除文件、apply patch 能力。
- 没有任意 shell。
- 没有 Docker sandbox。
- 没有 benchmark harness。
- 没有真实 MCP 协议。

## 后续路线简述

优先接入 OpenAI function calling / tool calling adapter，再考虑受控 shell 与测试运行器。随后扩展 patch draft、approval-based apply_patch、git worktree sandbox、benchmark harness，以及更完整的 memory/context compression/project index。
