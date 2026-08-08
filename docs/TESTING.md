# 测试说明

## 自动化测试

```bash
pytest -q
```

v0.3 开发过程中记录的最近基线：

```text
91 passed, 1 skipped
```

该数字是仓库文档中的历史记录，不等同于当前工作区已经完成 release 验收。v0.3 仍应在收口后重新运行完整测试，并把运行环境、commit 和结果记录到 release 验收中。

测试覆盖：

- Session store、用户级 session root、旧 session fallback、metadata、事件和 transcript。
- CLI provider/model/session-root、readonly/execution mode、list、resume 和 cleanup。
- `WorkspaceContext`、Git detached worktree、双 session 隔离和保守 cleanup。
- `ready`、`source_dirty`、`stale`、`worktree_dirty` 和 `missing` 状态检查。
- Workspace path guard、敏感文件拒绝、文件与 Git 只读工具。
- `ToolSpec -> ModelTool` 与 provider adapter schema。
- `CommandSpec`、`CommandPolicy`、Approval deny/approve-once 和 receipt。
- `FakeCommandExecutor` 与真实 `LocalCommandExecutor`。
- 真实 pytest 成功、失败、spawn failure、timeout 和 cwd 二次检查。
- `shell=False`、不可交互 stdin、minimal environment 和敏感环境变量过滤。
- stdout/stderr、request/result、workspace patch artifacts。
- 命令前后 tracked/untracked workspace side-effect audit。
- Windows 可信绝对 `taskkill.exe` 路径。
- FakeLLM、DeepSeekClient mock、tool call history 和 readonly 回归。

`tests/conftest.py` 会把 `CODEAGENT_SESSION_ROOT` 指向临时目录，集成测试也使用临时 Git 仓库和 detached worktree，不依赖用户真实项目。

## Readonly smoke test

```bash
codeagent ask . "帮我看看这个项目结构" --session-root .tmp-sessions
```

预期：FakeLLM 完成只读工具循环，不创建 worktree，也不注册 `run_check`。

## DeepSeek readonly smoke test

PowerShell：

```powershell
$env:DEEPSEEK_API_KEY="你的 key"
codeagent ask examples\buggy-repos\tiny-sort-bug "请只读分析测试为什么失败，不要修改文件" --provider deepseek --model deepseek-v4-flash
```

预期：真实模型请求只读工具，Runtime 记录 tool calls，不运行 pytest，也不写文件。

## v0.3 真实终端验收

完整 15 项清单见 [V03_MANUAL_TEST.md](V03_MANUAL_TEST.md)。当前清单仍是待填写模板，覆盖：

- readonly/execution mode 分流；
- detached worktree 与 `run_check`；
- 真实 approval deny/approve once；
- pytest、摘要和 command artifacts；
- source 不受测试副作用污染；
- exit、resume、stale、dirty 和 cleanup；
- workspace side-effect audit；
- 两个 execution session 的隔离。

真实 DeepSeek 验收需要 API key、网络和真实 TTY。不要使用包含敏感代码或未提交修改的重要仓库。

## 当前测试不能证明什么

自动化测试验证协议、路径、审计和生命周期行为，但不能证明 pytest 受到主机级安全隔离。当前没有网络、CPU、内存、磁盘、进程数量或系统调用 sandbox；只应在可信本地仓库中使用 execution mode。
