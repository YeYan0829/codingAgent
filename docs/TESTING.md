# 测试说明

## 自动化测试

```bash
pytest -q
```

v0.3 冻结基线：

```text
92 passed, 1 skipped
```

该数字是 v0.3 的历史记录，不等同于当前 v0.4 工作区结果；v0.4 的最终数字以本次全量 `pytest -q` 为准。

2026-08-11 v0.4 功能分支全量结果（Python 3.11 / Windows）：

```text
112 passed, 2 skipped in 76.95s
```

两个 skip 都是当前 Windows 测试环境不能创建 symlink；对应拒绝逻辑仍由可创建 symlink 的平台用例覆盖，不是功能执行失败。

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
- `apply_text_patch` 对 active/source 边界、越界、symlink、敏感路径、二进制、UTF-8、大小和上下文匹配的检查。
- Windows CRLF 文件可以使用 `read_file` 返回的 LF 上下文编辑，并保持 CRLF；混合行尾不被隐式重写。
- edit journal 的前后 hash、diff 和 session/workspace identity。
- 确定性模型执行读取、pytest 失败、编辑、pytest 通过、冻结 Candidate 的完整 tool loop。
- Candidate patch/hash 冻结、UTF-8 新文件、测试 receipt 引用和 workspace side effects。
- apply 拒绝、source dirty、HEAD 改变、批准期间并发变化、固定 hash 应用与 apply receipt。
- journaled candidate dirty resume 与普通未知 dirty worktree 的能力分流。
- 八个工具步骤之后仍保留最终模型总结额度；Candidate status 路径解析保留 Git porcelain 前导状态列。

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

## 真实终端验收

当前唯一清单见 [MANUAL_TEST.md](MANUAL_TEST.md)，合并覆盖历史 v0.3 readonly/controlled pytest 基线和 v0.4 edit/Candidate/apply 闭环。真实 DeepSeek 验收需要 API key、网络和真实 TTY，只能使用 disposable fixture。

人工清单关注用户可观察行为和最终交付证据；内部 schema、逐个 artifact 文件、异常分支组合和平台差异主要由自动化测试覆盖。

## 当前测试不能证明什么

自动化测试验证协议、路径、审计和生命周期行为，但不能证明 pytest 受到主机级安全隔离。当前没有网络、CPU、内存、磁盘、进程数量或系统调用 sandbox；只应在可信本地仓库中使用 execution mode。

自动化测试也不证明真实 DeepSeek 在所有任务上都会主动选择正确的编辑上下文或完成多轮自我修正。真实 provider 的 TTY 验收步骤见 [MANUAL_TEST.md](MANUAL_TEST.md)，需要用户显式提供 API Key 和网络授权。
