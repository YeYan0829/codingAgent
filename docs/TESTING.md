# 测试说明

## 单元测试

```bash
pytest -q
```

当前测试覆盖：

- session store、用户级 session root、旧 session fallback、标题生成、append-only events、transcript。
- CLI provider/model/session-root 选项，以及跨 workspace 的全局 list/resume 编号选择 fallback。
- workspace path guard、敏感文件拒绝和模板文件例外。
- filesystem tools、git readonly tools、tool schema conversion。
- policy 默认权限。
- FakeLLM runner 回归。
- DeepSeekClient mock 响应：普通文本、tool_calls、malformed arguments、extra_body 回退。
- assistant tool_calls 与 tool result 的上下文配对重建。

测试环境通过 `tests/conftest.py` 将 `CODEAGENT_SESSION_ROOT` 指向临时目录，避免污染真实用户目录。

## Fake smoke test

```bash
codeagent ask . "帮我看看这个项目结构" --session-root .tmp-sessions
```

预期：不需要 API key，FakeLLM 完成 `list_dir` / `read_file` 工具循环。CLI 会显示工具参数。

## DeepSeek 手动测试

PowerShell：

```powershell
$env:DEEPSEEK_API_KEY="你的 key"
codeagent ask examples\buggy-repos\tiny-sort-bug "请只读分析这个仓库的测试为什么失败，不要修改文件。" --provider deepseek --model deepseek-v4-flash
```

预期：

- 真实 LLM 请求只读工具。
- runtime 打印工具调用和参数，例如 `read_file {"path": "sort_utils.py"}`。
- 最终回答指出 `sort_utils.py` 中 `list.reverse()` 返回 `None`，导致测试失败。
- 不写文件、不执行 shell。

## Session 手动测试

```bash
codeagent list-sessions .
codeagent resume --workspace .
codeagent resume <session_id> .
```

更常用的全局方式：

```bash
codeagent list-sessions
codeagent resume
codeagent resume <session_id>
```

UTF-8 TTY 下 `resume` 会使用上下键选择器；Windows GBK 等非 UTF-8 终端和非 TTY 下会显示编号并等待输入编号。`--workspace` 只用于过滤某个项目。

## 当前已知限制

真实 DeepSeek 测试需要 `DEEPSEEK_API_KEY` 和网络。写文件、任意 shell、Docker sandbox、benchmark harness 和真实 MCP 都不在当前范围内。
