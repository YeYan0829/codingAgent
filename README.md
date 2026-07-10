# CodeAgent Runtime

CodeAgent Runtime 是一个本地交互式 Coding Agent Harness / Runtime 原型。它的目标不是直接自动修复代码，而是搭建一个可运行、可测试、可扩展的基础框架：CLI 进入本地代码库，模型通过 ReAct-style tool loop 提出只读工具调用，runtime 负责 session、工具调用、approval、安全边界和日志记录。

## v0.2 当前能力

- Typer CLI：`start`、`ask`、`list-sessions`、`resume`。
- 默认 FakeLLM：不需要真实 API key，仍可完成本地 smoke test。
- DeepSeek provider：通过 OpenAI SDK 调用 DeepSeek Chat Completions tool calling。
- Session 存储：`.codeagent/sessions/<session_id>/meta.json`、`events.jsonl`、`transcript.md`。
- 只读工具：`list_dir`、`show_tree`、`read_file`、`search_text`、`find_files`。
- 只读 git 工具：`git_status`、`git_diff_stat`，默认需要 approval。
- Safety：workspace path guard、敏感内容读取拒绝、敏感文件 listing 标记、权限策略。
- Observability：append-only events 与人类可读 transcript。

## 安装方式

```bash
python -m pip install -e ".[dev]"
```

## CLI 使用方式

默认仍使用 fake provider：

```bash
codeagent ask . "帮我看看这个项目结构"
codeagent start .
```

使用 DeepSeek：

```powershell
$env:DEEPSEEK_API_KEY="你的 key"
codeagent ask examples\buggy-repos\tiny-sort-bug "请只读分析这个仓库的测试为什么失败，不要修改文件。" --provider deepseek --model deepseek-v4-flash
codeagent start examples\buggy-repos\tiny-sort-bug --provider deepseek --model deepseek-v4-flash
```

参数说明：

- `workspace`：代码工作区目录；`.` 表示当前目录。
- `--provider`：模型 provider，当前支持 `fake` 和 `deepseek`。
- `--model`：provider 内的模型编号；DeepSeek 默认 `deepseek-v4-flash`，也可使用 `deepseek-v4-pro`。

## DeepSeek Key

DeepSeek API key 使用环境变量：

```powershell
$env:DEEPSEEK_API_KEY="你的 key"
```

不要把 key 写进代码或提交到仓库。`.env`、`.env.*` 已被 `.gitignore` 忽略，并且 runtime 会拒绝读取敏感文件内容。

## Demo 流程

Fake smoke test：

```bash
codeagent ask . "帮我看看这个项目结构"
```

真实 LLM 只读 bug 分析：

```powershell
codeagent ask examples\buggy-repos\tiny-sort-bug "请只读分析这个仓库的测试为什么失败，不要修改文件。" --provider deepseek --model deepseek-v4-flash
```

## 当前限制

- 真实 LLM 仍然只能提出工具调用，工具执行必须经过 runtime / policy / approval / ToolRegistry。
- 没有写文件、删除文件、apply patch 能力。
- 没有任意 shell。
- 没有 Docker sandbox。
- 没有 benchmark harness。
- 没有真实 MCP 协议。
- DeepSeek strict mode 未启用；thinking mode 默认关闭。

## 后续路线简述

下一步可以继续加强真实 LLM 的 tool-call 历史恢复、错误恢复和受控 test runner。写操作、patch、sandbox、benchmark、MCP 仍应作为后续独立阶段推进。
