# CodeAgent Runtime

CodeAgent Runtime 是一个本地运行的受控 Coding Agent Runtime。v0.3.0 支持只读代码分析，以及 pytest-only controlled execution：模型可以读取代码、申请运行 pytest、观察结果并继续分析，但不能主动编辑项目文件。

当前包版本为 `0.3.0`。v0.3 的自动化回归和 Windows 真实 TTY/DeepSeek 核心端到端验收已经通过，形成可继续开发的受控执行基线；当前仍位于 `wip/v0.3` 分支，尚未合并到 `master` 或创建公开 release/tag。

Execution session 从用户仓库当前 HEAD 创建独立的 Git detached worktree，测试产生的缓存和代码状态变化留在任务工作区。命令还必须经过 `Policy` 和逐次 `Approval`，完整输出与副作用审计保存为 session artifact。

当前项目面向可信的本地 Git 仓库。Git worktree 保护的是代码状态，不是宿主机安全沙箱；项目不承诺抵御恶意仓库中的 Python 代码。

## 当前能力

- Typer CLI：`start`、`ask`、`list-sessions`、`resume`、`cleanup`。
- Session 持久化、标题、事件历史、transcript 和跨进程 resume。
- Fake provider，以及通过 OpenAI SDK 适配的 DeepSeek Chat Completions tool calling。
- 只读文件工具：`list_dir`、`show_tree`、`read_file`、`search_text`、`find_files`。
- 只读 Git 工具：`git_status`、`git_diff_stat`。
- `readonly` 与 `execution` 两种 session mode；默认仍为 `readonly`。
- Execution session 只接受完全干净的 Git 仓库，并创建唯一 detached worktree。
- 模型可通过 `run_check` 请求固定的 `sys.executable -m pytest ...`，不能提交 shell command string。
- `CommandPolicy` 与 `approve_once` / `deny`；非交互终端默认拒绝需要 approval 的命令。
- 固定 timeout、最小环境变量、`shell=False`、不可交互 stdin。
- 完整 stdout/stderr、request、result 和 workspace diff artifacts。
- Resume 时检查 source/worktree HEAD、Git 注册关系及 dirty 状态。
- `ready`、`source_dirty`、`stale`、`worktree_dirty`、`missing`、`discarded` 等 workspace 状态。
- 普通退出保留 worktree；用户可显式 cleanup，dirty worktree 不会被静默删除。
- 每次真实命令前后记录 Git 状态、tracked diff、untracked 文件和 changed files。

版本能力和完整数据流见 [v0.3 总览](docs/V03_OVERVIEW.md)。

## 安装

```bash
python -m pip install -e ".[dev]"
```

## CLI 示例

### Readonly

默认 mode 是 `readonly`，不创建 worktree，也不注册 `run_check`：

```bash
codeagent start .
codeagent ask . "分析这个项目的结构"
```

使用 DeepSeek：

```powershell
$env:DEEPSEEK_API_KEY="你的 key"
codeagent start . --provider deepseek --model deepseek-v4-flash
```

### Execution

Execution mode 只支持完全干净的 Git 仓库：

```bash
codeagent start . --mode execution --provider deepseek --model deepseek-v4-flash
```

交互过程中，模型调用 `run_check` 时 CLI 会展示固定后的完整 argv、相对 cwd、timeout、active workspace 和安全提示。用户只能选择本次批准或拒绝。

非交互命令也可创建 execution session，但当前会默认拒绝需要 approval 的 pytest：

```bash
codeagent ask . "运行 pytest 并解释失败" --mode execution --provider deepseek
```

### Resume 与 cleanup

```bash
codeagent list-sessions
codeagent resume <session-id>
codeagent cleanup <session-id>
```

普通退出不会删除 worktree，便于 resume。`cleanup` 只清理干净、仍被 Git 注册且位于受管 session root 的 worktree，不使用强制删除。

可通过环境变量或选项指定 session root：

```powershell
$env:CODEAGENT_SESSION_ROOT="D:\codeagent-sessions"
codeagent list-sessions
codeagent resume <session-id> --session-root D:\codeagent-sessions
```

## 当前限制

- 只支持 pytest，不支持 npm、pnpm、ruff、mypy 或任意 shell。
- Agent 不能主动写文件、编辑文件、应用 patch、merge 或 rebase。
- 不自动发现项目 `.venv`，pytest 使用运行 CodeAgent 的 `sys.executable`。
- 不安装 Python、Node 或其他项目依赖。
- 没有网络隔离或受控网络访问。
- 没有 Docker、bubblewrap、VM 或其他主机级 sandbox。
- 没有 CPU、内存、磁盘、进程数量和系统调用限制。
- Worktree 只隔离 Git 代码状态，不限制进程能访问的宿主机资源。
- pytest 仍以当前用户权限运行，可能访问 workspace 之外的文件、网络和本地服务。
- 当前仅适用于用户信任的本地仓库，不应对不可信项目启用 execution mode。

## 文档

- [v0.3 总览](docs/V03_OVERVIEW.md)：当前能力、模式、生命周期、安全边界和成熟度。
- [架构说明](docs/ARCHITECTURE.md)：模块职责、执行链、workspace、session 和 artifacts。
- [设计决策](docs/DECISIONS.md)：关键取舍以及为何不自动同步、stash 或直接写 source。
- [Roadmap](docs/ROADMAP.md)：从稳定 v0.3 到候选修改、正式 apply 和 dirty snapshot。
- [测试说明](docs/TESTING.md)：自动化测试、smoke test 和测试边界。
- [真实终端人工验收](docs/V03_MANUAL_TEST.md)：v0.3 release 前的人工检查清单。
