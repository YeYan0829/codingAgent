# 测试说明

## 单元测试怎么跑

```bash
python -m pip install -e ".[dev]"
pytest -q
```

当前测试覆盖：

- `test_session_store.py`：创建 session、写入 events、append-only、读取 meta 和 events。
- `test_path_guard.py`：workspace 内路径允许、`../` 逃逸拒绝、绝对路径逃逸拒绝、symlink 逃逸拒绝。
- `test_sensitive_files.py`：`.env`、`id_rsa`、`token_config.txt`、`secret.json` 等敏感文件拒绝；`.env.example`、`.env.sample`、`.env.template` 默认允许。
- `test_tools_fs_read.py`：目录列表、敏感文件标记、文本读取、行范围、搜索、glob、大文件截断、`.env` 内容拒绝、`.env.example` 可读。
- `test_policy.py`：READ allow，EXEC_READONLY ask，WRITE/NETWORK/DANGEROUS deny。
- `test_runner_fake_llm.py`：FakeLLM 完成 `list_dir`、`read_file` 和最终 `assistant_message`。

## 功能测试怎么跑

```bash
codeagent ask . "帮我看看这个项目结构"
```

预期结果：非交互式完成一轮 FakeLLM tool loop，打印工具调用过程和最终中文总结。

## 敏感文件策略测试

默认策略是 `SHOW_MARKED`：

```text
/call list_dir {"path":"."}
```

如果目录里存在 `.env`，预期显示：

```text
.env [sensitive, content blocked]
```

读取 `.env` 仍应失败：

```text
/call read_file {"path":".env"}
```

搜索 `.env` 内容也应跳过，不返回 secret value。模板文件 `.env.example`、`.env.sample`、`.env.template` 默认可读。

## 手动挑战用例

正常探索：

```bash
codeagent start .
```

输入“帮我看看这个项目是干嘛的”。预期 agent 调用 `list_dir` 和 `read_file`，然后总结。

session 恢复：

```bash
codeagent list-sessions .
codeagent resume <session_id> .
```

预期能恢复会话，并在进入交互前显示 session id、事件数量、transcript 路径和最近几条历史。进入后执行 `/status` 也会显示同样的 session 概览。

这里最后的 `.` 是 workspace 参数，表示从当前目录下的 `.codeagent/sessions/` 查找 session。

手动工具调用：

```text
/call list_dir {"path":"."}
/call read_file {"path":"README.md","start_line":1,"end_line":20}
```

路径逃逸拒绝：

```text
/call read_file {"path":"../outside.txt"}
```

预期被拒绝。

approval：

```text
/call git_status {}
```

预期提示用户 y/N；输入 n 时拒绝，输入 y 时执行 `git status --short`。

大文件截断：

创建 `large.txt` 后执行：

```text
/call read_file {"path":"large.txt"}
```

预期输出被截断，`truncated=true`。

## 当前已知限制

交互式 CLI 需要人工输入，无法完全自动化验证 approval 的 y/N 分支。真实 LLM、任意 shell、写文件、Docker sandbox、benchmark harness 和真实 MCP 都不在 v0.1 范围内。
