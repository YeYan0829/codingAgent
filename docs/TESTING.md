# 测试说明

## 单元测试

```bash
pytest -q
```

当前测试覆盖：

- session store、append-only events、transcript。
- workspace path guard、敏感文件拒绝和模板文件例外。
- filesystem tools、git readonly tools、tool schema conversion。
- policy 默认权限。
- FakeLLM runner 回归。
- DeepSeekClient mock 响应：普通文本、tool_calls、malformed arguments、extra_body 回退。
- provider factory 和 CLI provider/model 选项。
- assistant tool_calls 与 tool result 的上下文配对重建。

## Fake smoke test

```bash
codeagent ask . "帮我看看这个项目结构"
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

## Session 位置

session 存在 workspace 内，例如：

```text
examples/buggy-repos/tiny-sort-bug/.codeagent/sessions/<session_id>/
```

因此对不同 workspace 运行 `codeagent ask/start` 会产生分散的 session。当前可以用 `codeagent list-sessions <workspace>` 查看某个 workspace 的历史。

## 当前已知限制

交互式 CLI 需要人工输入。真实 DeepSeek 测试需要 `DEEPSEEK_API_KEY` 和网络。写文件、任意 shell、Docker sandbox、benchmark harness 和真实 MCP 都不在 v0.2 范围内。
