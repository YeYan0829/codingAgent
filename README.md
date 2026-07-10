# CodeAgent Runtime

CodeAgent Runtime 是一个本地交互式 Coding Agent Harness / Runtime 原型。模型通过 ReAct-style tool loop 提出只读工具调用，runtime 负责 session、工具调用、approval、安全边界和日志记录。

## 当前能力

- Typer CLI：`start`、`ask`、`list-sessions`、`resume`。
- 默认 FakeLLM：不需要 API key，仍可完成本地 smoke test。
- DeepSeek provider：通过 OpenAI SDK 调用 DeepSeek Chat Completions tool calling。
- Session 存储：默认写入用户级 `~/.codeagent/sessions/<workspace-key>/<session_id>/`，也支持 `CODEAGENT_SESSION_ROOT` 或 `--session-root` 指定位置。
- Session 标题：根据第一条用户消息生成，`list-sessions` 和 `resume` 会展示标题。
- 只读工具：`list_dir`、`show_tree`、`read_file`、`search_text`、`find_files`。
- 只读 git 工具：`git_status`、`git_diff_stat`，默认需要 approval。
- Safety：workspace path guard、敏感内容读取拒绝、敏感文件 listing 标记、权限策略。

## 安装

```bash
python -m pip install -e ".[dev]"
```

## 基本用法

默认使用 fake provider：

```bash
codeagent ask . "帮我看看这个项目结构"
codeagent start .
```

使用 DeepSeek：

```powershell
$env:DEEPSEEK_API_KEY="你的 key"
codeagent ask examples\buggy-repos\tiny-sort-bug "请只读分析这个仓库的测试为什么失败，不要修改文件。" --provider deepseek --model deepseek-v4-flash
```

指定 session 存储位置：

```powershell
$env:CODEAGENT_SESSION_ROOT="D:\codeagent-sessions"
codeagent ask . "帮我看看这个项目结构"

codeagent ask . "帮我看看这个项目结构" --session-root D:\codeagent-sessions
```

恢复 session：

```bash
codeagent list-sessions
codeagent resume
```

默认会列出/选择统一 session root 下的全部 session，并显示每条 session 对应的 workspace。需要只看某个项目时再加 workspace 过滤：

```bash
codeagent list-sessions --workspace .
codeagent resume --workspace .
codeagent resume <session_id>
```

在 UTF-8 TTY 中会优先使用上下键选择器；Windows GBK 等非 UTF-8 终端和非 TTY 环境会退回编号选择，避免中文显示乱码。

## 当前限制

- 真实 LLM 只能提出工具调用，工具执行必须经过 runtime / policy / approval / ToolRegistry。
- 没有写文件、删除文件、apply patch 能力。
- 没有任意 shell。
- 没有 Docker sandbox。
- 没有 benchmark harness。
- 没有真实 MCP 协议。
- DeepSeek strict mode 未启用；thinking mode 默认关闭。
